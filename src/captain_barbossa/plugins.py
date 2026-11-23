"""Plugins: home-registered executables that add org context and crew command denials.

Each call is one process with one JSON request and one JSON response. `sync` returns a
whole snapshot, which captain publishes as an immutable generation behind one atomically
replaced pointer, so a reader sees one complete snapshot or the previous one. Launch reads
only cached state and never waits on a plugin; docs/plugin-design.md is the wire contract.
"""

import contextlib
import hashlib
import json
import os
import re
import secrets
import select
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from . import __version__, config
from .runtime import CaptainError
from .store import lock, private_dir, read_json, state_root, write_json, write_text

NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
CODE = re.compile(r"[a-z_]{1,64}")
# LF and TAB only, so a provider that JSON-escapes the launch text at most doubles it.
FORBIDDEN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
PROTOCOLS = (1,)
POINTS = {"context": (1,), "crew_denials": (1,)}
ROLES = ("captain", "crew")
DEADLINES = {"hello": 10, "sync": 120}
MAX_OUTPUT = 4 * 1024 * 1024
PLUGIN_CEILING = 8_192
TOTAL_CEILING = 16_384
REFRESH_SECONDS = 900
STALE_SECONDS = 86_400
# Launch paths with no gate captain can trust to stop a shell command before it runs.
UNGATED = {("codex", "foreground"), ("grok", "foreground"), ("codex", "background")}
UNGATED_QUIET = {"codex"}


class PluginError(CaptainError):
    def __init__(self, code, message):
        super().__init__(f"[{code}] {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Negotiated:
    """What captain and one plugin agreed in `hello`; `mismatch` is one line, or None."""

    versions: dict
    denials: tuple
    refresh: int
    mismatch: str | None


def registered():
    """{name: entry} from home settings only; a repo's [plugins] table is never read."""
    table = config.home_settings().get("plugins")
    if not isinstance(table, dict):
        return {}
    return {
        name: entry
        for name, entry in table.items()
        if NAME.fullmatch(name)
        and isinstance(entry, dict)
        and isinstance(entry.get("command"), str)
    }


def directory(name):
    return state_root() / "plugins" / name


def request(call, options, *, points=None, server_id=None):
    payload = {
        "protocol": PROTOCOLS[-1],
        "call": call,
        "captain_version": __version__,
        "options": options,
    }
    if call == "hello":
        supported = {point: list(versions) for point, versions in POINTS.items()}
        payload["supported"] = {"protocol": list(PROTOCOLS), "points": supported}
    else:
        payload.update(points=points, server_id=server_id)
    return payload


def invoke(entry, payload):
    """Run the plugin once; the deadline or reading past MAX_OUTPUT kills its process group."""
    command = entry["command"]
    try:
        process = subprocess.Popen(
            [command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise PluginError("plugin_exit", f"cannot run {command}: {exc}") from exc
    try:
        output = _read_bounded(process, payload, time.monotonic() + DEADLINES[payload["call"]])
    except BaseException:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.stdout.close()
        process.wait()
        raise
    if process.returncode:
        raise PluginError("plugin_exit", f"{payload['call']} exited {process.returncode}")
    try:
        return json.loads(output)
    except ValueError as exc:
        raise PluginError("bad_response", "response is not JSON") from exc


def _read_bounded(process, payload, deadline):
    """Stdout until EOF and exit, never holding more than MAX_OUTPUT bytes."""
    with contextlib.suppress(BrokenPipeError):
        process.stdin.write(json.dumps(payload).encode())
    with contextlib.suppress(BrokenPipeError):
        process.stdin.close()
    chunks, size, stream = [], 0, process.stdout.fileno()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PluginError("timeout", f"{payload['call']} took longer than allowed")
        if not select.select([stream], [], [], remaining)[0]:
            continue
        chunk = os.read(stream, 65536)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_OUTPUT:
            raise PluginError("bad_response", f"response exceeds {MAX_OUTPUT} bytes")
        chunks.append(chunk)
    try:
        process.wait(timeout=max(deadline - time.monotonic(), 0.001))
    except subprocess.TimeoutExpired as exc:
        raise PluginError("timeout", f"{payload['call']} took longer than allowed") from exc
    process.stdout.close()
    return b"".join(chunks)


def checked(response):
    """The response when it succeeded, else its error as a PluginError."""
    if isinstance(response, dict) and response.get("ok") is True:
        return response
    error = response.get("error") if isinstance(response, dict) else None
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        if CODE.fullmatch(error["code"]):
            message = str(error.get("message", "")).splitlines()[:1]
            raise PluginError(error["code"], (message or ["failed"])[0][:200])
    raise PluginError("bad_response", "response has neither ok nor a valid error")


def _versions(value):
    return [v for v in value if type(v) is int] if isinstance(value, list) else []


def negotiate(name, hello):
    """Agree the highest common version per point; never raises on what the plugin sent."""
    problems = []
    if not isinstance(hello, dict) or hello.get("name") != name:
        return Negotiated(
            {}, (), REFRESH_SECONDS, f"hello does not come from a plugin named {name}"
        )
    protocols = _versions(hello.get("protocol"))
    if not set(protocols) & set(PROTOCOLS):
        problems.append(f"speaks transport {protocols}; captain speaks {list(PROTOCOLS)}")
    versions, denials = {}, ()
    points = hello.get("points") if isinstance(hello.get("points"), dict) else {}
    for point, spec in points.items():
        spec = spec if isinstance(spec, dict) else {}
        offered = _versions(spec.get("versions"))
        common = set(offered) & set(POINTS.get(point, ()))
        if common:
            versions[point] = max(common)
        elif spec.get("required") is True:
            supported = list(POINTS.get(point, ()))
            problems.append(f"requires {point} v{offered}; captain supports {supported}")
    if versions.get("crew_denials") == 1:
        commands = points["crew_denials"].get("commands")
        valid = isinstance(commands, list) and all(
            isinstance(rule, list) and rule and all(isinstance(w, str) and w for w in rule)
            for rule in commands
        )
        if valid:
            denials = tuple(tuple(rule) for rule in commands)
        else:
            problems.append("crew_denials commands are not lists of words")
    refresh = hello.get("refresh_interval")
    refresh = refresh if type(refresh) is int and refresh > 0 else REFRESH_SECONDS
    return Negotiated(versions, denials, refresh, "; ".join(problems) or None)


def _options(entry):
    return entry.get("options") if isinstance(entry.get("options"), dict) else {}


def render(name, files, role, synced_at, stale):
    """One plugin's section for one role; '' when it has no file for that role.

    The freshness line has a fixed width, so the size admitted at sync is the size injected
    however stale the generation later becomes.
    """
    chosen = [item for item in files if role in item["roles"]]
    if not chosen:
        return ""
    freshness = "stale" if stale else "fresh"
    stamp = datetime.fromtimestamp(synced_at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = f"## Plugin context: {name}\nSynced {stamp} ({freshness}).\n"
    return text + "".join(f"\n### {item['path']}\n{item['text']}" for item in chosen)


def validate(name, reply):
    """The checked snapshot from a sync response, or why it cannot be published."""
    server_id, expires_at, files = (reply.get(key) for key in ("server_id", "expires_at", "files"))
    if not isinstance(server_id, str) or not server_id or len(server_id.encode()) > 256:
        raise PluginError("invalid_manifest", "server_id must be a string of 1 to 256 bytes")
    try:
        expires = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError) as exc:
        raise PluginError("invalid_manifest", "expires_at must be RFC 3339") from exc
    if expires.tzinfo is None:
        raise PluginError("invalid_manifest", "expires_at needs an explicit offset")
    if not isinstance(files, list):
        raise PluginError("invalid_manifest", "files must be a list")
    seen = set()
    for item in files:
        path = item.get("path") if isinstance(item, dict) else None
        path = path if isinstance(path, str) and path else "/"
        parts = PurePosixPath(path).parts
        roles = item.get("roles") if isinstance(item, dict) else None
        if (
            path in seen
            or path == "manifest.json"
            or any(ord(char) < 32 or char == "\x7f" for char in path)
            or any(other.startswith(path + "/") or path.startswith(other + "/") for other in seen)
            or path.startswith("/")
            or any(part in (".", "..") for part in parts)
            or path != "/".join(parts)
            or not isinstance(roles, list)
            or not set(roles) <= set(ROLES)
            or not isinstance(item.get("text"), str)
        ):
            raise PluginError("invalid_manifest", f"bad file entry {str(path)[:80]!r}")
        seen.add(path)
        if FORBIDDEN.search(item["text"]):
            raise PluginError("invalid_context", f"{path} has a control character")
    for role in ROLES:
        size = len(render(name, files, role, 0, True).encode())
        if size > PLUGIN_CEILING:
            raise PluginError(
                "context_too_large", f"{role} section is {size} bytes, over {PLUGIN_CEILING}"
            )
    return {"server_id": server_id, "expires_at": expires.timestamp(), "files": files}


def _read(path):
    try:
        return read_json(path) if path.is_file() else None
    except CaptainError:
        return None


def pointer(name):
    return _read(directory(name) / "current.json")


def manifest(name, generation):
    return _read(directory(name) / "generations" / generation / "manifest.json")


def section(name, generation, role, now=None):
    """The section a launch injects from this generation; '' when expired or missing."""
    found = manifest(name, generation)
    now = time.time() if now is None else now
    if not found or now >= found["expires_at"]:
        return ""
    root = directory(name) / "generations" / generation
    files = [
        {**item, "text": (root / item["path"]).read_text(encoding="utf-8")}
        for item in found["files"]
        if role in item["roles"]
    ]
    stale = now - found["synced_at"] > STALE_SECONDS
    return render(name, files, role, found["synced_at"], stale)


def _publish(root, snapshot):
    generation = f"{time.time_ns()}-{secrets.token_hex(4)}"
    synced_at = time.time()
    staging = root / f".tmp-{secrets.token_hex(4)}"
    staging.mkdir(mode=0o700)
    try:
        for item in snapshot["files"]:
            target = staging / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item["text"], encoding="utf-8")
        listing = [{"path": item["path"], "roles": item["roles"]} for item in snapshot["files"]]
        write_json(
            staging / "manifest.json",
            {
                "server_id": snapshot["server_id"],
                "synced_at": synced_at,
                "expires_at": snapshot["expires_at"],
                "files": listing,
            },
        )
        private_dir(root / "generations")
        os.rename(staging, root / "generations" / generation)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    current = {key: snapshot[key] for key in ("server_id", "expires_at")}
    write_json(root / "current.json", {"generation": generation, "synced_at": synced_at, **current})
    return generation


def collect(root):
    """Delete leftovers and every generation neither current nor pinned by a live session.

    Runs under the mirror lock, which every publisher holds, so nothing it deletes is being
    written; a pin whose session directory is gone no longer holds anything.
    """
    for leftover in root.glob(".tmp-*"):
        shutil.rmtree(leftover, ignore_errors=True)
    with lock(root / "refs.lock"):
        _collect_generations(root)


def _collect_generations(root):
    live = {(_read(root / "current.json") or {}).get("generation")}
    refs = root / "refs"
    for ref in refs.iterdir() if refs.is_dir() else ():
        if Path(ref.read_text(encoding="utf-8")).is_dir():
            live.add(ref.name.partition("@")[0])
        else:
            ref.unlink(missing_ok=True)
    generations = root / "generations"
    for generation in generations.iterdir() if generations.is_dir() else ():
        if generation.name not in live:
            shutil.rmtree(generation, ignore_errors=True)


def _status(root, code):
    write_json(root / "status.json", {"at": time.time(), "error": code})


def sync(name):
    """Refresh one plugin's mirror; the current generation survives any failure."""
    entry = registered().get(name)
    if entry is None:
        raise CaptainError(f"No plugin named {name} is registered in home settings.")
    root = private_dir(directory(name))
    with lock(root / "lock", timeout=0):
        try:
            generation = _sync(name, entry, root)
        except PluginError as exc:
            _status(root, exc.code)
            raise
        _status(root, None)
        return generation


def _sync(name, entry, root):
    hello = checked(invoke(entry, request("hello", _options(entry))))
    found = negotiate(name, hello)
    write_json(root / "hello.json", hello)
    if found.mismatch:
        raise PluginError("version_mismatch", found.mismatch)
    if "context" not in found.versions:
        return None
    last = _read(root / "current.json") or {}
    payload = request(
        "sync",
        _options(entry),
        points={"context": found.versions["context"]},
        server_id=last.get("server_id"),
    )
    generation = _publish(root, validate(name, checked(invoke(entry, payload))))
    collect(root)
    return generation


def pin(name, generation, session_directory):
    """Hold a generation for a session's crew; returns the one actually pinned.

    The ref is written and checked under the refs lock collection holds, so collection either
    sees the ref or has already taken the generation, and then the reader retries.
    """
    root = directory(name)
    key = hashlib.sha256(os.fsencode(str(session_directory))).hexdigest()[:16]
    for _ in range(3):
        ref = private_dir(root / "refs") / f"{generation}@{key}"
        with lock(root / "refs.lock"):
            write_text(ref, str(session_directory))
            if (root / "generations" / generation).is_dir():
                return generation
            ref.unlink(missing_ok=True)
        generation = (pointer(name) or {}).get("generation")
        if generation is None:
            return None
    raise CaptainError(f"Plugin {name} kept replacing its generation; recruit again.")


def binding():
    """The session's plugin binding at launch, from cached `hello`s only.

    An optional plugin on a version mismatch is skipped with one line and a required one
    blocks the launch; either way its denials still apply, so a mismatch never frees crew.
    """
    active, denials = [], []
    for name, entry in registered().items():
        hello = _read(directory(name) / "hello.json")
        if hello is None:
            continue
        found = negotiate(name, hello)
        denials += [list(rule) for rule in found.denials if list(rule) not in denials]
        if found.mismatch:
            if entry.get("required") is True:
                raise CaptainError(f"plugin {name} is required and blocked: {found.mismatch}")
            print(f"captain: plugin {name} skipped: {found.mismatch}", file=sys.stderr)
        elif "context" in found.versions:
            active.append(name)
    return {"active": active, "denials": denials}


def refresh():
    """Start a detached sync for every plugin whose last sync is older than its interval."""
    for name in registered():
        hello = _read(directory(name) / "hello.json")
        interval = negotiate(name, hello).refresh if hello else REFRESH_SECONDS
        synced_at = (pointer(name) or {}).get("synced_at", 0)
        if time.time() - synced_at <= interval:
            continue
        spawn([sys.executable, "-m", "captain_barbossa", "plugin", "sync", name])


def spawn(argv):
    """Start a process that outlives captain and never writes to its terminal."""
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass  # the next launch, or the org scheduler, tries again


def context(bound, role):
    """Every bound plugin's section for this role, within the aggregate ceiling.

    A captain binding names active plugins and reads their current generations; a crew
    binding names the generations it pinned. A plugin that does not fit is omitted whole.
    """
    if "generations" in bound:
        chosen = bound["generations"]
    else:
        chosen = {name: (pointer(name) or {}).get("generation") for name in bound["active"]}
    parts, total = [], 0
    for name, generation in chosen.items():
        body = section(name, generation, role) if generation else ""
        if not body:
            continue
        text = "\n" + body
        size = len(text.encode())
        if total + size > TOTAL_CEILING:
            text = (
                f"\n[plugin {name}: context omitted, {size} bytes would pass the "
                f"{TOTAL_CEILING}-byte total; see captain plugin status]\n"
            )
            size = len(text.encode())
            if total + size > TOTAL_CEILING:
                continue
        parts.append(text)
        total += size
    return "".join(parts)


def refuse(denials, provider, path, managed=True):
    """Refuse a crew launch path that has no gate able to enforce an active denial."""
    if not denials:
        return
    ungated = (
        provider in UNGATED_QUIET
        if path == "quiet"
        else (provider, path) in UNGATED or (provider, path, managed) == ("pi", "foreground", False)
    )
    if ungated:
        commands = ", ".join(f"`{' '.join(rule)}`" for rule in denials)
        raise CaptainError(
            f"[plugin-ungated] Captain plugins deny crew {commands}, and {provider} {path} "
            "work has no gate captain can trust to enforce that; use claude or pi crew, "
            "or start captain with --no-plugins."
        )


def bind(bound, record, provider, path, session_directory, managed=True):
    """Fix a crew's denials and pinned generations at recruit; returns its crew context."""
    denials = bound.get("denials", [])
    refuse(denials, provider, path, managed)
    generations = {}
    for name in bound.get("active", []):
        current = pointer(name)
        pinned = current and pin(name, current["generation"], session_directory)
        if pinned:
            generations[name] = pinned
    record["plugins"] = {"denials": denials, "generations": generations}
    return context(record["plugins"], "crew")


def status(name):
    current = pointer(name) or {}
    generation = current.get("generation")
    found = manifest(name, generation) if generation else None
    now = time.time()
    hello = _read(directory(name) / "hello.json")
    negotiated = negotiate(name, hello) if hello else None
    return {
        "name": name,
        "generation": generation,
        "server_id": current.get("server_id"),
        "synced_at": current.get("synced_at"),
        "stale": bool(found) and now - found["synced_at"] > STALE_SECONDS,
        "expired": bool(found) and now >= found["expires_at"],
        "error": (_read(directory(name) / "status.json") or {}).get("error"),
        "versions": negotiated.versions if negotiated else {},
        "mismatch": negotiated.mismatch if negotiated else None,
        "denials": [list(rule) for rule in negotiated.denials] if negotiated else [],
        "bytes": {
            role: len(section(name, generation, role, now).encode()) if generation else 0
            for role in ROLES
        },
    }


def _settings_path():
    path = Path.home() / config.SETTINGS_PATH
    if path.is_symlink():
        raise CaptainError(f"{path} is a symlink; edit [plugins] there by hand.")
    return path


def _rewrite(path, text, expected):
    """Write edited home settings only when they parse to exactly the intended table set."""
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CaptainError(
            f"Could not edit {path} safely ({exc}); edit [plugins] by hand."
        ) from exc
    if parsed != expected:
        raise CaptainError(f"Could not edit {path} safely; edit [plugins] by hand.")
    private_dir(path.parent)
    write_text(path, text)
    config.settings.cache_clear()


def add(name, command, required):
    """Register a plugin in home settings after a successful hello, then sync it once."""
    if not NAME.fullmatch(name):
        raise CaptainError(f"Plugin names match {NAME.pattern}; '{name}' does not.")
    if name in registered():
        raise CaptainError(f"Plugin {name} is already registered; remove it first.")
    resolved = shutil.which(command)
    if resolved is None:
        raise CaptainError(f"No executable at {command}.")
    resolved = str(Path(resolved).resolve())
    found = negotiate(name, checked(invoke({"command": resolved}, request("hello", {}))))
    if found.mismatch:
        raise CaptainError(f"plugin {name} not added: {found.mismatch}")
    path = _settings_path()
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    expected = tomllib.loads(before)
    if not isinstance(expected.setdefault("plugins", {}), dict):
        raise CaptainError(f"{path} sets plugins to a non-table; edit [plugins] by hand.")
    expected["plugins"][name] = {"command": resolved, "required": required}
    block = (
        f"[plugins.{name}]\ncommand = {json.dumps(resolved)}\nrequired = {str(required).lower()}\n"
    )
    joint = "" if not before else ("\n" if before.endswith("\n") else "\n\n")
    _rewrite(path, before + joint + block, expected)
    print(f"Added plugin {name} ({resolved}).")
    try:
        sync(name)
    except PluginError as exc:
        print(f"captain: plugin {name} added, first sync failed: {exc}", file=sys.stderr)


def remove(name):
    """Unregister a plugin; live crew keep what they pinned until their session is pruned."""
    if name not in registered():
        raise CaptainError(f"No plugin named {name} is registered in home settings.")
    path = _settings_path()
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.strip() == f"[plugins.{name}]")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines)
    )
    if start and not lines[start - 1].strip():
        start -= 1
    expected = tomllib.loads("".join(lines))
    del expected["plugins"][name]
    if not expected["plugins"]:
        del expected["plugins"]
    text = "".join(lines[:start] + lines[end:])
    _rewrite(path, text, expected)
    root = directory(name)
    if root.is_dir():
        try:
            with lock(root / "lock", timeout=0):
                for stale in ("current.json", "hello.json"):
                    (root / stale).unlink(missing_ok=True)
                collect(root)
        except CaptainError:
            pass  # a running sync finishes; the next collection takes what is unpinned
    print(f"Removed plugin {name}.")


def command(args):
    """The `captain plugin` verb; returns the exit status."""
    action = args.plugin_command
    if action == "add":
        add(args.name, args.executable, args.required)
        return 0
    if action == "remove":
        remove(args.name)
        return 0
    names = [args.name] if getattr(args, "name", None) else list(registered())
    if action == "sync":
        failed = 0
        for name in names:
            try:
                print(f"{name}: {sync(name) or 'no context point'}")
            except CaptainError as exc:
                print(f"captain: plugin {name}: {exc}", file=sys.stderr)
                failed = 1
        return failed
    reports = [status(name) for name in names]
    if action == "list":
        for report in reports:
            state = f"skipped: {report['mismatch']}" if report["mismatch"] else "active"
            print(f"{report['name']}  {state}  {json.dumps(report['versions'])}")
        return 0
    print(json.dumps(reports, indent=2))
    return 0
