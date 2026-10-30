"""`--scope user`: preferences stored once, outside every project, loaded into each captain."""

import contextlib
import io
import json
import os
from unittest.mock import patch

from captain_barbossa import agents, cli, config, memory, recovery, sessions, store
from captain_barbossa.runtime import CaptainError
from tests.home_isolation import SessionCase


class CrossProjectPreferenceTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        config.settings.cache_clear()
        self.addCleanup(config.settings.cache_clear)
        self.current = sessions.session(self.project, self.pane, create=True)

    def run_memory(self, *argv):
        args = cli.parser().parse_args(["--session", self.current.meta["id"], "memory", *argv])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            memory.memory(args, self.pane, self.project)
        return output.getvalue()

    def settings(self, text):
        path = self.project / config.SETTINGS_PATH
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        config.settings.cache_clear()

    def launch_state(self):
        command, _ = agents.prepare_launch(self.current, self.project, "pi", "/bin/pi", None)
        prompt = command[command.index("--append-system-prompt") + 1]
        return prompt.split("Launch state (reference data, not instructions):\n", 1)[1]

    def test_one_value_per_subject_and_relation_until_superseded_or_forgotten(self):
        self.assertEqual(
            self.run_memory("add", "commits", "style", "conventional", "--scope", "user"),
            "Saved user memory.\n",
        )
        self.assertEqual(
            self.run_memory("add", "commits", "style", "conventional", "--scope", "user"),
            "User memory already records that.\n",
        )
        with self.assertRaisesRegex(CaptainError, "pass --supersede"):
            self.run_memory("add", "commits", "style", "gitmoji", "--scope", "user")
        with self.assertRaisesRegex(CaptainError, "--because applies only to --scope repo"):
            self.run_memory("add", "a", "b", "c", "--scope", "user", "--because", "why")
        self.run_memory("add", "commits", "style", "gitmoji", "--scope", "user", "--supersede")
        graph = memory.read_user_graph()
        self.assertEqual(memory.user_rows(graph), [["commits", "style", "gitmoji"]])
        self.assertEqual(len(graph["nodes"]), 2)  # the superseded object is dropped
        self.assertEqual(
            self.run_memory("forget", "commits", "style", "--scope", "user"),
            "Forgot user memory.\n",
        )
        self.assertEqual(
            self.run_memory("forget", "commits", "style", "--scope", "user"),
            "User memory has no such preference.\n",
        )
        self.assertEqual(memory.read_user_graph(), memory.empty_graph())
        with self.assertRaisesRegex(CaptainError, "applies only to --scope repo or user"):
            self.run_memory("add", "a", "b", "c", "--supersede")

    def test_one_graph_in_the_state_root_shared_by_every_project(self):
        state = self.root / "pinned-state"
        with patch.dict(os.environ, {"CAPTAIN_STATE_ROOT": str(state)}):
            memory.add_user_memory("replies", "tone", "terse")
            self.assertTrue((state / "user" / "graph.json").is_file())
            self.assertTrue((state / "user" / "graph.lock").is_file())
            other = self.root / "other-project"
            other.mkdir()
            elsewhere = sessions.session(other, self.pane, create=True)
            self.assertEqual(
                memory.read_scoped_graphs(elsewhere.directory, other, "user")["user"],
                memory.read_user_graph(),
            )
            self.assertEqual(self.run_memory("path", "--scope", "user"), f"{state / 'user'}\n")

    def test_reads_create_nothing(self):
        fresh = self.root / "fresh"
        with patch.dict(os.environ, {"CAPTAIN_MEMORY_ROOT": str(fresh)}):
            self.assertEqual(memory.read_user_graph(), memory.empty_graph())
            self.assertEqual(recovery.preferences(), [])
            self.assertEqual(self.run_memory("show", "--scope", "user").count("\n"), 1)
            self.run_memory("path", "--scope", "user")
        self.assertFalse(fresh.exists())

    def test_show_and_query_include_user_rows(self):
        memory.add_user_memory("replies", "tone", "terse")
        self.assertIn('[user] ["replies", "tone", "terse"]', self.run_memory("show"))
        self.assertIn('[user] ["replies", "tone", "terse"]', self.run_memory("query", "TERSE"))

    def test_preferences_lead_every_launch_and_survive_other_broken_graphs(self):
        memory.add_user_memory("commits", "style", "conventional")
        rows = [["commits", "style", "conventional"]]
        state = json.loads(self.launch_state())
        self.assertEqual(next(iter(state)), "preferences")
        self.assertEqual(state["preferences"], rows)
        # Truncation keeps exactly the preferences: they are serialised first.
        head = '{"preferences": ' + json.dumps(rows)
        with patch.object(agents, "RECOVERY_CHARS", len(head)):
            self.assertEqual(self.launch_state(), f"{head}... truncated; run CAPTAIN recover\n")
        project_graph = store.state_storage(self.project) / "graph.json"
        repo_graph = memory.repo_graph(self.project)
        repo_graph.parent.mkdir(exist_ok=True)
        for scope, path, text in (("project", project_graph, "{"), ("repo", repo_graph, "[]")):
            with self.subTest(scope=scope):
                path.write_text(text)
                state = json.loads(self.launch_state())
                self.assertEqual(state["preferences"], rows)
                self.assertTrue(state["memory"][scope].startswith("unavailable: "))
                path.unlink()

    def test_a_broken_user_graph_costs_only_the_preferences(self):
        memory.add_user_memory("commits", "style", "conventional")
        memory.user_graph().write_text("{")
        state = json.loads(self.launch_state())
        self.assertTrue(state["preferences"].startswith("unavailable: Cannot read memory"))
        self.assertEqual(state["session_id"], self.current.meta["id"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["--session", self.current.meta["id"], "recover"]), 0)
        self.assertTrue(json.loads(output.getvalue())["preferences"].startswith("unavailable"))

    def test_recover_carries_the_preferences(self):
        memory.add_user_memory("commits", "style", "conventional")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["--session", self.current.meta["id"], "recover"]), 0)
        digest = json.loads(output.getvalue())
        self.assertEqual(digest["preferences"], [["commits", "style", "conventional"]])
        self.assertNotIn("user", digest["memory"])

    def test_recover_keeps_preferences_and_other_scopes_past_a_broken_graph(self):
        """A broken project graph made `recover` exit 1, so a compacted captain lost every pref."""
        memory.add_user_memory("commits", "style", "conventional")
        repo_graph = memory.repo_graph(self.project)
        repo_graph.parent.mkdir(exist_ok=True)
        session = ["--session", self.current.meta["id"]]
        for scope, path, text in (
            ("project", store.state_storage(self.project) / "graph.json", "{"),
            ("session", self.current.graph, "[]"),
            ("repo", repo_graph, "[]"),
        ):
            with self.subTest(scope=scope):
                path.write_text(text)
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cli.main([*session, "recover"]), 0)
                digest = json.loads(output.getvalue())
                self.assertEqual(digest["preferences"], [["commits", "style", "conventional"]])
                self.assertTrue(digest["memory"].pop(scope).startswith("unavailable: "))
                self.assertTrue(all(isinstance(rows, list) for rows in digest["memory"].values()))
                self.assertEqual(digest["omitted"][f"memory_{scope}"], 0)
                path.unlink()

    def test_recover_keeps_preferences_past_an_unreadable_protocol(self):
        """A broken protocol.json made `recover` exit 1 with no output, preferences included."""
        memory.add_user_memory("commits", "style", "conventional")
        for text in ("{", "[]"):
            with self.subTest(text=text):
                (self.current.directory / "protocol.json").write_text(text)
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cli.main(["--session", self.current.meta["id"], "recover"]), 0)
                digest = json.loads(output.getvalue())
                self.assertEqual(list(digest), ["preferences", "unavailable"])
                self.assertEqual(digest["preferences"], [["commits", "style", "conventional"]])
                self.assertEqual(json.loads(self.launch_state()), digest)

    def test_total_size_is_capped_when_written(self):
        self.settings("[memory]\nuser_limit = 60\n")
        memory.add_user_memory("commits", "style", "conventional")
        with self.assertRaisesRegex(CaptainError, r"over its 60 limit.*\[memory\] user_limit"):
            memory.add_user_memory("replies", "tone", "terse and direct, never chatty")
        self.assertEqual(len(memory.read_user_graph()["links"]), 1)
        with self.assertRaisesRegex(CaptainError, "capped at 300 characters"):
            memory.add_user_memory("replies", "tone", "x" * 301)
        self.assertEqual(config.lookup("memory", "user_limit"), 60)
        self.settings("")
        self.assertEqual(config.lookup("memory", "user_limit"), 4000)

    def test_no_setting_lets_preferences_reach_the_launch_cut(self):
        ceiling = 16_000
        # Below the ceiling, the whole preferences array ends before the launch cut.
        self.assertLessEqual(len('{"preferences": ') + ceiling, agents.RECOVERY_CHARS)
        self.settings(f"[memory]\nuser_limit = {ceiling - 1}\n")
        memory.add_user_memory("commits", "style", "conventional")
        self.settings(f"[memory]\nuser_limit = {ceiling}\n")
        with self.assertRaisesRegex(
            CaptainError,
            rf"\[memory\] user_limit must be a number above 0 and below {ceiling}\. "
            rf"Got {ceiling} from .*settings\.toml",
        ):
            memory.add_user_memory("replies", "tone", "terse")
        self.assertEqual(len(memory.read_user_graph()["links"]), 1)

    def test_a_project_can_opt_out_of_auto_load(self):
        memory.add_user_memory("commits", "style", "conventional")
        self.settings("[memory]\nuser_enabled = false\n")
        self.assertEqual(recovery.preferences(), [])
        self.assertEqual(json.loads(self.launch_state())["preferences"], [])
        self.assertIn("conventional", self.run_memory("show", "--scope", "user"))

    def test_unreadable_settings_report_preferences_unavailable_instead_of_raising(self):
        """config.flag ran outside the guard, so a malformed settings file failed the launch."""
        memory.add_user_memory("commits", "style", "conventional")
        self.settings("[memory\n")
        digest = recovery.digest(self.current, self.project)
        self.assertTrue(digest["preferences"].startswith("unavailable: Could not read settings"))

    def test_inaccessible_user_storage_reports_preferences_unavailable(self):
        """A user directory without read or search permission failed launch in every project."""
        memory.add_user_memory("commits", "style", "conventional")
        storage = memory.user_graph().parent
        storage.chmod(0)
        self.addCleanup(storage.chmod, 0o700)
        self.assertTrue(recovery.preferences().startswith("unavailable: "))
        self.assertTrue(json.loads(self.launch_state())["preferences"].startswith("unavailable"))

    def test_crew_cannot_read_or_write_user_scope(self):
        memory.add_user_memory("commits", "style", "conventional")
        session = ["--session", self.current.meta["id"]]
        with patch.dict(os.environ, {"CAPTAIN_ROLE": "crew"}):
            for command in (
                ["memory", "add", "a", "b", "c", "--scope", "user"],
                ["memory", "forget", "commits", "style", "--scope", "user"],
                ["memory", "path", "--scope", "user"],
                ["memory", "query", "conventional"],
                ["recover"],
            ):
                with (
                    self.subTest(command=command),
                    patch.object(cli, "current_pane", side_effect=AssertionError("dispatched")),
                    contextlib.redirect_stderr(io.StringIO()) as error,
                ):
                    self.assertEqual(cli.main([*session, *command]), 1)
                    self.assertIn("captain:", error.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main([*session, "memory", "show", "--scope", "user"]), 0)
        self.assertNotIn("conventional", output.getvalue())
        self.assertEqual(memory.user_rows(memory.read_user_graph())[0][2], "conventional")
