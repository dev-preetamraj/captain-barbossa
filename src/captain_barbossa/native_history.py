"""Incarnation-scoped native history ownership and conservative, retryable cleanup."""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

from . import pi_history, store, usage
from .runtime import CaptainError

_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _grok_context():
    """Detect changed native context without persisting credentials from GROK_* variables."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.startswith("GROK_") or key in {"HOME", "XDG_CONFIG_HOME"}
    }
    return hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()


def _key(value):
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise CaptainError("Invalid native-history identity.")
    return value


def _path(crew, incarnation=None):
    incarnation = incarnation or crew.record.get("incarnation_id")
    return (
        crew.session.directory.resolve()
        / "native-history"
        / (f"{_key(crew.crew_id)}-{_key(incarnation)}.json")
    )


def _plain(path):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise CaptainError("Native history path contains parent traversal.")
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise CaptainError(f"Native history path is a symlink: {part}")
    return path


def _anchor(path):
    path = _plain(path)
    while not path.exists():
        path = path.parent
    info = path.stat()
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino}


def _validate(record):
    if Path(record["native_alias"]).resolve() != Path(record["native_root"]):
        raise CaptainError("Native history root alias was replaced.")
    for anchor in record["anchors"]:
        path = _plain(anchor["path"])
        info = path.stat()
        if (info.st_dev, info.st_ino) != (anchor["device"], anchor["inode"]):
            raise CaptainError("Native history root was replaced.")
    _plain(record["native_root"])


def prepare(crew, *, incarnation, project, background=False):
    """Persist ownership before launch; return only additive native flags and env."""
    if crew.crew_id.lower() == "captain":
        raise CaptainError("Native history policy is crew-only.")
    if crew.record.get("incarnation_id") != incarnation:
        raise CaptainError("Native history incarnation differs from the roster.")
    path = _path(crew, incarnation)
    _plain(path)
    store.private_dir(path.parent)
    with store.lock(path.parent / "history.lock"):
        if path.exists():
            raise CaptainError("Native history incarnation already prepared.")
        provider = crew.record["provider"]
        session_id = str(uuid.uuid4())
        root = {
            "claude": Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")),
            "codex": Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
            "grok": Path(os.environ.get("GROK_HOME", Path.home() / ".grok")),
            "pi": path.parent / f"pi-{incarnation}",
        }.get(provider)
        if root is None:
            raise CaptainError(f"Unsupported native history provider: {provider}")
        alias = root.expanduser().absolute()
        root = _plain(alias if provider == "pi" else alias.resolve())
        argv, env = [], {}
        if provider in {"claude", "grok"}:
            argv = ["--session-id", session_id]
            if provider == "claude":
                env["CLAUDE_CONFIG_DIR"] = str(root)
            else:
                env.update(
                    {
                        key: os.environ[key]
                        for key in ("GROK_HOME", "HOME", "XDG_CONFIG_HOME")
                        if key in os.environ
                    }
                )
        elif provider == "codex":
            argv = ["-c", 'history.persistence="none"']
            env["CODEX_HOME"] = str(root)
        elif not background:
            store.private_dir(root)
            extension = path.with_suffix(".ts")
            store.write_text(
                extension, pi_history.extension(root, path.with_suffix(".pi.jsonl"), incarnation)
            )
            argv = [
                "--session-dir",
                str(root),
                "--session-id",
                session_id,
                "--extension",
                str(extension),
            ]
        record = {
            "crew_id": crew.crew_id,
            "incarnation_id": incarnation,
            "provider": provider,
            "project": str(Path(project).resolve()),
            "execution_root": str(crew.record.get("execution_root") or Path(project).resolve()),
            "worktree_id": crew.record.get("worktree_id"),
            "background": background,
            "history_policy": (
                "ephemeral"
                if background and provider == "codex"
                else "no-session"
                if background and provider == "pi"
                else "persisted"
            ),
            "native_root": str(root),
            "native_alias": str(alias),
            "anchors": [_anchor(root), _anchor(path.parent)],
            "session_ids": [] if provider == "codex" else [session_id],
            "transcripts": [],
            "deleted_ids": [],
            "state": "pending",
            "reason": "writer-running",
            "ambiguous": False,
            "observed": False,
            "events": str(path.parent.parent / "events" / f"{crew.crew_id}-{incarnation}.jsonl"),
            "binary": shutil.which(provider),
        }
        if provider == "grok":
            record["grok_context"] = _grok_context()
            record["grok_environment"] = env
        store.write_json(path, record)
    return {"argv": argv, "env": env}


def _observe(record, event):
    if event.get("incarnation_id") != record["incarnation_id"]:
        return
    provider = record["provider"]
    if event.get("provider", provider) != provider:
        return
    kind = event.get("hook_event_name", event.get("type"))
    if kind not in {"SessionStart", "session_start"}:
        return
    session_id = event.get("session_id") or event.get("thread_id")
    try:
        session_id = str(uuid.UUID(session_id))
    except (ValueError, TypeError, AttributeError):
        record["ambiguous"] = True
        return
    if session_id not in record["session_ids"]:
        # An observed resume is not proof that we created the resumed session.
        if provider != "pi" and (provider != "codex" or record["session_ids"]):
            record["ambiguous"] = True
            return
        if event.get("source") == "resume" or event.get("reason") == "resume":
            record["ambiguous"] = True
            return
        record["session_ids"].append(session_id)
    transcript = event.get("transcript_path")
    if transcript:
        if not isinstance(transcript, str) or not Path(transcript).is_absolute():
            record["ambiguous"] = True
            return
        root = Path(record["native_root"])
        transcript = Path(transcript)
        alias = Path(record["native_alias"])
        if transcript.is_relative_to(alias):
            transcript = root / transcript.relative_to(alias)
        transcript = _plain(transcript)
        valid = transcript.is_relative_to(root) and transcript.suffix == ".jsonl"
        if provider == "claude":
            valid = (
                valid
                and transcript.name == f"{session_id}.jsonl"
                and transcript.is_relative_to(root / "projects")
            )
        elif provider == "pi":
            valid = valid and transcript.parent == root and transcript.stem.endswith(session_id)
        elif provider == "codex":
            valid = (
                valid
                and transcript.is_relative_to(root / "sessions")
                and transcript.stem.endswith(session_id)
            )
        else:
            valid = False
        if not valid:
            record["ambiguous"] = True
            return
        if str(transcript) not in record["transcripts"]:
            record["transcripts"].append(str(transcript))
        for directory in (root, transcript.parent):
            anchor = _anchor(directory)
            if anchor not in record["anchors"]:
                record["anchors"].append(anchor)
    record["observed"] = True


def observe(crew, event):
    """Accept only explicit native SessionStart identity tied to this incarnation."""
    if not crew.record.get("incarnation_id"):
        return
    path = _path(crew)
    if not path.exists():
        return
    with store.lock(path.parent / "history.lock"):
        record = store.read_json(_plain(path))
        try:
            _validate(record)
            _observe(record, event)
        except (CaptainError, OSError) as exc:
            record.update(ambiguous=True, state="partial", reason=str(exc))
        store.write_json(path, record)


def _unlink(path):
    """Open every directory without following links, then unlink only a regular file."""
    path = _plain(path)
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        try:
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            raise CaptainError("Native transcript is not a regular file.")
        os.unlink(path.name, dir_fd=fd)
    finally:
        os.close(fd)


def _grok_delete_argv(record, launch):
    argv = launch["argv"]
    if (
        launch.get("state") != "exited"
        or launch.get("receipt_version") != 2
        or launch.get("native_returncode") != 0
    ):
        return None
    if len(record["session_ids"]) != 1 or not argv or argv[0] != record["binary"]:
        return None
    if record.get("grok_context") != _grok_context() or argv.count("--session-id") != 1:
        return None
    index = argv.index("--session-id")
    if argv[index + 1 : index + 2] != record["session_ids"]:
        return None
    forbidden = {"--resume", "--continue", "--fork", "-r", "-c"}
    if any(
        arg.split("=", 1)[0] in forbidden or arg.startswith("--session-id=") for arg in argv[1:]
    ):
        return None
    sockets = []
    for index, arg in enumerate(argv[1:], 1):
        if arg == "--leader-socket":
            sockets.append(argv[index + 1] if index + 1 < len(argv) else "")
        elif arg.startswith("--leader-socket="):
            sockets.append(arg.split("=", 1)[1])
    if len(sockets) > 1 or any(not socket or socket.startswith("-") for socket in sockets):
        return None
    command = [record["binary"], "sessions", "delete"]
    if sockets:
        command += ["--leader-socket", sockets[0]]
    return command + record["session_ids"]


def cleanup(crew):
    """Freeze accounting before deletion; retain incomplete manifests for retry/pruning."""
    # lifetime imports sessions, whose pruning path imports native_history.
    from . import lifetime

    if not crew.record.get("incarnation_id"):
        return {"state": "unsupported", "reason": "no-ownership-manifest"}
    path = _path(crew)
    if not path.exists():
        return {"state": "unsupported", "reason": "no-ownership-manifest"}
    with store.lock(path.parent / "history.lock"):
        record = store.read_json(_plain(path))
        if record["state"] == "complete":
            return record
        # Only the native process writes history; an escaped descendant does not block it.
        verdict = lifetime.check(
            crew.session,
            record["incarnation_id"],
            crew_id=record["crew_id"],
            incarnation_id=record["incarnation_id"],
            root=record["execution_root"],
            worktree=record["worktree_id"] or None,
            closure=False,
            scope="native",
        )
        if not verdict.ok:
            record.update(state="pending", reason=verdict.reason)
            store.write_json(path, record)
            return record
        try:
            _validate(record)
            no_history = False
            grok_command = None
            if record["provider"] == "grok":
                launch = lifetime.read(crew.session, record["incarnation_id"])
                grok_command = _grok_delete_argv(record, launch)
                if not grok_command:
                    record.update(
                        state="pending", reason="initial-native-session-provenance-unverified"
                    )
                    store.write_json(path, record)
                    return record
            policy = record.get(
                "history_policy", {"codex": "ephemeral", "pi": "no-session"}.get(record["provider"])
            )
            if record["background"] and policy in {"ephemeral", "no-session"}:
                launch = lifetime.read(crew.session, record["incarnation_id"])
                argv = launch["argv"]
                no_history = (
                    record["provider"] == "codex"
                    and policy == "ephemeral"
                    and argv[1:2] == ["exec"]
                    and "--ephemeral" in argv[2:-1]
                ) or (
                    record["provider"] == "pi"
                    and policy == "no-session"
                    and "--no-session" in argv[1:-1]
                )
                if not no_history:
                    record.update(state="pending", reason="no-history-launch-policy-unverified")
                    store.write_json(path, record)
                    return record
                record["history_policy"] = policy
            if record["provider"] == "pi":
                for event in usage.records(path.with_suffix(".pi.jsonl")):
                    _observe(record, event)
            if not record["observed"] and not no_history and not grok_command:
                record.update(state="pending", reason="native-identity-unobserved")
                store.write_json(path, record)
                return record
            for transcript in record["transcripts"]:
                _plain(transcript)
            _plain(Path(record["events"]).with_suffix(".usage.json"))
            saved_usage = usage.freeze(
                record["events"],
                record["incarnation_id"],
                provider=record["provider"],
                transcripts=record["transcripts"],
                native_root=record["native_root"],
            )
            record["accounting"] = "saved" if saved_usage is not None else "unknown"
            if record["ambiguous"]:
                record.update(state="partial", reason="ambiguous-native-identity")
            elif no_history:
                record.update(
                    state="partial" if record["transcripts"] else "complete",
                    reason="no-history-transcript-observed"
                    if record["transcripts"]
                    else "native-no-history",
                )
            elif grok_command:
                session_id = record["session_ids"][0]
                if session_id not in record["deleted_ids"]:
                    result = subprocess.run(
                        grok_command,
                        cwd=record["execution_root"],
                        env={**os.environ, **record["grok_environment"]},
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    if result.returncode:
                        record.update(state="pending", reason="native-delete-result-unconfirmed")
                        store.write_json(path, record)
                        return record
                    record["deleted_ids"].append(session_id)
                record.update(
                    state="partial",
                    reason="initial-native-delete-reported-success",
                    conversation_cleanup="native-reported-success",
                    remaining_artifacts={
                        "unverified": [
                            "changed or forked sessions",
                            "native auxiliary or cloud artifacts",
                        ]
                    },
                )
            elif record["provider"] == "codex":
                if not record["binary"]:
                    raise CaptainError("Native Codex executable unavailable.")
                for session_id in record["session_ids"]:
                    if session_id in record["deleted_ids"]:
                        continue
                    result = subprocess.run(
                        [record["binary"], "delete", session_id, "--force"],
                        env={**os.environ, "CODEX_HOME": record["native_root"]},
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    if result.returncode:
                        raise CaptainError("Native Codex deletion failed; retry required.")
                    record["deleted_ids"].append(session_id)
                    store.write_json(path, record)
                record.update(state="complete", reason="native-delete-succeeded")
            elif record["provider"] in {"claude", "pi"}:
                for transcript in record["transcripts"]:
                    _unlink(transcript)
                # No native locking contract exists for Claude's shared history or sidecars.
                record.update(state="partial", reason="shared-history-or-sidecars-unverified")
                if record["provider"] == "claude":
                    record["conversation_cleanup"] = (
                        "removed" if record["transcripts"] else "unobserved"
                    )
                    recall = Path(record["native_root"]) / "history.jsonl"
                    record["remaining_artifacts"] = {
                        "shared_prompt_recall": str(recall) if recall.exists() else None,
                        "session_sidecars": [
                            str(Path(transcript).with_suffix(""))
                            for transcript in record["transcripts"]
                            if Path(transcript).with_suffix("").exists()
                        ],
                        "unverified": ["other native session sidecars and index entries"],
                    }
                if record["provider"] == "pi":
                    remaining = list(_plain(record["native_root"]).iterdir())
                    record.update(
                        state="partial" if remaining else "complete",
                        reason="unobserved-files" if remaining else "exclusive-sessions-removed",
                    )
            else:
                record.update(
                    state="unsupported", reason="native-root-and-fork-coverage-unverified"
                )
        except subprocess.TimeoutExpired:
            record.update(state="pending", reason="native-delete-timeout-unconfirmed")
        except (CaptainError, OSError, subprocess.SubprocessError) as exc:
            record.update(state="partial", reason=str(exc))
        store.write_json(path, record)
        return record


def pending(session_directory):
    """Include every retained incarnation, not only the current roster."""
    directory = Path(session_directory).resolve() / "native-history"
    try:
        _plain(directory)
        return any(
            store.read_json(_plain(path)).get("state") != "complete"
            for path in directory.glob("*.json")
        )
    except (CaptainError, OSError, TypeError):
        return True
