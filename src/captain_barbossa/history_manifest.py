"""The native-history manifest: a bounded, validated record of one crew incarnation's native sessions."""

from pathlib import Path

from . import store
from .runtime import CaptainError

MANIFEST_BYTES = 1 << 20


def plain(path):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise CaptainError("Native history path contains parent traversal.")
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise CaptainError(f"Native history path is a symlink: {part}")
    return path


def strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def read_manifest(path):
    """Bounded regular-file read; any unexpected shape is unusable evidence."""
    record = store.read_json(plain(path), max_bytes=MANIFEST_BYTES)
    keys = ("crew_id", "incarnation_id", "provider", "project", "execution_root")
    keys += ("native_root", "native_alias", "events", "state", "reason")
    if not (
        isinstance(record, dict)
        and all(isinstance(record.get(key), str) for key in keys)
        and all(
            isinstance(record.get(key), bool) for key in ("background", "ambiguous", "observed")
        )
        and all(strings(record.get(key)) for key in ("session_ids", "transcripts", "deleted_ids"))
        and strings(record.get("usage_transcripts", []))
        and all(
            record.get(key) is None or isinstance(record[key], str)
            for key in ("worktree_id", "binary", "history_policy", "grok_context")
        )
        and isinstance(record.get("grok_environment", {}), dict)
        and all(isinstance(value, str) for value in record.get("grok_environment", {}).values())
        and isinstance(record.get("anchors"), list)
        and all(
            isinstance(anchor, dict)
            and isinstance(anchor.get("path"), str)
            and type(anchor.get("device")) is int
            and type(anchor.get("inode")) is int
            for anchor in record["anchors"]
        )
    ):
        raise CaptainError(f"Native history manifest is malformed: {path}")
    return record
