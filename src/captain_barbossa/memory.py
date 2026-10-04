"""Graph memory in three scopes: session and project outside the checkout,
repo in the committed .captain/graph.json the team shares."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .runtime import CaptainError, check_text, executable
from .sessions import prune_sessions, read_session, session
from .store import (
    lock,
    private_dir,
    read_json,
    read_storage,
    state_root,
    state_storage,
    temp_root,
    write_json,
    write_text,
)


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
        check_text(text, "memory value")
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


def repo_graph(project):
    """The committed, team-shared graph inside the checkout, beside .captain/settings.toml."""
    directory = project.resolve() / ".captain"
    path = directory / "graph.json"
    if directory.is_symlink() or path.is_symlink():
        raise CaptainError(f"Repo memory must not be a symlink: {path}")
    return path


# The one place the repo vocabulary is defined: relation -> (what it records, single).
# A single-valued relation holds one object per subject, so changing it is a supersede;
# the rest are sets a subject accumulates, where only the same triple collides.
REPO_RELATIONS = {
    "convention": ("a rule the code follows", False),
    "decided": ("an architectural decision", True),
    "method": ("how the team does something", False),
}


def check_repo_fact(subject, relation, target, rationale):
    """Fix the shape of a repo fact; the model still chooses its content."""
    if relation not in REPO_RELATIONS:
        allowed = ", ".join(f"{name} ({why})" for name, (why, _) in REPO_RELATIONS.items())
        raise CaptainError(f"Repo memory relation must be one of: {allowed}.")
    if not rationale or not rationale.strip():
        raise CaptainError("Repo memory needs --because '<why this holds>'.")
    for text in (subject, relation, target, rationale):
        check_text(text, "memory value")
        if len(text) > LABEL_LIMIT:
            raise CaptainError(
                f"Repo memory values are capped at {LABEL_LIMIT} characters; "
                "record the decision, not the transcript."
            )


def repo_nodes(links, labels):
    """The endpoints of `links`, sorted by id, so the file is a pure function of the
    facts rather than of the order they were written or superseded in."""
    known = {node_id(label): label for label in labels}
    live = {link["source"] for link in links} | {link["target"] for link in links}
    return sorted(
        ({"id": key, "label": known[key], "file_type": "memory"} for key in live if key in known),
        key=lambda node: node["id"],
    )


def repo_edge(subject, relation, target, rationale):
    return {
        "source": node_id(subject),
        "target": node_id(target),
        "key": relation,
        "relation": relation,
        "rationale": rationale,
        "confidence": 1.0,
    }


def repo_match(graph, edge):
    """The link a write of `edge` would replace, or None; equal to it when already recorded."""
    single = REPO_RELATIONS[edge["relation"]][1]
    return next(
        (
            link
            for link in graph["links"]
            if link["source"] == edge["source"]
            and link.get("key") == edge["key"]
            and (single or link["target"] == edge["target"])
        ),
        None,
    )


def add_repo_memory(project, subject, relation, target, rationale, supersede=False):
    """Upsert one curated fact into the committed graph. Explicit `--scope repo` only.

    Nothing here infers, extracts or summarises: the caller states the fact. Ids hash
    the label alone and rows are sorted, so the same facts always serialise to the same
    bytes and re-adding one is a no-op rather than a duplicate row. A fact already on
    record is never overwritten by the newer write; changing it is an explicit
    --supersede. The lock stays in the state root, so none is ever committed.
    """
    check_repo_fact(subject, relation, target, rationale)
    path = repo_graph(project)
    edge = repo_edge(subject, relation, target, rationale)
    with lock(state_storage(project) / "repo-graph.lock"):
        graph = read_repo_graph(project)
        existing = repo_match(graph, edge)
        if existing == edge:
            return False
        if existing is not None:
            if not supersede:
                raise CaptainError(
                    f"Repo memory already records {subject!r} {relation!r}; "
                    "pass --supersede to replace it."
                )
            graph["links"].remove(existing)
        graph["links"].append(edge)
        graph["links"].sort(key=lambda link: (link["source"], link["key"], link["target"]))
        labels = {node["label"] for node in graph["nodes"]} | {subject, target}
        graph["nodes"] = repo_nodes(graph["links"], labels)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, graph)
    return True


# Seeding reads only these sections, so `memory init` never depends on a model deciding
# which prose is a rule. A nested section inherits its parent's scope.
RULEBOOK_FILES = ("AGENTS.md", "CLAUDE.md")


RULEBOOK_SECTIONS = ("rules", "key facts", "conventions")


ATX_HEADING = re.compile(r"^(#{1,6})\s+(\S.*?)\s*$")


LABEL_HEADING = re.compile(r"^(\S.*?):$")


BULLET = re.compile(r"^[-*]\s+(\S.*?)\s*$")


def rulebook_path(project, source=None):
    if source:
        path = Path(source).expanduser()
        path = path if path.is_absolute() else project / path
        if not path.is_file():
            raise CaptainError(f"No rulebook to read at {path}.")
        return path
    for name in RULEBOOK_FILES:
        path = project / name
        if path.is_file():
            return path
    raise CaptainError(f"No {' or '.join(RULEBOOK_FILES)} at the project root; pass --from PATH.")


def rulebook_bullets(text):
    """Top-level bullets under a rulebook section, in file order, as (heading, bullet).

    A fixed parse, never an interpretation: an ATX heading or a `Key facts:` style label
    opens a section, a section is in scope when its own title is a rulebook one or an
    enclosing section's was, fenced code is skipped, and a bullet's wrapped continuation
    lines are folded back in so no rule is cut in half.
    """
    bullets = []
    sections = []  # (level, title, in_scope)
    open_bullet = None
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced, open_bullet = not fenced, None
            continue
        if fenced:
            continue
        heading = ATX_HEADING.match(line)
        label = None if heading or BULLET.match(line) else LABEL_HEADING.match(line)
        if heading or label:
            level = len(heading.group(1)) if heading else (sections[-1][0] if sections else 0) + 1
            title = (heading or label).group(2 if heading else 1)
            while sections and sections[-1][0] >= level:
                sections.pop()
            inherited = bool(sections and sections[-1][2])
            sections.append((level, title, inherited or title.casefold() in RULEBOOK_SECTIONS))
            open_bullet = None
            continue
        bullet = BULLET.match(line)
        if bullet:
            open_bullet = None
            if sections and sections[-1][2]:
                bullets.append([sections[-1][1], bullet.group(1)])
                open_bullet = bullets[-1]
            continue
        if not line.strip():
            continue
        if open_bullet is not None and line[:1] in " \t":
            open_bullet[1] += " " + line.strip()
            continue
        open_bullet = None
    return bullets


def rulebook_facts(project, source=None):
    """The repo facts a rulebook proposes, as (edge, subject, relation, target, skipped).

    `skipped` carries why a bullet was rejected; it is never truncated to fit.
    """
    path = rulebook_path(project, source)
    try:
        origin = path.resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        origin = path.name  # a --from outside the checkout must not leak a machine path
    proposed = []
    for heading, bullet in rulebook_bullets(path.read_text(encoding="utf-8")):
        rationale = f"{origin} > {heading}"
        try:
            check_repo_fact(heading, "convention", bullet, rationale)
        except CaptainError as exc:
            proposed.append((None, heading, bullet, str(exc)))
            continue
        proposed.append(
            (repo_edge(heading, "convention", bullet, rationale), heading, bullet, None)
        )
    return path, origin, proposed


def init_repo_memory(project, source=None, apply=False):
    """Preview, or with --apply write, the repo facts the project rulebook states.

    Seeding reuses add_repo_memory, so ids, sorting, the field cap and idempotency are
    the same ones every repo write gets. It only ever adds: a bullet that would change a
    recorded fact is reported and left alone, never silently superseded.
    """
    path, origin, proposed = rulebook_facts(project, source)
    graph = read_repo_graph(project)
    added = recorded = 0
    lines = []
    for edge, heading, bullet, skipped in proposed:
        if skipped:
            # The opening words locate the bullet in the rulebook; the value itself is
            # never stored truncated, it is not stored at all.
            lines.append(f"! {heading} / {bullet[:60]}...: {skipped}")
            continue
        existing = repo_match(graph, edge)
        row = json.dumps([heading, "convention", bullet], ensure_ascii=False)
        if existing == edge:
            recorded += 1
            lines.append(f"= {row}")
        elif existing is not None:
            lines.append(f"! {heading}: already recorded with another rationale; left alone.")
        else:
            added += 1
            lines.append(f"+ {row}")
            if apply:
                add_repo_memory(project, heading, "convention", bullet, edge["rationale"])
    skipped = len(proposed) - added - recorded
    verb = "Wrote" if apply else "Would write"
    print(f"{origin}: {verb} {added} fact(s), {recorded} already recorded, {skipped} skipped.")
    for line in lines:
        print(line)
    if not apply and added:
        target = repo_graph(project).relative_to(project.resolve()).as_posix()
        print(f"Preview only. Re-run with --apply to write {target}.")


def read_repo_graph(project):
    """Read the committed graph as-is; never migrate a file the whole team shares."""
    if project is None:
        return empty_graph()
    path = repo_graph(project)
    if not path.is_file():
        return empty_graph()
    graph = read_json(path)
    if not (
        isinstance(graph, dict)
        and isinstance(graph.get("nodes"), list)
        and isinstance(graph.get("links"), list)
    ):
        raise CaptainError(f"Repo memory at {path} is not a graph; fix or remove it.")
    return graph


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
def memory_snapshot(directory, project=None):
    # Clean up stray query-* directories from interrupted previous runs, but only
    # once they're old enough that no concurrent `memory query` could still own one.
    cutoff = time.time() - STALE_QUERY_SECONDS
    for item in directory.iterdir():
        if item.is_dir() and item.name.startswith("query-") and item.stat().st_mtime < cutoff:
            shutil.rmtree(item, ignore_errors=True)
    combined = empty_graph()
    seen = set()
    graphs = [*project_and_session_graphs(directory), read_repo_graph(project)]
    for graph in graphs:
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


REPO_RESERVE = 5


def _scoped_rows(graph, scope):
    labels = {node["id"]: truncate_label(node["label"]) for node in graph["nodes"]}
    return [
        (scope, labels[link["source"]], truncate_label(link["relation"]), labels[link["target"]])
        for link in reversed(graph["links"])
        if link["source"] in labels and link["target"] in labels
    ]


def read_scoped_graphs(directory, project=None, scope=None):
    """Read only requested scopes, preserving legacy graph bytes and labels."""
    graphs = {}
    for name in (scope,) if scope else ("project", "session", "repo"):
        if name == "repo":
            graphs[name] = read_repo_graph(project)
            continue
        if name == "session":
            path = directory / "graph.json"
        elif project is not None:
            path = read_storage(state_root(), project) / "graph.json"
        else:
            path = state_root() / directory.parent.parent.name / "graph.json"
        if path.is_symlink():
            raise CaptainError(f"Memory graph must not be a symlink: {path}")
        if name == "project" and not path.exists():
            base = (
                read_storage(temp_root(), project)
                if project is not None
                else directory.parent.parent
            )
            path = base / "graph.json"
            if path.is_symlink():
                raise CaptainError(f"Memory graph must not be a symlink: {path}")
        graphs[name] = read_json(path) if path.exists() else empty_graph()
    return graphs


def show_memory(directory, show_all, project=None, scope=None):
    graphs = read_scoped_graphs(directory, project, scope)
    session_rows = _scoped_rows(graphs.get("session", empty_graph()), "session")
    project_rows = _scoped_rows(graphs.get("project", empty_graph()), "project")
    repo_rows = _scoped_rows(graphs.get("repo", empty_graph()), "repo")
    if scope:
        rows = {"session": session_rows, "project": project_rows, "repo": repo_rows}[scope]
        rows = rows if show_all else rows[:SHOW_LIMIT]
    else:
        if not show_all:
            # Reserve project and repo slices so a busy session can't crowd durable
            # facts off the end; session rows fill whatever the two leave unused.
            project_rows = project_rows[:PROJECT_RESERVE]
            repo_rows = repo_rows[:REPO_RESERVE]
            session_rows = session_rows[: SHOW_LIMIT - len(project_rows) - len(repo_rows)]
        rows = session_rows + project_rows + repo_rows
    print("Memory (subject, relation, object):")
    for row_scope, subject, relation, target in rows:
        print(f"[{row_scope}] " + json.dumps([subject, relation, target], ensure_ascii=False))


def memory(args, pane, project):
    if args.memory_command in ("show", "path"):
        scope = getattr(args, "scope", None)
        directory = None
        if scope in (None, "session"):
            directory = read_session(project, args.session, pane).directory
        if args.memory_command == "path":
            if scope == "project":
                directory = read_storage(state_root(), project)
            elif scope == "repo":
                directory = repo_graph(project).parent
            print(directory)
        elif args.json:
            graph, seen = empty_graph(), set()
            for source in read_scoped_graphs(directory, project, scope).values():
                for node in source["nodes"]:
                    if node["id"] not in seen:
                        graph["nodes"].append(node)
                        seen.add(node["id"])
                graph["links"].extend(source["links"])
            raw = json.dumps(graph, ensure_ascii=False, indent=2) + "\n"
            print(
                f"captain: dumping the whole graph: {len(graph['nodes'])} nodes, "
                f"{len(graph['links'])} links, {len(raw.encode('utf-8'))} bytes.",
                file=sys.stderr,
            )
            print(raw, end="")
        else:
            show_memory(directory, args.all, project, scope)
        return
    if args.memory_command == "init":
        init_repo_memory(project, args.source, args.apply)
        return
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
        if args.scope == "repo":
            written = add_repo_memory(
                project, args.subject, args.relation, args.target, args.because, args.supersede
            )
            print("Saved repo memory." if written else "Repo memory already records that.")
            return
        if args.because or args.supersede:
            raise CaptainError("--because and --supersede apply only to --scope repo.")
        base = state_storage(project) if args.scope == "project" else directory
        add_memory(base / "graph.json", args.subject, args.relation, args.target)
        print(f"Saved {args.scope} memory.")
    else:
        with memory_snapshot(directory, project) as snapshot:
            env = dict(os.environ, GRAPHIFY_OUT=str(snapshot), GRAPHIFY_QUERY_LOG_DISABLE="1")
            # No "--" terminator in graphify; a leading space defuses a "-"-led question.
            question = f" {args.question}" if args.question.startswith("-") else args.question
            result = subprocess.run(
                [
                    executable("graphify"),
                    "query",
                    question,
                    "--graph",
                    str(snapshot / "graph.json"),
                ],
                cwd=snapshot,
                env=env,
                timeout=60,
            )
            if result.returncode:
                raise CaptainError(f"Graphify exited with status {result.returncode}.")
