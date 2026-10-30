"""Graph memory in four scopes: session and project outside the checkout, repo in the
committed .captain/graph.json the team shares, and the user's cross-project preferences."""

import hashlib
import json
import re
import sys
from pathlib import Path

from . import config

# Sessions launched before 0.30.0 baked `from captain_barbossa.memory import append_event`
# into their native CLI hook settings; that string outlives the upgrade that moved it.
from .events import append_event as append_event
from .runtime import CaptainError, check_text
from .sessions import prune_sessions, read_session, session
from .store import (
    USER_NAMESPACE,
    lock,
    private_dir,
    read_json,
    read_namespace,
    read_storage,
    state_root,
    state_storage,
    temp_root,
    user_storage,
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

    Long labels cost context in every `memory show`;
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


def live_nodes(links, labels):
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
        graph["nodes"] = live_nodes(graph["links"], labels)
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


def read_graph(path, what):
    """Read a graph as-is: missing is empty, and anything but a graph is refused."""
    if not path.is_file():
        return empty_graph()
    graph = read_json(path)
    if not (
        isinstance(graph, dict)
        and isinstance(graph.get("nodes"), list)
        and isinstance(graph.get("links"), list)
    ):
        raise CaptainError(f"{what} at {path} is not a graph; fix or remove it.")
    return graph


def read_repo_graph(project):
    """Read the committed graph as-is; never migrate a file the whole team shares."""
    if project is None:
        return empty_graph()
    return read_graph(repo_graph(project), "Repo memory")


def user_graph():
    """The cross-project preference graph, located without creating anything."""
    path = read_namespace(state_root(), USER_NAMESPACE) / "graph.json"
    if path.is_symlink():
        raise CaptainError(f"Memory graph must not be a symlink: {path}")
    return path


def read_user_graph():
    return read_graph(user_graph(), "User memory")


def user_rows(graph):
    """Every preference as [subject, relation, object]: the shape each launch carries."""
    return [list(row[1:]) for row in _scoped_rows(graph, "user")]


def user_match(graph, subject, relation):
    source = node_id(subject)
    return next(
        (
            link
            for link in graph["links"]
            if link["source"] == source and link.get("key") == relation
        ),
        None,
    )


def add_user_memory(subject, relation, target, supersede=False):
    """Upsert one cross-project captain preference. Explicit `--scope user` only.

    One value per subject and relation: the same triple is a no-op and another value
    needs --supersede. Every preference rides whole in every captain launch, so the
    total is capped here rather than cut there.
    """
    for text in (subject, relation, target):
        check_text(text, "memory value")
        if len(text) > LABEL_LIMIT:
            raise CaptainError(
                f"User memory values are capped at {LABEL_LIMIT} characters; "
                "record the preference, not the conversation."
            )
    edge = {
        "source": node_id(subject),
        "target": node_id(target),
        "key": relation,
        "relation": relation,
        "confidence": 1.0,
    }
    path = user_storage() / "graph.json"
    with lock(path.with_suffix(".lock")):
        graph = read_user_graph()
        existing = user_match(graph, subject, relation)
        if existing == edge:
            return False
        if existing is not None:
            if not supersede:
                raise CaptainError(
                    f"User memory already records {subject!r} {relation!r}; "
                    "pass --supersede to replace it."
                )
            graph["links"].remove(existing)
        graph["links"].append(edge)
        labels = {node["label"] for node in graph["nodes"]} | {subject, target}
        graph["nodes"] = live_nodes(graph["links"], labels)
        # Measured as the launch serialises it, so the cap is the context it costs.
        size = len(json.dumps(user_rows(graph)))
        # Half of agents.RECOVERY_CHARS, so the launch digest's cut never reaches them.
        limit = config.number("memory", "user_limit", kind=int, below=16_000)
        if size > limit:
            raise CaptainError(
                f"User memory would take {size} characters, over its {limit} limit; "
                "forget or shorten a preference, or raise [memory] user_limit."
            )
        write_json(path, graph)
    return True


def forget_user_memory(subject, relation):
    """Drop the preference recorded for subject and relation; False when there is none."""
    path = user_storage() / "graph.json"
    with lock(path.with_suffix(".lock")):
        graph = read_user_graph()
        existing = user_match(graph, subject, relation)
        if existing is None:
            return False
        graph["links"].remove(existing)
        graph["nodes"] = live_nodes(graph["links"], {node["label"] for node in graph["nodes"]})
        write_json(path, graph)
    return True


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
    for name in (scope,) if scope else ("project", "session", "repo", "user"):
        if name == "repo":
            graphs[name] = read_repo_graph(project)
            continue
        if name == "user":
            graphs[name] = read_user_graph()
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
    user_rows = _scoped_rows(graphs.get("user", empty_graph()), "user")
    if scope:
        rows = {
            "session": session_rows,
            "project": project_rows,
            "repo": repo_rows,
            "user": user_rows,
        }[scope]
        rows = rows if show_all else rows[:SHOW_LIMIT]
    else:
        if not show_all:
            # Reserve project and repo slices so a busy session can't crowd durable
            # facts off the end; session rows fill whatever the two leave unused. User
            # rows are never cut: their total is capped when written.
            project_rows = project_rows[:PROJECT_RESERVE]
            repo_rows = repo_rows[:REPO_RESERVE]
            session_rows = session_rows[: SHOW_LIMIT - len(project_rows) - len(repo_rows)]
        rows = session_rows + project_rows + repo_rows + user_rows
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
            elif scope == "user":
                directory = user_graph().parent
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
    if args.memory_command == "forget":
        forgotten = forget_user_memory(args.subject, args.relation)
        print("Forgot user memory." if forgotten else "User memory has no such preference.")
        return
    if args.memory_command == "add":
        if args.scope == "repo":
            written = add_repo_memory(
                project, args.subject, args.relation, args.target, args.because, args.supersede
            )
            print("Saved repo memory." if written else "Repo memory already records that.")
            return
        if args.because:
            raise CaptainError("--because applies only to --scope repo.")
        if args.scope == "user":
            written = add_user_memory(args.subject, args.relation, args.target, args.supersede)
            print("Saved user memory." if written else "User memory already records that.")
            return
        if args.supersede:
            raise CaptainError("--supersede applies only to --scope repo or user.")
        base = state_storage(project) if args.scope == "project" else directory
        add_memory(base / "graph.json", args.subject, args.relation, args.target)
        print(f"Saved {args.scope} memory.")
    else:
        print("Memory (subject, relation, object):")
        for scope, graph in read_scoped_graphs(directory, project).items():
            labels = {node["id"]: node["label"] for node in graph["nodes"]}
            for link in reversed(graph["links"]):
                if link["source"] not in labels or link["target"] not in labels:
                    continue
                row = [labels[link["source"]], link["relation"], labels[link["target"]]]
                if any(args.question.casefold() in value.casefold() for value in row):
                    print(f"[{scope}] " + json.dumps(row, ensure_ascii=False))
