"""A read-only captain recovery digest from existing memory and protocol readers."""

from . import lifetime, memory, native_history, protocol, results, worktrees
from .crew import Crew

LIMIT = 25


def digest(current, project):
    result = {
        "session_id": current.meta["id"],
        "memory": {},
        "assignments": [],
        "owned_paths": [],
        "questions": [],
        "deliveries": [],
        "mail": [],
        "omitted": {},
    }
    result["lifetimes"] = [
        {
            key: record[key]
            for key in ("id", "crew_id", "incarnation_id", "root", "state", "revoked")
        }
        for path in sorted((current.directory / "lifetimes").glob("*.json"))
        for record in (lifetime.read(current, path.stem),)
    ]
    result["native_history_pending"] = native_history.pending(current.directory)
    result["worktrees"] = worktrees.inventory(current)
    result["results"] = [
        {
            key: manifest[key]
            for key in (
                "id",
                "worktree_id",
                "crew_id",
                "incarnation_id",
                "assignment_id",
                "provisional",
                "unresolved",
                "inventory_hash",
            )
        }
        for manifest in results.inventory(current)
    ]
    for scope, graph in memory.read_scoped_graphs(current.directory, project).items():
        labels = {node["id"]: node["label"] for node in graph["nodes"]}
        rows = [
            {
                **link,
                "source": labels.get(link["source"], link["source"]),
                "target": labels.get(link["target"], link["target"]),
            }
            for link in reversed(graph["links"])
        ]
        result["memory"][scope] = rows[:LIMIT]
        result["omitted"][f"memory_{scope}"] = max(0, len(rows) - LIMIT)
    for crew in sorted(Crew.members(current), key=lambda crew: crew.crew_id):
        if crew.is_dismissed:
            continue
        assignment = protocol.active_assignment(crew)
        if not assignment or assignment["incarnation_id"] != crew.record.get("incarnation_id"):
            continue
        identity = {"crew": crew.display_name, "assignment_id": assignment["id"]}
        if assignment["state"] != "done":
            result["assignments"].append(
                {
                    **identity,
                    "incarnation_id": assignment["incarnation_id"],
                    "state": assignment["state"],
                    "task": assignment["original_task"],
                    "actions": assignment["actions"],
                    "messages": assignment["messages"],
                }
            )
            result["owned_paths"].extend({**identity, "path": path} for path in assignment["paths"])
        if assignment["question"]:
            result["questions"].append({**identity, **assignment["question"]})
        pending = assignment["pending"]
        if pending:
            result["deliveries"].append(pending["result"])
        start = pending["seq"] if pending else assignment["ack_seq"]
        result["deliveries"].extend(
            {**identity, "delivery_id": None, **notice} for notice in assignment["notices"][start:]
        )
        result["mail"].extend({**identity, **message} for message in protocol.unread(crew))
    for section in (
        "assignments",
        "owned_paths",
        "questions",
        "deliveries",
        "mail",
        "worktrees",
        "results",
        "lifetimes",
    ):
        result["omitted"][section] = max(0, len(result[section]) - LIMIT)
        result[section] = result[section][:LIMIT]
    return result
