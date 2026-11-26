"""Files attached to the user's message for the captain: validation, the recorded shape, and the
prompt a provider is sent, images natively and every other file as a path reference."""

import base64
import errno
import os
import stat
from mimetypes import guess_type
from pathlib import Path

from .runtime import CaptainError

LIMIT = 10
IMAGE_BYTES = 5 * 1024 * 1024
# Leading bytes of the image types every provider takes natively; webp is checked apart.
SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
REFERENCES_HEAD = "\n\nAttached files (local paths; open them with your tools):\n"


def _sniff(head):
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return next((mime for magic, mime in SIGNATURES if head.startswith(magic)), None)


def _open(path):
    """A descriptor of the regular file at an absolute path, never through a final symlink."""
    if "\0" in path or not Path(path).is_absolute():
        raise CaptainError(f"[attachment-missing] {path!r} is not an absolute path.")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise CaptainError(f"[attachment-unsupported] {path} is a symlink.") from exc
        raise CaptainError(f"[attachment-missing] Cannot read {path}: {exc.strerror}.") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise CaptainError(f"[attachment-unsupported] {path} is not a regular file.")
    return fd


def _record(raw):
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("path"), str)
        or any(
            raw.get(key) is not None and not isinstance(raw[key], str) for key in ("name", "mime")
        )
    ):
        raise CaptainError(
            "[attachment-unsupported] Each attachment is {path, name?, mime?} of strings."
        )
    path = raw["path"]
    with os.fdopen(_open(path), "rb") as file:
        size = os.fstat(file.fileno()).st_size
        image = _sniff(file.read(12))
    if image and size > IMAGE_BYTES:
        raise CaptainError(
            f"[attachment-too-large] {path} is {size} bytes; an image goes up to {IMAGE_BYTES}."
        )
    return {
        "path": path,
        "name": raw.get("name") or Path(path).name,
        "mime": image or raw.get("mime") or guess_type(path)[0] or "application/octet-stream",
        "kind": "image" if image else "file",
        "size": size,
    }


def validate(raw):
    """Recorded attachments, an image known by its own bytes; the first bad one refuses all."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise CaptainError("[attachment-unsupported] attachments is a list.")
    if len(raw) > LIMIT:
        raise CaptainError(f"[attachment-unsupported] At most {LIMIT} attachments per message.")
    return [_record(item) for item in raw]


def image_data(record):
    """The image's bytes while it is still that image type within the limit, else None."""
    try:
        fd = _open(record["path"])
    except CaptainError:
        return None
    with os.fdopen(fd, "rb") as file:
        data = file.read(IMAGE_BYTES + 1)
    if len(data) > IMAGE_BYTES or _sniff(data[:12]) != record["mime"]:
        return None
    return data


def encoded(data):
    return base64.b64encode(data).decode("ascii")


def prompt(text, records, budget=None):
    """(text with file references, [(record, bytes)]); an image past `budget` base64 goes by path."""
    images, files, spent = [], [], 0
    for record in records:
        data = image_data(record) if record["kind"] == "image" else None
        cost = 4 * -(-len(data) // 3) if data is not None else 0
        if data is None or (budget is not None and spent + cost > budget):
            files.append(record)
            continue
        images.append((record, data))
        spent += cost
    if not files:
        return text, images
    lines = "\n".join(
        f"- {record['path']} ({record['name']}, {record['mime']})" for record in files
    )
    return text + REFERENCES_HEAD + lines, images


def without_references(text):
    head, found, _ = text.rpartition(REFERENCES_HEAD)
    return head if found else text
