"""Project and session graph storage outside the working repository."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .runtime import CaptainError, executable, herdr


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


def _root(env_var, default):
    # CAPTAIN_STATE_ROOT/CAPTAIN_TEMP_ROOT let a launched child pin the exact roots its
    # parent resolved, so a default (no CAPTAIN_MEMORY_ROOT) parent doesn't get collapsed
    # onto one root when re-resolving XDG_STATE_HOME/tempfile defaults in the child's env.
    value = os.environ.get(env_var, os.environ.get("CAPTAIN_MEMORY_ROOT", default))
    return Path(value).expanduser().absolute()


def temp_root():
    """Ephemeral root for session data; the OS may reclaim it between reboots."""
    return _root(
        "CAPTAIN_TEMP_ROOT", str(Path(tempfile.gettempdir()) / f"captain-barbossa-{os.getuid()}")
    )


_warned_temp_state_root = False


def state_root():
    """Durable root for project-scope graph.json; survives OS temp cleanup."""
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    root = _root("CAPTAIN_STATE_ROOT", str(base / "captain-barbossa"))
    global _warned_temp_state_root
    if not _warned_temp_state_root and root.is_relative_to(Path(tempfile.gettempdir())):
        # A captain launched before the state/temp split exported one root into its
        # children, so the "durable" graph lands where the OS reclaims it.
        _warned_temp_state_root = True
        print(
            f"captain: durable memory root {root} is inside the OS temp directory; "
            "restart the captain so project memory outlives temp cleanup.",
            file=sys.stderr,
        )
    return root


def rooted_storage(root, project):
    project = project.resolve()
    if root.resolve().is_relative_to(project):
        raise CaptainError("Memory root must be outside the project repository.")
    private_dir(root)
    project_id = hashlib.sha256(os.fsencode(project)).hexdigest()
    return private_dir(root / project_id)


def storage(project):
    return rooted_storage(temp_root(), project)


def state_storage(project):
    return rooted_storage(state_root(), project)


def migrate_project_graph(project):
    """Copy a pre-split project graph from the temp root into the state root, once."""
    source = storage(project) / "graph.json"
    if not source.is_file():
        return
    destination = state_storage(project) / "graph.json"
    if source == destination or destination.exists():
        return
    with lock(destination.parent / "graph.lock"):
        if not destination.exists():
            shutil.copyfile(source, destination)
            destination.chmod(0o600)


def backfill_terminal_id(directory, pane):
    """Record the Herdr terminal ID on a captain.json written before it was tracked.

    Without it prune_sessions cannot tell a live pre-fix captain from a dead one.
    Crew run the same commands, so only the captain's own pane may claim it.
    """
    path = directory / "captain.json"
    terminal_id = pane.get("terminal_id")
    if not terminal_id or not path.is_file():
        return
    try:
        data = read_json(path)
    except CaptainError:
        return
    if not isinstance(data, dict) or data.get("terminal_id"):
        return
    if data.get("pane") == pane["pane_id"]:
        write_json(path, {**data, "terminal_id": terminal_id})


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise CaptainError(f"Cannot read memory at {path}: {exc}") from exc


def write_text(path, text):
    fd, name = tempfile.mkstemp(prefix=".captain-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path, data):
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    path.chmod(0o600)
    with path.open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        yield


def empty_graph():
    return {"directed": True, "multigraph": True, "graph": {}, "nodes": [], "links": []}


LABEL_LIMIT = 300


def truncate_label(label, maxlen=LABEL_LIMIT):
    """Truncate label to maxlen chars with ellipsis if needed."""
    if len(label) <= maxlen:
        return label
    return label[: maxlen - 3] + "..."


def note_name(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:12] + ".txt"


def store_label(path, label):
    """Cap a stored label, spilling the full text to a note file beside the graph.

    Long labels drown Graphify keyword queries and cost context in every `memory show`;
    the marker keeps the full text one file read away.
    """
    if len(label) <= LABEL_LIMIT:
        return label
    name = note_name(label)
    marker = f" see notes/{name}"
    note = private_dir(path.parent / "notes") / name
    if not note.exists():
        write_text(note, label)
    return truncate_label(label, LABEL_LIMIT - len(marker)) + marker


def node_id(label):
    """Hash the label alone, so ids stay stable across memory roots and scopes."""
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def migrate_graph(path, graph):
    """Bring an on-disk graph up to the current id/label scheme. True if it changed.

    Ids used to hash "<graph path>:<label>", so moving a root orphaned every edge and
    the same label was two unrelated nodes across scopes. Labels and relations written
    before the notes/ spill keep their full text, which has no recovery pointer.
    """
    changed = False
    rekeyed = {}
    for node in graph["nodes"]:
        label = store_label(path, node["label"])
        new_id = node_id(label)
        if label != node["label"] or new_id != node["id"]:
            changed = True
            rekeyed[node["id"]] = new_id
            node["label"], node["id"] = label, new_id
    for link in graph["links"]:
        updated = {
            "source": rekeyed.get(link["source"], link["source"]),
            "target": rekeyed.get(link["target"], link["target"]),
            "relation": store_label(path, link["relation"]),
        }
        if any(link[field] != value for field, value in updated.items()):
            changed = True
            link.update(updated, key=updated["relation"])
    return changed


def load_graph(path):
    """Read a graph, migrating it to the current id/label scheme once, under lock."""
    if not path.exists():
        return empty_graph()
    graph = read_json(path)
    if not migrate_graph(path, graph):
        return graph
    with lock(path.with_suffix(".lock")):
        graph = read_json(path)  # re-read: a writer may have won the race to the lock
        migrate_graph(path, graph)
        write_json(path, graph)
    return graph


def add_memory(path, subject, relation, target):
    for text in (subject, relation, target):
        if not text.strip() or len(text) > 8000 or "\x00" in text:
            raise CaptainError("Memory values must contain 1–8000 characters and no NUL bytes.")
    with lock(path.with_suffix(".lock")):
        graph = read_json(path) if path.exists() else empty_graph()
        migrate_graph(path, graph)
        ids = []
        for label in (store_label(path, subject), store_label(path, target)):
            new_id = node_id(label)
            ids.append(new_id)
            if not any(node["id"] == new_id for node in graph["nodes"]):
                graph["nodes"].append({"id": new_id, "label": label, "file_type": "memory"})
        relation = store_label(path, relation)
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


SESSION_ID = re.compile(r"[a-f0-9]{32}")


def session(project, pane, session_id=None, create=False):
    project = project.resolve()
    if session_id is None:
        if not create:
            raise CaptainError("Start captain first, or pass --session <id>.")
        session_id = uuid.uuid4().hex
    if not SESSION_ID.fullmatch(session_id):
        raise CaptainError("Invalid captain session ID.")
    base = storage(project)
    directory = private_dir(base / "sessions") / session_id
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
        backfill_terminal_id(directory, pane)
    try:
        migrate_project_graph(project)
    except (CaptainError, OSError):
        pass  # migration is housekeeping; never block a command on it
    return directory, meta


def project_and_session_graphs(directory):
    """Read the project-scope and session-scope graphs backing `directory` separately.

    directory is <temp_root>/<project_id>/sessions/<id>; project-scope data lives
    under the same project_id in the (possibly different) durable state root.
    """
    project_id = directory.parent.parent.name
    project_path = state_root() / project_id / "graph.json"
    session_path = directory / "graph.json"
    return load_graph(project_path), load_graph(session_path)


STALE_QUERY_SECONDS = 600


@contextmanager
def memory_snapshot(directory):
    # Clean up stray query-* directories from interrupted previous runs, but only
    # once they're old enough that no concurrent `memory query` could still own one.
    cutoff = time.time() - STALE_QUERY_SECONDS
    for item in directory.iterdir():
        if item.is_dir() and item.name.startswith("query-") and item.stat().st_mtime < cutoff:
            shutil.rmtree(item, ignore_errors=True)
    combined = empty_graph()
    seen = set()
    for graph in project_and_session_graphs(directory):
        # Ids key on the label alone, so a label shared across scopes is one node.
        combined["nodes"].extend(
            node for node in graph["nodes"] if node["id"] not in seen and not seen.add(node["id"])
        )
        combined["links"].extend(graph["links"])
    # Truncate labels in snapshot for graphify query to prevent keyword-BFS on long labels
    for node in combined["nodes"]:
        node["label"] = truncate_label(node["label"])
    # Each reader gets its own snapshot so concurrent Graphify queries cannot replace it.
    out = Path(tempfile.mkdtemp(prefix="query-", dir=directory))
    try:
        private_dir(out)
        write_json(out / "graph.json", combined)
        yield out
    finally:
        shutil.rmtree(out, ignore_errors=True)


SHOW_LIMIT = 25
PROJECT_RESERVE = 5


def _scoped_rows(graph, scope):
    labels = {node["id"]: truncate_label(node["label"]) for node in graph["nodes"]}
    return [
        (scope, labels[link["source"]], link["relation"], labels[link["target"]])
        for link in reversed(graph["links"])
        if link["source"] in labels and link["target"] in labels
    ]


def show_memory(directory, show_all):
    project_graph, session_graph = project_and_session_graphs(directory)
    session_rows = _scoped_rows(session_graph, "session")
    project_rows = _scoped_rows(project_graph, "project")
    if not show_all:
        # Reserve a project slice so a busy session can't crowd durable project
        # facts off the end; session rows fill whatever project leaves unused.
        project_rows = project_rows[:PROJECT_RESERVE]
        session_rows = session_rows[: SHOW_LIMIT - len(project_rows)]
    rows = session_rows + project_rows
    print("Memory (subject, relation, object):")
    for scope, subject, relation, target in rows:
        print(f"[{scope}] " + json.dumps([subject, relation, target], ensure_ascii=False))


PRUNE_DAYS = 7


def newest_mtime(directory):
    newest = directory.stat().st_mtime
    for path in directory.rglob("*"):
        try:
            newest = max(newest, path.lstat().st_mtime)
        except OSError:
            continue
    return newest


def live_agents():
    """Terminal IDs and agent names Herdr reports, or None when Herdr cannot be reached."""
    try:
        agents = herdr("agent", "list", timeout=10).get("agents") or []
    except (CaptainError, OSError, subprocess.TimeoutExpired):
        return None
    return (
        {agent.get("terminal_id") for agent in agents if agent.get("terminal_id")},
        {agent.get("name") for agent in agents if agent.get("name")},
    )


def session_terminal_id(directory):
    """The captain's recorded Herdr terminal ID, or None if never recorded.

    Pane IDs are position-in-layout and Herdr recycles them across terminals, so
    liveness cannot key on the pane; the terminal ID is stable for the process.
    """
    path = directory / "captain.json"
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except CaptainError:
        return None
    terminal_id = data.get("terminal_id") if isinstance(data, dict) else None
    return terminal_id if isinstance(terminal_id, str) else None


def prune_sessions(project, days=PRUNE_DAYS, current=None):
    """Remove session directories older than `days` that hold no live Herdr agent.

    Herdr being unreachable makes liveness unknowable, so the cutoff doubles rather
    than guessing. Returns the directories removed.
    """
    sessions = storage(project) / "sessions"
    if not sessions.is_dir():
        return []
    agents = live_agents()
    cutoff = time.time() - days * 86400 * (2 if agents is None else 1)
    removed = []
    for directory in sorted(sessions.iterdir()):
        if directory.is_symlink() or not directory.is_dir():
            continue
        if not SESSION_ID.fullmatch(directory.name) or directory.name == current:
            continue
        if newest_mtime(directory) >= cutoff:
            continue
        if agents is not None:
            terminal_ids, names = agents
            prefix = f"c-{directory.name[:8]}-"
            terminal_id = session_terminal_id(directory)
            if (terminal_id and terminal_id in terminal_ids) or any(
                name.startswith(prefix) for name in names
            ):
                continue
        shutil.rmtree(directory, ignore_errors=True)
        if not directory.exists():
            removed.append(directory)
    return removed


def memory(args, pane, project):
    if args.memory_command == "prune":
        removed = prune_sessions(project, args.older_than, args.session)
        if not removed:
            print(f"No session memory older than {args.older_than} days to prune.")
        else:
            noun = "directory" if len(removed) == 1 else "directories"
            print(f"Pruned {len(removed)} session {noun}:")
            for directory in removed:
                print(f"  {directory}")
        return
    directory, _ = session(project, pane, args.session)
    if args.memory_command == "add":
        base = state_storage(project) if args.scope == "project" else directory
        add_memory(base / "graph.json", args.subject, args.relation, args.target)
        print(f"Saved {args.scope} memory.")
    elif args.memory_command == "path":
        print(directory)
    else:
        with memory_snapshot(directory) as snapshot:
            if args.memory_command == "show":
                if args.json:
                    raw = (snapshot / "graph.json").read_text(encoding="utf-8")
                    graph = json.loads(raw)
                    print(
                        f"captain: dumping the whole graph: {len(graph['nodes'])} nodes, "
                        f"{len(graph['links'])} links, {len(raw.encode('utf-8'))} bytes.",
                        file=sys.stderr,
                    )
                    print(raw, end="")
                else:
                    show_memory(directory, args.all)
            else:
                env = dict(os.environ, GRAPHIFY_OUT=str(snapshot), GRAPHIFY_QUERY_LOG_DISABLE="1")
                result = subprocess.run(
                    [
                        executable("graphify"),
                        "query",
                        args.question,
                        "--graph",
                        str(snapshot / "graph.json"),
                    ],
                    cwd=snapshot,
                    env=env,
                    timeout=60,
                )
                if result.returncode:
                    raise CaptainError(f"Graphify exited with status {result.returncode}.")
