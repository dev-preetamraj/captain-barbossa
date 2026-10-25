"""Immutable crew output and explicit, unstaged transfer with durable evidence."""

import base64
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import store, worktrees
from .runtime import CaptainError


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _payload(data, mode):
    return {
        "mode": mode,
        "content": base64.b64encode(data).decode(),
        "hash": hashlib.sha256(data).hexdigest(),
    }


def _relative(value):
    path = Path(value)
    if (
        not value
        or value == "."
        or path.is_absolute()
        or ".." in path.parts
        or ".git" in path.parts
        or "\0" in value
        or path.as_posix() != value
    ):
        raise CaptainError("Unsafe result path.")
    return path


@contextmanager
def _parent(root, value, *, create=False):
    path = _relative(value)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o777, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except NotADirectoryError:
                mode = os.stat(part, dir_fd=fd, follow_symlinks=False).st_mode
                if not create and stat.S_ISREG(mode):
                    raise FileNotFoundError(value) from None
                raise
            os.close(fd)
            fd = next_fd
        yield fd, path.name
    finally:
        os.close(fd)


def _at(parent, name, *, directory=False):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return None
    if directory and stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CaptainError(f"Unsupported or hardlinked result file: {name}")
        return _payload(file.read(), "100755" if info.st_mode & 0o111 else "100644")


def _file(root, value, *, directory=False):
    try:
        with _parent(root, value) as (parent, name):
            return _at(parent, name, directory=directory)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CaptainError(f"Unsafe or unreadable result path: {value}") from exc


def _tree(root, oid, *paths, error=None):
    result = {}
    for row in worktrees.git(root, "ls-tree", "-rz", oid, "--", *paths).split(b"\0"):
        if not row:
            continue
        meta, name = row.split(b"\t", 1)
        mode, kind, blob = meta.decode().split()
        path = os.fsdecode(name)
        _relative(path)
        if kind != "blob" or mode not in ("100644", "100755"):
            raise CaptainError(
                error
                or "Results containing symlinks or submodules are unsupported; retain checkout."
            )
        result[path] = _payload(worktrees.git(root, "cat-file", "blob", blob), mode)
    return result


def _names(root, *args):
    return [
        os.fsdecode(name)
        for name in worktrees.git(root, "ls-files", "-z", *args).split(b"\0")
        if name
    ]


def _snapshot(record, owned_paths):
    root = Path(record["execution_root"])
    base = _tree(root, record["base_oid"])
    head = worktrees.git(root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    head_tree = _tree(root, head)
    tracked = set(_names(root, "--cached")) | set(base) | set(head_tree)
    untracked = set(_names(root, "--others", "--exclude-standard"))
    ignored = set(_names(root, "--others", "--ignored", "--exclude-standard"))
    discovered, directories = set(), []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                if directory == root and child.name == ".git":
                    continue
                name = Path(child.path).relative_to(root).as_posix()
                if child.is_dir(follow_symlinks=False):
                    directories.append(name)
                    pending.append(Path(child.path))
                else:
                    discovered.add(name)
    entries, unresolved, observed = [], [], {}
    for name in sorted(tracked | untracked | ignored | discovered):
        try:
            post = _file(root, name, directory=name in directories)
        except (CaptainError, OSError) as exc:
            unresolved.append({"path": name, "reason": str(exc)})
            continue
        observed[name] = post
        if name in ignored:
            unresolved.append({"path": name, "reason": "ignored"})
        elif name not in tracked | untracked:
            unresolved.append({"path": name, "reason": "unknown"})
        elif base.get(name) != post:
            if not any(
                p == "." or name == p or name.startswith(p.rstrip("/") + "/") for p in owned_paths
            ):
                unresolved.append({"path": name, "reason": "out-of-grant"})
            entries.append({"path": name, "base": base.get(name), "post": post})
    # Include index-only work so cleanup cannot silently discard a staged version.
    index = worktrees.git(root, "ls-files", "--stage", "-z")
    status = worktrees.git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    return {
        "head_oid": head,
        "entries": entries,
        "unresolved": unresolved,
        "inventory_hash": _hash(
            {
                "files": observed,
                "directories": sorted(directories),
                "head": head,
                "index": base64.b64encode(index).decode(),
                "status": base64.b64encode(status).decode(),
                "unresolved": unresolved,
            }
        ),
        "index": base64.b64encode(index).decode(),
        "head_files": head_tree,
    }


def _directory(session, result_id):
    return worktrees.plain_path(session.directory / "results" / worktrees.validate_id(result_id))


def read(session, result_id):
    try:
        return _read(session, result_id)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise CaptainError("Malformed result artifact; retain for manual recovery.") from exc


def _read(session, result_id):
    manifest = store.read_json(
        worktrees.plain_path(_directory(session, result_id) / "manifest.json")
    )
    if (
        manifest.get("id") != result_id
        or manifest.get("session_id") != session.meta["id"]
        or manifest.get("project") != session.meta["project"]
    ):
        raise CaptainError("Result identity mismatch.")
    digest = manifest.get("manifest_hash")
    if digest != _hash({key: value for key, value in manifest.items() if key != "manifest_hash"}):
        raise CaptainError("Result manifest hash mismatch.")
    record = worktrees.read(session, manifest["worktree_id"], validate=False)
    for key in ("incarnation_id", "assignment_id", "crew_id", "base_oid", "execution_root"):
        if manifest.get(key) != record[key]:
            raise CaptainError("Result source identity mismatch.")
    for entry in manifest["entries"]:
        _relative(entry["path"])
        for payload in (entry["base"], entry["post"]):
            if payload is not None:
                data = base64.b64decode(payload["content"], validate=True)
                if payload != _payload(data, payload["mode"]) or payload["mode"] not in (
                    "100644",
                    "100755",
                ):
                    raise CaptainError("Invalid result payload.")
    return manifest


def inventory(session):
    directory = worktrees.plain_path(session.directory / "results")
    return [read(session, path.name) for path in sorted(directory.glob("*")) if path.is_dir()]


def collect(session, worktree_id, owned_paths, report=""):
    with store.lock(session.directory / "worktrees.lock"):
        record = worktrees.read(session, worktree_id)
        paths = []
        for value in owned_paths:
            path = Path(value)
            if path.is_absolute():
                try:
                    value = path.relative_to(record["execution_root"]).as_posix()
                except ValueError as exc:
                    raise CaptainError("Owned path is outside the worktree.") from exc
            if value != ".":
                _relative(value)
            paths.append(value)
        snapshot = _snapshot(record, paths)
        # A second full scan detects ordinary concurrent edits, but is not liveness evidence.
        if snapshot != _snapshot(record, paths):
            raise CaptainError("Worktree changed during collection; retry after writers stop.")
        result_id = uuid.uuid4().hex
        manifest = {
            key: record[key]
            for key in (
                "project",
                "session_id",
                "crew_id",
                "incarnation_id",
                "assignment_id",
                "execution_root",
                "base_oid",
            )
        }
        manifest.update(
            schema=1,
            id=result_id,
            worktree_id=worktree_id,
            report=report,
            provisional=not worktrees.stopped(session, record),
            owned_paths=paths,
            **snapshot,
        )
        # Preserve staged blobs even when the working file has a different postimage.
        manifest["index_blobs"] = {}
        for row in base64.b64decode(snapshot["index"]).split(b"\0"):
            if row:
                oid = row.split(b"\t", 1)[0].split()[1].decode()
                manifest["index_blobs"][oid] = base64.b64encode(
                    worktrees.git(record["execution_root"], "cat-file", "blob", oid)
                ).decode()
        if snapshot != _snapshot(record, paths):
            raise CaptainError("Worktree changed during collection; no artifact published.")
        manifest["manifest_hash"] = _hash(manifest)
        directory = _directory(session, result_id)
        store.private_dir(directory.parent)
        directory.mkdir(mode=0o700)
        worktrees.durable(directory / "manifest.json", manifest)
        record.update(result_id=result_id, phase="retained")
        worktrees.durable(worktrees.record_path(session, worktree_id), record)
        return manifest


def _destination(manifest):
    root = worktrees.plain_path(manifest["project"])
    if str(root) != manifest["project"]:
        raise CaptainError("Transfer destination must be the recorded captain checkout.")
    common = worktrees.validate_repository(root)
    for name in (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "rebase-merge",
        "rebase-apply",
        "BISECT_LOG",
        "index.lock",
    ):
        if (common / name).exists():
            raise CaptainError("Destination has an unfinished Git operation.")
    return root


def _index(root, paths):
    result = {}
    for row in worktrees.git(root, "ls-files", "--stage", "-z", "--", *paths).split(b"\0"):
        if not row:
            continue
        meta, name = row.split(b"\t", 1)
        mode, oid, stage = meta.decode().split()
        if stage != "0" or mode not in ("100644", "100755"):
            raise CaptainError("Destination index has unsupported or unresolved entries.")
        result[os.fsdecode(name)] = _payload(worktrees.git(root, "cat-file", "blob", oid), mode)
    return result


def preview(session, result_id):
    manifest = read(session, result_id)
    root = _destination(manifest)
    journal = _directory(session, result_id) / "transfer.json"
    transfer = store.read_json(worktrees.plain_path(journal)) if journal.exists() else None
    states = set()
    for entry in manifest["entries"]:
        current = _file(root, entry["path"], directory=entry["post"] is None)
        states.add(
            "post"
            if current == entry["post"]
            else "base"
            if current == entry["base"]
            else "changed"
        )
    observed = (
        "externally-modified"
        if "changed" in states
        else "partial"
        if len(states) > 1
        else "untouched"
        if states == {"base"}
        else "applied"
    )
    return {
        **manifest,
        "destination_root": str(root),
        "transfer": transfer,
        "observed_state": observed,
    }


def _committed(root, paths):
    return _tree(
        root,
        "HEAD",
        *paths,
        error="[transfer-unsupported] Destination HEAD has a symlink or submodule.",
    )


def _write(root, value, expected, payload):
    """Replace one file atomically, rechecking its preimage right before the swap."""
    with _parent(root, value, create=payload is not None) as (fd, name):
        if _at(fd, name) != expected:
            raise CaptainError(f"[transfer-overlap] Destination changed during transfer: {value}")
        if payload is None:
            os.unlink(name, dir_fd=fd)
        else:
            temp = f".{name}.captain-{uuid.uuid4().hex}"
            mode = 0o755 if payload["mode"] == "100755" else 0o644
            out = os.open(
                temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd
            )
            try:
                with os.fdopen(out, "wb") as file:
                    file.write(base64.b64decode(payload["content"]))
                    file.flush()
                    os.fsync(file.fileno())
                if _at(fd, name) != expected:
                    raise CaptainError(
                        f"[transfer-overlap] Destination changed during transfer: {value}"
                    )
                os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
            except BaseException:
                try:
                    os.unlink(temp, dir_fd=fd)
                except FileNotFoundError:
                    pass
                raise
        os.fsync(fd)


def apply(session, result_id):
    """Write recorded postimages into the captain checkout, unstaged, or refuse untouched."""
    manifest = read(session, result_id)
    root = _destination(manifest)
    if manifest["unresolved"]:
        raise CaptainError("[transfer-unresolved] Result has unresolved content; retain it.")
    entries = manifest["entries"]
    paths = [entry["path"] for entry in entries]
    with store.lock(session.directory.parent.parent / "result-transfer.lock"):
        directory = _directory(session, result_id)
        if (directory / "discard.json").exists():
            raise CaptainError("Discarded result cannot be applied.")
        journal = directory / "transfer.json"
        receipt = None
        if journal.exists():
            receipt = store.read_json(worktrees.plain_path(journal))
            if receipt.get("manifest_hash") != manifest["manifest_hash"] or receipt.get(
                "destination_root"
            ) != str(root):
                raise CaptainError("Transfer receipt identity mismatch.")
        try:
            current = {
                entry["path"]: _file(root, entry["path"], directory=entry["post"] is None)
                for entry in entries
            }
        except (CaptainError, OSError) as exc:
            raise CaptainError(f"[transfer-uncertain] {exc}; nothing applied.") from exc
        at_post = all(current[entry["path"]] == entry["post"] for entry in entries)
        if receipt is not None and receipt.get("state") == "applied":
            if at_post:
                return receipt
            raise CaptainError("[transfer-overlap] Applied files changed since; retain result.")
        if receipt is not None and not at_post:
            raise CaptainError(
                f"[transfer-partial] Earlier transfer ended {receipt.get('state')}; "
                f"recover manually from {directory / 'manifest.json'}."
            )
        index = _index(root, paths) if paths else {}
        committed = _committed(root, paths) if paths else {}
        staged = [path for path in paths if index.get(path) != committed.get(path)]
        if staged:
            raise CaptainError(
                f"[transfer-overlap] Staged changes on touched paths: {', '.join(staged)}; "
                "nothing applied."
            )
        receipt = {
            "schema": 1,
            "result_id": result_id,
            "manifest_hash": manifest["manifest_hash"],
            "destination_root": str(root),
            "entries": entries,
        }
        if at_post:
            # Already integrated by hand; nothing to write.
            receipt["state"] = "applied"
            worktrees.durable(journal, receipt)
            return receipt
        changed = [e["path"] for e in entries if current[e["path"]] != e["base"]]
        if changed or any(index.get(e["path"]) != e["base"] for e in entries):
            raise CaptainError(
                "[transfer-overlap] Destination differs from the result base: "
                f"{', '.join(changed) or 'index'}; nothing applied."
            )
        receipt["state"] = "applying"
        worktrees.durable(journal, receipt)
        written = []
        try:
            # Deletions first, so a removed file can become a directory.
            for entry in sorted(entries, key=lambda e: e["post"] is not None):
                _write(root, entry["path"], entry["base"], entry["post"])
                written.append(entry["path"])
        except (CaptainError, OSError) as exc:
            receipt.update(state="partial", written=written, error=str(exc))
            worktrees.durable(journal, receipt)
            raise CaptainError(
                f"[transfer-partial] {exc}; wrote {len(written)} of {len(entries)} files. "
                f"Recover manually from {directory / 'manifest.json'}."
            ) from exc
        if any(
            _file(root, entry["path"], directory=entry["post"] is None) != entry["post"]
            for entry in entries
        ):
            receipt.update(state="partial", written=written, error="changed after write")
            worktrees.durable(journal, receipt)
            raise CaptainError("[transfer-partial] Destination changed after write; retain result.")
        receipt["state"] = "applied"
        worktrees.durable(journal, receipt)
        return receipt


def discard(session, result_id, reason):
    if not isinstance(reason, str) or not reason.strip():
        raise CaptainError("Explicit discard requires a reason.")
    manifest = read(session, result_id)
    with store.lock(session.directory.parent.parent / "result-transfer.lock"):
        path = _directory(session, result_id) / "discard.json"
        if path.exists():
            return store.read_json(worktrees.plain_path(path))
        receipt = {
            "result_id": result_id,
            "manifest_hash": manifest["manifest_hash"],
            "state": "discarded",
            "reason": reason,
        }
        worktrees.durable(path, receipt)
        return receipt


def cleanup_ready(session, record):
    if not record["result_id"]:
        raise CaptainError("No final result; retain worktree.")
    manifest = read(session, record["result_id"])
    if manifest["provisional"] or manifest["unresolved"]:
        raise CaptainError("Result is provisional or unresolved; retain worktree.")
    if _snapshot(record, manifest["owned_paths"])["inventory_hash"] != manifest["inventory_hash"]:
        raise CaptainError("Worktree changed after collection; retain worktree.")
    directory = _directory(session, manifest["id"])
    discard_path = directory / "discard.json"
    if discard_path.exists():
        receipt = store.read_json(worktrees.plain_path(discard_path))
        if (
            receipt.get("manifest_hash") == manifest["manifest_hash"]
            and receipt.get("state") == "discarded"
        ):
            return
    if manifest["head_oid"] != manifest["base_oid"]:
        raise CaptainError("Detached commits remain; explicit discard required for cleanup.")
    root = Path(record["execution_root"])
    index = _index(root, ["."])
    base = _tree(root, record["base_oid"])
    for name in index.keys() | base.keys():
        if index.get(name) != base.get(name) and _file(
            root, name, directory=index.get(name) is None
        ) != index.get(name):
            raise CaptainError(
                "Distinct staged content remains; explicit discard required for cleanup."
            )
    if not manifest["entries"]:
        return
    journal = directory / "transfer.json"
    if journal.exists():
        receipt = store.read_json(worktrees.plain_path(journal))
        if (
            receipt.get("state") == "applied"
            and receipt.get("manifest_hash") == manifest["manifest_hash"]
            and receipt.get("destination_root") == manifest["project"]
            and all(
                _file(Path(manifest["project"]), entry["path"], directory=entry["post"] is None)
                == entry["post"]
                for entry in manifest["entries"]
            )
        ):
            return
    raise CaptainError("Result has no verified integration or discard; retain worktree.")
