"""Regression tests for the committed `--scope repo` graph in .captain/graph.json."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import cli, instructions, memory
from captain_barbossa.runtime import CaptainError


class RepoMemoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "CAPTAIN_MEMORY_ROOT": str(self.root / "state"),
                    "CAPTAIN_PROJECT": str(self.project),
                },
            )
        )
        self.pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}
        self.directory, self.meta = memory.session(self.project, self.pane, create=True)
        self.repo_graph = self.project / ".captain" / "graph.json"

    def run_memory(self, *args):
        parsed = cli.parser().parse_args(["--session", self.meta["id"], "memory", *args])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            memory.memory(parsed, self.pane, self.project)
        return output.getvalue().splitlines()

    def add_repo(self, subject, relation, target, because="it keeps panes even", *extra):
        return self.run_memory(
            "add", subject, relation, target, "--scope", "repo", "--because", because, *extra
        )

    def graph(self):
        return json.loads(self.repo_graph.read_text(encoding="utf-8"))

    # --- shape: explicit writes, closed vocabulary, mandatory rationale ---

    def test_add_scope_repo_writes_the_graph_inside_the_checkout(self):
        self.assertEqual(
            self.add_repo("placement", "decided", "declared tab shapes", "ratios must be even"),
            ["Saved repo memory."],
        )
        graph = self.graph()
        self.assertEqual(
            {node["label"] for node in graph["nodes"]}, {"placement", "declared tab shapes"}
        )
        self.assertEqual(len(graph["links"]), 1)
        self.assertEqual(graph["links"][0]["relation"], "decided")
        self.assertEqual(graph["links"][0]["rationale"], "ratios must be even")

    def test_a_relation_outside_the_vocabulary_is_refused_with_the_allowed_set(self):
        with self.assertRaises(CaptainError) as caught:
            self.add_repo("Jack", "report", "gate green")
        message = str(caught.exception)
        for relation in memory.REPO_RELATIONS:
            self.assertIn(relation, message)
        self.assertFalse(self.repo_graph.exists())

    def test_every_vocabulary_relation_is_accepted(self):
        for relation in memory.REPO_RELATIONS:
            with self.subTest(relation=relation):
                self.add_repo(f"subject {relation}", relation, "object", "a reason")
        self.assertEqual(len(self.graph()["links"]), len(memory.REPO_RELATIONS))

    def test_the_rationale_is_required(self):
        parsed = cli.parser().parse_args(
            ["--session", self.meta["id"], "memory", "add", "a", "decided", "b", "--scope", "repo"]
        )
        with self.assertRaises(CaptainError) as caught:
            memory.memory(parsed, self.pane, self.project)
        self.assertIn("--because", str(caught.exception))
        self.assertFalse(self.repo_graph.exists())

    def test_because_and_supersede_are_rejected_outside_repo_scope(self):
        for extra in (["--because", "why"], ["--supersede"]):
            with self.subTest(extra=extra):
                with self.assertRaises(CaptainError) as caught:
                    self.run_memory("add", "a", "decided", "b", *extra)
                self.assertIn("only to --scope repo", str(caught.exception))

    def test_session_and_project_scopes_never_touch_the_checkout(self):
        self.run_memory("add", "Jack", "report", "gate green")
        self.run_memory("add", "test command", "is", "unittest", "--scope", "project")
        self.assertFalse((self.project / ".captain").exists())

    def test_the_lock_file_stays_out_of_the_checkout(self):
        self.add_repo("locks", "convention", "live outside the checkout")
        self.assertEqual(
            sorted(path.name for path in (self.project / ".captain").iterdir()), ["graph.json"]
        )
        self.assertTrue((memory.state_storage(self.project) / "repo-graph.lock").is_file())

    def test_a_value_over_the_field_limit_is_refused_and_writes_nothing(self):
        with self.assertRaises(CaptainError) as caught:
            self.add_repo("Jack", "method", "x" * (memory.LABEL_LIMIT + 1))
        self.assertIn("capped at", str(caught.exception))
        self.assertFalse(self.repo_graph.exists())

    def test_a_value_at_the_field_limit_is_stored_whole_without_a_notes_spill(self):
        target = "y" * memory.LABEL_LIMIT
        self.add_repo("convention", "convention", target)
        self.assertIn(target, {node["label"] for node in self.graph()["nodes"]})
        self.assertFalse((self.project / ".captain" / "notes").exists())

    def test_a_symlinked_graph_or_directory_is_refused(self):
        (self.project / ".captain").mkdir()
        (self.project / ".captain" / "graph.json").symlink_to(self.root / "elsewhere.json")
        with self.assertRaises(CaptainError):
            self.add_repo("symlink", "decided", "refused")

    # --- requirement 3: deterministic bytes, idempotent upsert ---

    def test_the_same_fact_twice_is_a_no_op_with_byte_identical_output(self):
        self.add_repo("placement", "decided", "declared tab shapes", "even ratios")
        first = self.repo_graph.read_bytes()

        self.assertEqual(
            self.add_repo("placement", "decided", "declared tab shapes", "even ratios"),
            ["Repo memory already records that."],
        )

        self.assertEqual(self.repo_graph.read_bytes(), first)
        self.assertEqual(len(self.graph()["links"]), 1)

    def test_the_same_facts_in_any_order_serialise_to_the_same_bytes(self):
        facts = [
            ("placement", "decided", "declared tab shapes", "even ratios"),
            ("memory", "convention", "three scopes", "durability differs"),
            ("gate", "method", "pre-commit --hook-stage pre-push", "one command"),
        ]
        for fact in facts:
            self.add_repo(*fact)
        forward = self.repo_graph.read_bytes()

        self.repo_graph.unlink()
        for fact in reversed(facts):
            self.add_repo(*fact)

        self.assertEqual(self.repo_graph.read_bytes(), forward)

    def test_ids_hash_the_content_and_carry_no_timestamp_or_machine_path(self):
        self.add_repo("placement", "decided", "declared tab shapes", "even ratios")
        graph = self.graph()

        by_label = {node["label"]: node["id"] for node in graph["nodes"]}
        self.assertEqual(by_label["placement"], memory.node_id("placement"))
        self.assertEqual(by_label["declared tab shapes"], memory.node_id("declared tab shapes"))

        raw = self.repo_graph.read_text(encoding="utf-8")
        for absent in (str(self.root), str(self.project), str(Path.home()), self.meta["id"]):
            self.assertNotIn(absent, raw)
        self.assertNotIn("time", raw)
        self.assertNotIn("date", raw)

    def test_rows_are_sorted_so_the_file_never_reorders_under_a_teammate(self):
        for subject in ("zeta", "alpha", "mu"):
            self.add_repo(subject, "convention", f"{subject} rule", "a reason")
        graph = self.graph()

        self.assertEqual(
            [node["id"] for node in graph["nodes"]], sorted(node["id"] for node in graph["nodes"])
        )
        keys = [(link["source"], link["key"], link["target"]) for link in graph["links"]]
        self.assertEqual(keys, sorted(keys))

    # --- requirement 4: updates are an explicit supersede ---

    def test_changing_a_recorded_fact_is_refused_without_supersede(self):
        self.add_repo("placement", "decided", "declared tab shapes", "even ratios")
        before = self.repo_graph.read_bytes()

        with self.assertRaises(CaptainError) as caught:
            self.add_repo("placement", "decided", "free-form splits", "even ratios")

        self.assertIn("--supersede", str(caught.exception))
        self.assertEqual(self.repo_graph.read_bytes(), before)

    def test_changing_only_the_rationale_is_also_refused_without_supersede(self):
        self.add_repo("placement", "decided", "declared tab shapes", "even ratios")
        with self.assertRaises(CaptainError):
            self.add_repo("placement", "decided", "declared tab shapes", "a different reason")

    def test_supersede_replaces_the_row_instead_of_adding_a_second_one(self):
        self.add_repo("placement", "decided", "declared tab shapes", "even ratios")
        self.add_repo("placement", "decided", "free-form splits", "ratios drifted", "--supersede")

        graph = self.graph()
        self.assertEqual(len(graph["links"]), 1)
        self.assertEqual(graph["links"][0]["rationale"], "ratios drifted")
        labels = {node["label"] for node in graph["nodes"]}
        self.assertEqual(labels, {"placement", "free-form splits"})
        self.assertNotIn("declared tab shapes", labels)

    def test_a_superseded_fact_leaves_the_same_bytes_as_recording_it_first(self):
        self.add_repo("placement", "decided", "declared tab shapes", "even ratios")
        self.add_repo("placement", "decided", "free-form splits", "ratios drifted", "--supersede")
        superseded = self.repo_graph.read_bytes()

        self.repo_graph.unlink()
        self.add_repo("placement", "decided", "free-form splits", "ratios drifted")

        self.assertEqual(self.repo_graph.read_bytes(), superseded)

    def test_a_different_relation_on_the_same_subject_is_not_a_supersede(self):
        self.add_repo("placement", "decided", "declared tab shapes", "even ratios")
        self.add_repo("placement", "method", "layout.py declares slots", "one place")
        self.assertEqual(len(self.graph()["links"]), 2)

    def test_a_subject_accumulates_the_relations_declared_as_sets(self):
        for relation, single in memory.REPO_RELATIONS.items():
            self.assertIs(single[1], relation == "decided")
        for i in range(3):
            self.add_repo("Rules", "convention", f"rule {i}", "AGENTS.md > Rules")
        self.assertEqual(len(self.graph()["links"]), 3)

    def test_a_set_relation_still_refuses_to_restate_a_fact_with_a_new_rationale(self):
        self.add_repo("Rules", "convention", "rule one", "AGENTS.md > Rules")
        with self.assertRaises(CaptainError) as caught:
            self.add_repo("Rules", "convention", "rule one", "CLAUDE.md > Rules")
        self.assertIn("--supersede", str(caught.exception))
        self.assertEqual(len(self.graph()["links"]), 1)

    # --- reads ---

    def test_reading_never_rewrites_the_committed_file(self):
        self.add_repo("memory", "convention", "three scopes", "durability differs")
        # An id scheme migration would rekey nodes; a shared committed file must not move
        # under a teammate whose only action was reading it.
        stale = self.graph()
        stale["nodes"][0]["id"] = "legacy-id"
        memory.write_json(self.repo_graph, stale)
        before = self.repo_graph.read_text(encoding="utf-8")

        self.run_memory("show")
        with memory.memory_snapshot(self.directory, self.project):
            pass

        self.assertEqual(self.repo_graph.read_text(encoding="utf-8"), before)

    def test_a_malformed_committed_graph_says_what_to_fix(self):
        (self.project / ".captain").mkdir()
        memory.write_text(self.repo_graph, '{"nodes": "not a list"}')
        with self.assertRaises(CaptainError) as caught:
            self.run_memory("show")
        self.assertIn("is not a graph", str(caught.exception))

    def test_show_labels_repo_rows_and_reserves_room_for_them(self):
        for i in range(10):
            self.add_repo(f"convention {i}", "convention", f"r{i}", "a reason")
        for i in range(30):
            memory.add_memory(self.directory / "graph.json", "session", "has", f"s{i}")

        rows = self.run_memory("show")[1:]
        self.assertEqual(len(rows), memory.SHOW_LIMIT)
        self.assertEqual(
            len([row for row in rows if row.startswith("[repo] ")]), memory.REPO_RESERVE
        )

    def test_show_scope_repo_hides_session_and_project_rows(self):
        self.add_repo("memory", "convention", "three scopes", "durability differs")
        memory.add_memory(self.directory / "graph.json", "Jack", "report", "done")
        memory.add_memory(
            memory.state_storage(self.project) / "graph.json", "test command", "is", "unittest"
        )

        rows = self.run_memory("show", "--scope", "repo")[1:]
        self.assertEqual(rows, ['[repo] ["memory", "convention", "three scopes"]'])

    def test_query_snapshot_carries_repo_facts_alongside_the_other_scopes(self):
        self.add_repo("memory", "convention", "three scopes", "durability differs")
        memory.add_memory(self.directory / "graph.json", "Jack", "report", "done")
        with memory.memory_snapshot(self.directory, self.project) as snapshot:
            graph = json.loads((snapshot / "graph.json").read_text(encoding="utf-8"))
        labels = {node["label"] for node in graph["nodes"]}
        self.assertTrue({"memory", "three scopes", "Jack", "done"} <= labels)

    # --- instructions ---

    def test_crew_instructions_read_the_repo_scope_and_write_only_their_report(self):
        crew = instructions.agent_instructions(self.directory, "crew member Jack")
        self.assertIn("memory show --scope repo", crew)
        self.assertNotIn("--scope project", crew)
        self.assertEqual(len([line for line in crew.splitlines() if " memory add " in line]), 1)
        self.assertIn("memory add Jack report '<summary>'", crew)

    def test_the_captain_is_told_the_repo_vocabulary_and_that_it_is_committed(self):
        captain = " ".join(
            instructions.agent_instructions(self.directory, "Captain Barbossa").split()
        )
        self.assertIn(
            "Use --scope repo ONLY when the user asks to record an architectural "
            "decision, method, or convention for the team; it is committed.",
            captain,
        )
        self.assertIn("Never promote session facts, reports, or events there.", captain)
        self.assertIn("The relation is one of decided|method|convention", captain)
        self.assertIn("--because 'why' --scope repo", captain)
        self.assertIn("Add --supersede only to replace the fact already recorded", captain)


class RepoMemoryInitTests(RepoMemoryTests):
    """`captain memory init` seeds the repo graph from the project rulebook."""

    RULEBOOK = """# AGENTS.md

Prose that is not a rule at all.

## What this is

- A bullet outside a rulebook section is ignored.

## Layout

```text
- a bullet inside fenced code is not a rule
```

Key facts:

- Herdr is driven only through `herdr` subprocesses.
- Settings layer bottom to top, with CLI flags above
  the project file, which sits above the global one.

## Rules

- Run the full gate before pushing.
- Commits follow Conventional Commits.

### Branching and releases

- Cut feature branches from `main`.
"""

    def write_rulebook(self, name="AGENTS.md", text=None):
        path = self.project / name
        path.write_text(self.RULEBOOK if text is None else text, encoding="utf-8")
        return path

    def rows(self, lines, mark):
        return [line[2:] for line in lines if line.startswith(f"{mark} ")]

    # --- the fixed parse ---

    def test_only_bullets_under_a_rulebook_section_are_proposed(self):
        self.write_rulebook()
        _, origin, proposed = memory.rulebook_facts(self.project)

        self.assertEqual(origin, "AGENTS.md")
        self.assertEqual(
            [(heading, bullet) for _, heading, bullet, _ in proposed],
            [
                ("Key facts", "Herdr is driven only through `herdr` subprocesses."),
                (
                    "Key facts",
                    "Settings layer bottom to top, with CLI flags above the project file, "
                    "which sits above the global one.",
                ),
                ("Rules", "Run the full gate before pushing."),
                ("Rules", "Commits follow Conventional Commits."),
                ("Branching and releases", "Cut feature branches from `main`."),
            ],
        )

    def test_a_wrapped_bullet_is_folded_whole_rather_than_cut_at_the_line_break(self):
        self.write_rulebook()
        _, _, proposed = memory.rulebook_facts(self.project)
        wrapped = proposed[1][2]
        self.assertTrue(wrapped.endswith("above the global one."))
        self.assertNotIn("\n", wrapped)

    def test_the_rationale_names_the_source_file_and_the_heading(self):
        self.write_rulebook()
        _, _, proposed = memory.rulebook_facts(self.project)
        self.assertEqual(
            {edge["rationale"] for edge, *_ in proposed},
            {"AGENTS.md > Key facts", "AGENTS.md > Rules", "AGENTS.md > Branching and releases"},
        )

    def test_claude_md_is_read_when_there_is_no_agents_md(self):
        self.write_rulebook("CLAUDE.md")
        _, origin, proposed = memory.rulebook_facts(self.project)
        self.assertEqual(origin, "CLAUDE.md")
        self.assertEqual(len(proposed), 5)

    def test_agents_md_wins_over_claude_md(self):
        self.write_rulebook("CLAUDE.md")
        self.write_rulebook("AGENTS.md", "## Rules\n\n- Only this one.\n")
        _, origin, proposed = memory.rulebook_facts(self.project)
        self.assertEqual((origin, len(proposed)), ("AGENTS.md", 1))

    def test_from_overrides_the_default_and_never_leaks_a_machine_path(self):
        self.write_rulebook()
        outside = self.root / "outside.md"
        outside.write_text("## Rules\n\n- From somewhere else.\n", encoding="utf-8")

        _, origin, proposed = memory.rulebook_facts(self.project, str(outside))

        self.assertEqual(origin, "outside.md")
        self.assertNotIn(str(self.root), proposed[0][0]["rationale"])

    def test_a_missing_rulebook_says_which_files_it_looked_for(self):
        with self.assertRaises(CaptainError) as caught:
            memory.rulebook_facts(self.project)
        self.assertIn("AGENTS.md or CLAUDE.md", str(caught.exception))

    def test_an_over_long_bullet_is_skipped_with_a_reason_not_truncated(self):
        long = "z" * (memory.LABEL_LIMIT + 1)
        self.write_rulebook(text=f"## Rules\n\n- Short one.\n- {long}\n")

        lines = self.run_memory("init")

        self.assertIn("1 skipped", lines[0])
        self.assertEqual(self.rows(lines, "!"), [f"Rules / {long[:60]}...: {self.cap_message()}"])
        self.assertEqual(len(self.rows(lines, "+")), 1)

    def cap_message(self):
        return (
            f"Repo memory values are capped at {memory.LABEL_LIMIT} characters; "
            "record the decision, not the transcript."
        )

    # --- preview vs apply ---

    def test_bare_init_is_a_preview_that_writes_nothing(self):
        self.write_rulebook()

        lines = self.run_memory("init")

        self.assertIn("Would write 5 fact(s), 0 already recorded, 0 skipped.", lines[0])
        self.assertEqual(len(self.rows(lines, "+")), 5)
        self.assertEqual(
            lines[-1], "Preview only. Re-run with --apply to write .captain/graph.json."
        )
        self.assertFalse(self.repo_graph.exists())

    def test_apply_writes_every_proposed_fact_as_a_convention(self):
        self.write_rulebook()

        lines = self.run_memory("init", "--apply")

        self.assertIn("Wrote 5 fact(s)", lines[0])
        self.assertNotIn("Re-run with --apply", "\n".join(lines))
        graph = self.graph()
        self.assertEqual(len(graph["links"]), 5)
        self.assertEqual({link["relation"] for link in graph["links"]}, {"convention"})
        self.assertEqual(
            {link["rationale"] for link in graph["links"]},
            {"AGENTS.md > Key facts", "AGENTS.md > Rules", "AGENTS.md > Branching and releases"},
        )

    def test_seeded_facts_are_sorted_and_deterministic_like_any_repo_write(self):
        self.write_rulebook()
        self.run_memory("init", "--apply")
        first = self.repo_graph.read_bytes()
        graph = self.graph()

        keys = [(link["source"], link["key"], link["target"]) for link in graph["links"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(
            [node["id"] for node in graph["nodes"]], sorted(node["id"] for node in graph["nodes"])
        )

        self.repo_graph.unlink()
        self.run_memory("init", "--apply")
        self.assertEqual(self.repo_graph.read_bytes(), first)

    def test_re_running_apply_is_a_no_op_on_facts_already_recorded(self):
        self.write_rulebook()
        self.run_memory("init", "--apply")
        first = self.repo_graph.read_bytes()

        lines = self.run_memory("init", "--apply")

        self.assertIn("Wrote 0 fact(s), 5 already recorded, 0 skipped.", lines[0])
        self.assertEqual(len(self.rows(lines, "=")), 5)
        self.assertEqual(self.repo_graph.read_bytes(), first)

    def test_a_new_bullet_is_added_without_disturbing_the_recorded_ones(self):
        self.write_rulebook()
        self.run_memory("init", "--apply")

        self.write_rulebook(text=self.RULEBOOK + "\n- Use the Makefile targets.\n")
        lines = self.run_memory("init", "--apply")

        self.assertIn("Wrote 1 fact(s), 5 already recorded", lines[0])
        self.assertEqual(len(self.graph()["links"]), 6)

    def test_init_never_supersedes_a_fact_recorded_with_another_rationale(self):
        self.write_rulebook()
        self.add_repo(
            "Rules", "convention", "Run the full gate before pushing.", "someone typed it by hand"
        )
        before = self.repo_graph.read_bytes()

        lines = self.run_memory("init", "--apply")

        self.assertIn("already recorded with another rationale; left alone.", "\n".join(lines))
        graph = self.graph()
        self.assertEqual(len(graph["links"]), 5)
        self.assertIn("someone typed it by hand", {link["rationale"] for link in graph["links"]})
        self.assertNotEqual(self.repo_graph.read_bytes(), before)  # the other four were added

    def test_seeding_needs_no_captain_session(self):
        """Seeding a checkout is the counterpart of `captain init`, not a session command."""
        self.write_rulebook()
        parsed = cli.parser().parse_args(["memory", "init", "--apply"])
        with (
            patch.object(memory, "session", side_effect=AssertionError("opened a session")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            memory.memory(parsed, self.pane, self.project)
        self.assertEqual(len(self.graph()["links"]), 5)


if __name__ == "__main__":
    unittest.main()
