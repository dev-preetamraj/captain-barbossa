"""Agent transcripts read from each provider's own session files, never from a second copy."""

# An entry is one block of one native record, addressed FILE:OFFSET:BLOCK (session file index,
# record byte offset, block); native session files only grow, so addresses stay valid.

import json
import os
import re
import stat
from collections import deque
from functools import partial
from pathlib import Path

from . import captain_bridge, eventlog, safe_read, usage
from .runtime import CaptainError

# Characters of an entry's text in a page; `step` returns its whole block up to STEP_CHARS.
TEXT_CHARS = 4096
STEP_CHARS = 1024 * 1024
DEFAULT_LIMIT = 200
MAX_LIMIT = 500
CURSOR = re.compile(r"(\d+):(\d+)")
ENTRY = re.compile(r"(\d+):(\d+):(\d+)")
INCARNATION = re.compile(r"[a-f0-9]{32}")


def events_path(session, agent, incarnation=None):
    """The events file of `agent` (a crew id or `captain`); a crew defaults to its live incarnation."""
    if agent == "captain":
        return session.events("captain")
    record = session.meta.get("crew", {}).get(agent)
    if record is None:
        raise CaptainError(f"[transcript-agent] No agent {agent} in this session.")
    incarnation = incarnation or record.get("incarnation_id")
    if not isinstance(incarnation, str) or not INCARNATION.fullmatch(incarnation):
        raise CaptainError(f"[transcript-agent] {agent} has no incarnation {incarnation}.")
    return session.events(f"{agent}-{incarnation}")


def _text(value):
    """Plain text of a native content value: a string, a text block, or a list of them."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text for item in value if (text := _text(item)))
    if isinstance(value, dict):
        return _text(value.get("text", value.get("content", "")))
    return ""


def _arguments(value):
    if isinstance(value, str):
        return value
    return json.dumps(eventlog.redact(value, eventlog.known_secrets()), ensure_ascii=False)


def _block(role, kind, text, tool=None, call=None, origin=None):
    return {"role": role, "kind": kind, "text": text, "tool": tool, "call": call, "origin": origin}


def _content(role, content):
    """Blocks of a Claude or pi message body: text, thinking and tool calls."""
    if isinstance(content, str):
        return [_block(role, "text", content)]
    blocks = []
    for item in content if isinstance(content, list) else ():
        kind = item.get("type") if isinstance(item, dict) else None
        if kind == "text":
            blocks.append(_block(role, "text", _text(item.get("text"))))
        elif kind == "thinking":
            blocks.append(_block(role, "thinking", _text(item.get("thinking"))))
        elif kind in ("tool_use", "toolCall"):
            text = _arguments(item.get("input", item.get("arguments")))
            blocks.append(_block(role, "tool_call", text, item.get("name"), item.get("id")))
        elif kind == "tool_result":
            text = _text(item.get("content"))
            blocks.append(_block("tool", "tool_result", text, None, item.get("tool_use_id")))
    return blocks


def _claude(record):
    message = record.get("message")
    if record.get("type") not in ("user", "assistant") or not isinstance(message, dict):
        return []
    return _content(message.get("role", record["type"]), message.get("content"))


def _codex(record):
    item = record.get("payload")
    if record.get("type") != "response_item" or not isinstance(item, dict):
        return []
    kind, call = item.get("type"), item.get("call_id")
    if kind == "message":
        role = {"developer": "system"}.get(item.get("role"), item.get("role"))
        return [_block(role, "text", _text(item.get("content")))]
    if kind == "reasoning":
        text = _text(item.get("summary"))
        return [_block("assistant", "thinking", text)] if text else []
    if kind in ("function_call", "custom_tool_call"):
        text = _arguments(item.get("arguments", item.get("input")))
        return [_block("assistant", "tool_call", text, item.get("name"), call)]
    if kind == "local_shell_call":
        action = item.get("action")
        text = _arguments(action.get("command") if isinstance(action, dict) else action)
        return [_block("assistant", "tool_call", text, "shell", call)]
    if kind in ("function_call_output", "custom_tool_call_output"):
        return [_block("tool", "tool_result", _text(item.get("output")), None, call)]
    return []


def _pi(record):
    message = record.get("message")
    if record.get("type") != "message" or not isinstance(message, dict):
        return []
    role, content = message.get("role"), message.get("content")
    if role == "toolResult":
        tool, call = message.get("toolName"), message.get("toolCallId")
        return [_block("tool", "tool_result", _text(content), tool, call)]
    return _content(role, content) if role in ("user", "assistant") else []


PARSERS = {"claude": _claude, "codex": _codex, "pi": _pi}


def _captain(parse, prompts, record):
    """The captain's user text split into parts, each with the origin the bridge wrote it for."""
    blocks = []
    for block in parse(record):
        if block["role"] != "user" or block["kind"] != "text":
            blocks.append(block)
            continue
        for origin, text in captain_bridge.parts(block["text"], prompts):
            blocks.append({**block, "text": text, "origin": origin})
    return blocks


def _open(path, root=None):
    """A native session file opened for reading: regular, and with a root, no symlink below it."""
    try:
        if root is None:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        else:
            with safe_read._open(root, path) as walked:
                fd = os.dup(walked)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise CaptainError(f"[transcript-file] Cannot open {path}: {exc.strerror}") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise CaptainError(f"[transcript-file] Not a regular file: {path}")
    return os.fdopen(fd, "rb")


def _records(files, root, start=(0, 0)):
    """(position, end, record) for each complete JSON object line at or after `start`."""
    for index in range(start[0], len(files)):
        try:
            file = _open(files[index], root)
        except FileNotFoundError:
            continue  # cleaned up with the crew's native history
        with file:
            offset = start[1] if index == start[0] else 0
            file.seek(offset)
            for line in file:
                if not line.endswith(b"\n"):
                    break  # still being written; a later read takes it whole
                position, offset = (index, offset), offset + len(line)
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield position, (index, offset), record


def _clip(text, limit):
    """(text cut to `limit`, whether it was cut), redacted whole first so no secret is split."""
    text = eventlog.redact_text(text, eventlog.known_secrets())
    return text[:limit], len(text) > limit


def _entries(parse, position, record):
    entries = []
    for number, block in enumerate(parse(record)):
        text, clipped = _clip(block["text"], TEXT_CHARS)
        entries.append(
            {
                "id": f"{position[0]}:{position[1]}:{number}",
                "at": record.get("timestamp"),
                **block,
                "text": text,
                "clipped": clipped,
            }
        )
    return entries


def _cursor(position):
    return f"{position[0]}:{position[1]}" if position is not None else None


def _position(value, name):
    match = CURSOR.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise CaptainError(f"[transcript-range] {name} must be a FILE:OFFSET cursor.")
    return int(match[1]), int(match[2])


def _sources(session, agent, incarnation):
    provider, files, root = usage.native_files(events_path(session, agent, incarnation))
    parse = PARSERS.get(provider)
    if agent == "captain" and parse is not None:
        parse = partial(_captain, parse, captain_bridge.prompt_digests(session.directory))
    return provider, [Path(file) for file in files], root, parse


def read(session, agent, *, after=None, before=None, limit=DEFAULT_LIMIT, incarnation=None):
    """The first `limit` entries from `after`, else the last before `before`; never splits a record."""
    if type(limit) is not int or not 0 < limit <= MAX_LIMIT:
        raise CaptainError(f"[transcript-range] limit must be 1 to {MAX_LIMIT}.")
    if after is not None and before is not None:
        raise CaptainError("[transcript-range] Pass after or before, not both.")
    first = _position(after, "after") if after is not None else (0, 0)
    stop = _position(before, "before") if before is not None else None
    provider, files, root, parse = _sources(session, agent, incarnation)
    pages, end, more = deque(), first, False
    if parse is not None:
        count = 0
        for position, finish, record in _records(files, root, first):
            if stop is not None and position >= stop:
                break
            found = _entries(parse, position, record)
            if after is not None and found and count >= limit:
                more = True
                break
            end = finish
            if found:
                pages.append((position, found))
                count += len(found)
                while after is None and count - len(pages[0][1]) >= limit:
                    count -= len(pages.popleft()[1])
                    more = True
    return {
        "agent": agent,
        "provider": provider,
        "files": [str(file) for file in files],
        "entries": [entry for _, found in pages for entry in found],
        "start": _cursor(pages[0][0] if pages else first if after is not None else stop),
        "end": _cursor(end),
        "more": more,
    }


def step(session, agent, entry, *, incarnation=None):
    """One entry's whole block, re-read from its native record."""
    match = ENTRY.fullmatch(entry) if isinstance(entry, str) else None
    if match is None:
        raise CaptainError("[transcript-entry] An entry id is FILE:OFFSET:BLOCK.")
    index, offset, number = (int(group) for group in match.groups())
    _, files, root, parse = _sources(session, agent, incarnation)
    if parse is None or index >= len(files):
        raise CaptainError(f"[transcript-entry] {agent} has no entry {entry}.")
    try:
        file = _open(files[index], root)
    except FileNotFoundError as exc:
        raise CaptainError(f"[transcript-gone] {files[index]} no longer exists.") from exc
    with file:
        if offset:
            file.seek(offset - 1)
            if file.read(1) != b"\n":
                raise CaptainError(f"[transcript-entry] {entry} is not a record boundary.")
        file.seek(offset)
        line = file.readline()
    try:
        record = json.loads(line)
    except ValueError:
        record = None
    blocks = parse(record) if isinstance(record, dict) else []
    if number >= len(blocks):
        raise CaptainError(f"[transcript-entry] {agent} has no entry {entry}.")
    text, clipped = _clip(blocks[number]["text"], STEP_CHARS)
    return {
        "id": entry,
        "at": record.get("timestamp"),
        **blocks[number],
        "text": text,
        "clipped": clipped,
    }
