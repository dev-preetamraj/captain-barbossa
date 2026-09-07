"""Project and session graph storage outside the working repository."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from .runtime import CaptainError, executable


def project_root():
    cwd = Path.cwd().resolve()
    if os.environ.get("CAPTAIN_PROJECT"):
        project = Path(os.environ["CAPTAIN_PROJECT"]).resolve()
        if not cwd.is_relative_to(project):
            raise CaptainError("This directory is outside the active captain project.")
        return project
    if shutil.which("git"):
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip()).resolve()
    return cwd


def private_dir(path):
    if path.is_symlink():
        raise CaptainError(f"Memory directory must not be a symlink: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise CaptainError(f"Memory directory belongs to another user: {path}")
    path.chmod(0o700)
    return path


def storage(project):
    project = project.resolve()
    root = (
        Path(
            os.environ.get(
                "CAPTAIN_MEMORY_ROOT",
                str(Path(tempfile.gettempdir()) / f"captain-barbossa-{os.getuid()}"),
            )
        )
        .expanduser()
        .absolute()
    )
    if root.resolve().is_relative_to(project):
        raise CaptainError("CAPTAIN_MEMORY_ROOT must be outside the project repository.")
    private_dir(root)
    project_id = hashlib.sha256(os.fsencode(project)).hexdigest()
    return private_dir(root / project_id)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise CaptainError(f"Cannot read memory at {path}: {exc}") from exc


def write_json(path, data):
    fd, name = tempfile.mkstemp(prefix=".captain-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def lock(path):
    with path.open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        yield


def empty_graph():
    return {"directed": True, "multigraph": True, "graph": {}, "nodes": [], "links": []}


def add_memory(path, subject, relation, target):
    for text in (subject, relation, target):
        if not text.strip() or len(text) > 8000 or "\x00" in text:
            raise CaptainError("Memory values must contain 1–8000 characters and no NUL bytes.")
    with lock(path.with_suffix(".lock")):
        graph = read_json(path) if path.exists() else empty_graph()
        ids = []
        for label in (subject, target):
            node_id = hashlib.sha256(f"{path}:{label}".encode()).hexdigest()
            ids.append(node_id)
            if not any(node["id"] == node_id for node in graph["nodes"]):
                graph["nodes"].append({"id": node_id, "label": label, "file_type": "memory"})
        edge = {
            "source": ids[0],
            "target": ids[1],
            "key": relation,
            "relation": relation,
            "confidence": 1.0,
        }
        if edge not in graph["links"]:
            graph["links"].append(edge)
        # ponytail: rewrite a small session graph; move to SQLite if this becomes large.
        write_json(path, graph)


def session(project, pane, session_id=None, create=False):
    project = project.resolve()
    if session_id is None:
        if not create:
            raise CaptainError("Start captain first, or pass --session <id>.")
        session_id = uuid.uuid4().hex
    if not re.fullmatch(r"[a-f0-9]{32}", session_id):
        raise CaptainError("Invalid captain session ID.")
    base = storage(project)
    directory = base / "sessions" / session_id
    if not create and not (directory / "session.json").is_file():
        raise CaptainError("This session does not exist for the current project.")
    private_dir(directory)
    with lock(directory / "session.lock"):
        meta_path = directory / "session.json"
        if meta_path.exists():
            meta = read_json(meta_path)
            if meta["project"] != str(project) or meta["workspace"] != pane["workspace_id"]:
                raise CaptainError("This session belongs to another project or Herdr workspace.")
        else:
            meta = {
                "id": session_id,
                "project": str(project),
                "workspace": pane["workspace_id"],
                "crew": {},
            }
            write_json(meta_path, meta)
    return directory, meta


def memory_snapshot(directory):
    combined = empty_graph()
    for path in (directory.parent.parent / "graph.json", directory / "graph.json"):
        if path.exists():
            graph = read_json(path)
            combined["nodes"].extend(graph["nodes"])
            combined["links"].extend(graph["links"])
    # Each reader gets its own snapshot so concurrent Graphify queries cannot replace it.
    out = private_dir(Path(tempfile.mkdtemp(prefix="query-", dir=directory)))
    write_json(out / "graph.json", combined)
    return out


def memory(args, pane, project):
    directory, _ = session(project, pane, args.session)
    if args.memory_command == "add":
        path = (directory.parent.parent if args.scope == "project" else directory) / "graph.json"
        add_memory(path, args.subject, args.relation, args.target)
        print(f"Saved {args.scope} memory.")
    elif args.memory_command == "path":
        print(directory)
    else:
        snapshot = memory_snapshot(directory)
        try:
            if args.memory_command == "show":
                if args.json:
                    print((snapshot / "graph.json").read_text(encoding="utf-8"), end="")
                else:
                    graph = read_json(snapshot / "graph.json")
                    labels = {node["id"]: node["label"] for node in graph["nodes"]}
                    print("Memory (subject, relation, object):")
                    for link in graph["links"]:
                        print(
                            json.dumps(
                                [labels[link["source"]], link["relation"], labels[link["target"]]],
                                ensure_ascii=False,
                            )
                        )
            else:
                env = dict(os.environ, GRAPHIFY_OUT=str(snapshot), GRAPHIFY_QUERY_LOG_DISABLE="1")
                result = subprocess.run(
                    [
                        executable("graphify"),
                        "query",
                        args.question,
                        "--graph",
                        str(snapshot / "graph.json"),
                        "--budget",
                        "2000",
                    ],
                    cwd=snapshot,
                    env=env,
                    timeout=60,
                )
                if result.returncode:
                    raise CaptainError(f"Graphify exited with status {result.returncode}.")
        finally:
            shutil.rmtree(snapshot)
