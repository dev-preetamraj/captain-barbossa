"""checkout = auto: share the checkout unless owned paths contend; explicit choices win."""

import contextlib
import io
import os
import unittest
from unittest.mock import patch

from captain_barbossa import agents, cli, config, runtime, sessions, store, worktrees
from captain_barbossa.crew import Crew
from tests.home_isolation import HERDR, SessionCase


class CheckoutChoiceTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        self.enterContext(
            patch.object(
                config,
                "settings",
                return_value=config.merge(config.defaults(), {"crew": {"isolation_enabled": True}}),
            )
        )
        (self.project / ".git").mkdir()
        self.directory, self.meta = sessions.session(self.project, self.pane, create=True)

    def args(self, *flags):
        return cli.parser().parse_args(
            ["--session", self.meta["id"], "crew", "--task", "work", *flags]
        )

    def choose(self, *flags, profile_source=None):
        return agents.choose_checkout(
            self.args(*flags), self.project, self.directory, profile_source
        )

    def occupy(self, directory, paths, state="working", root=None, ident="a" * 32):
        root = root or self.project
        path = directory / "protocol.json"
        protocol_state = (
            store.read_json(path) if path.exists() else {"active": {}, "assignments": {}}
        )
        protocol_state["assignments"][ident] = {
            "id": ident,
            "crew": "Jack",
            "state": state,
            "execution_root": str(root),
            "paths": list(paths),
        }
        store.write_json(path, protocol_state)

    def test_isolation_ships_disabled(self):
        self.assertFalse(config.defaults()["crew"]["isolation_enabled"])

    def test_disabled_auto_and_shared_never_select_isolation(self):
        self.occupy(self.directory, ["src"])
        with patch.object(config, "flag", return_value=False):
            for flags in (
                (),
                ("--allow", "commit"),
                ("--allow", "edit", "--owns", "src"),
                ("--checkout", "auto", "--allow", "commit"),
                ("--checkout", "shared", "--allow", "commit"),
            ):
                with self.subTest(flags=flags):
                    checkout, source = self.choose(*flags)
                    self.assertEqual(checkout, "shared")
                    self.assertIn("isolation disabled", source)

    def test_disabled_worktree_flag_profile_and_settings_fail_before_launch(self):
        for flags, settings in (
            (("--checkout", "worktree"), {}),
            (("--profile", "isolated"), {"profiles": {"isolated": {"checkout": "worktree"}}}),
            ((), {"crew": {"checkout": "worktree"}}),
        ):
            with (
                self.subTest(flags=flags),
                patch.object(
                    config, "settings", return_value=config.merge(config.defaults(), settings)
                ),
                patch.object(runtime, "herdr") as herdr,
                patch.object(worktrees, "reserve") as reserve,
                self.assertRaisesRegex(runtime.CaptainError, "worktree-disabled"),
            ):
                agents.create_crew(self.args(*flags), self.pane, self.project)
            herdr.assert_not_called()
            reserve.assert_not_called()
        self.assertEqual(store.read_json(self.directory / "session.json")["crew"], {})

    def test_disabled_direct_recruitment_cannot_bypass_selection(self):
        crew = Crew(
            "jack",
            {"checkout": "worktree"},
            sessions.Session(self.directory, self.meta),
        )
        with (
            patch.object(config, "flag", return_value=False),
            patch.object(worktrees, "reserve") as reserve,
            patch.object(worktrees, "create") as create,
            self.assertRaisesRegex(runtime.CaptainError, "worktree-disabled"),
        ):
            agents.begin_recruitment(crew, self.project, self.args())
        reserve.assert_not_called()
        create.assert_not_called()

    def test_read_only_crew_share(self):
        self.occupy(self.directory, ["src"])
        self.assertEqual(self.choose(), ("shared", "[crew] checkout auto: read-only crew"))
        self.assertEqual(self.choose("--allow", "test")[0], "shared")

    def test_disjoint_owns_share(self):
        self.occupy(self.directory, ["src/a.py"])
        checkout, source = self.choose("--allow", "edit", "--owns", "src/b.py")
        self.assertEqual(checkout, "shared")
        self.assertIn("owns disjoint from active crew", source)

    def test_overlapping_owns_get_worktree_naming_the_occupant(self):
        self.occupy(self.directory, ["src"])
        checkout, source = self.choose("--allow", "edit", "--owns", "src/b.py")
        self.assertEqual(checkout, "worktree")
        self.assertIn(f"owns overlap active assignment {'a' * 32} (Jack)", source)

    def test_overlap_counts_other_sessions_and_worktree_roots(self):
        other, _ = sessions.session(self.project, self.pane, create=True)
        self.occupy(other, ["docs"])
        self.assertEqual(self.choose("--allow", "edit", "--owns", "docs/x.md")[0], "worktree")
        self.occupy(self.directory, ["src"], root=self.root / "worktree", ident="b" * 32)
        checkout, source = self.choose("--allow", "edit", "--owns", "src")
        self.assertEqual(checkout, "worktree")
        self.assertIn("b" * 32, source)

    def test_owns_outside_the_checkout_share_because_a_worktree_cannot_hold_them(self):
        outside = str(self.root / "elsewhere" / "notes.md")
        self.occupy(self.directory, ["src"])
        checkout, source = self.choose("--allow", "edit", "--owns", outside, "--owns", "src")
        self.assertEqual(checkout, "shared")
        self.assertIn("outside the checkout", source)
        with self.assertRaisesRegex(runtime.CaptainError, "worktree-outside"):
            self.choose("--allow", "edit", "--owns", outside, "--checkout", "worktree")

    def test_done_assignments_release_their_paths(self):
        self.occupy(self.directory, ["src"], state="done")
        self.assertEqual(self.choose("--allow", "edit", "--owns", "src")[0], "shared")

    def test_write_grant_without_owns_gets_worktree(self):
        checkout, source = self.choose("--allow", "commit")
        self.assertEqual(checkout, "worktree")
        self.assertIn("write grant without owned paths", source)

    def test_background_and_handoff_share(self):
        self.assertEqual(self.choose("--background")[0], "shared")
        checkout, source = self.choose("--allow", "commit", "--handoff", "c" * 32)
        self.assertEqual(checkout, "shared")
        self.assertIn("handoff", source)

    def test_non_git_project_shares(self):
        (self.project / ".git").rmdir()
        self.assertEqual(
            self.choose("--allow", "commit"), ("shared", "[crew] checkout: not a Git checkout")
        )

    def test_non_git_settings_worktree_refuses(self):
        (self.project / ".git").rmdir()
        with patch.object(config, "text", return_value="worktree"):
            with self.assertRaisesRegex(runtime.CaptainError, "worktree-required"):
                self.choose()
        self.assertEqual(self.choose("--checkout", "shared")[0], "shared")

    def test_explicit_flag_profile_and_settings_win(self):
        self.assertEqual(self.choose("--checkout", "worktree"), ("worktree", "--checkout"))
        self.assertEqual(
            self.choose("--checkout", "shared", "--allow", "commit"), ("shared", "--checkout")
        )
        self.assertEqual(
            self.choose("--checkout", "worktree", profile_source="[profiles.p] checkout"),
            ("worktree", "[profiles.p] checkout"),
        )
        with (
            patch.object(config, "text", return_value="worktree"),
            patch.object(config, "source", return_value="/home/settings.toml"),
        ):
            self.assertEqual(
                self.choose(), ("worktree", "[crew] checkout from /home/settings.toml")
            )

    def test_profile_auto_keeps_its_reason(self):
        checkout, source = self.choose("--checkout", "auto", profile_source="[profiles.p] checkout")
        self.assertEqual(checkout, "shared")
        self.assertEqual(source, "[profiles.p] checkout auto: read-only crew")

    def test_auto_worktree_on_linked_checkout_still_refuses(self):
        (self.project / ".git").rmdir()
        (self.project / ".git").write_text("gitdir: /unrelated/admin\n")
        self.assertEqual(self.choose()[0], "shared")
        with self.assertRaisesRegex(runtime.CaptainError, "unsupported-checkout"):
            self.choose("--allow", "commit")

    def test_unknown_setting_refused(self):
        with patch.object(config, "text", return_value="copy"):
            with self.assertRaisesRegex(runtime.CaptainError, "auto, shared or worktree"):
                self.choose()

    def test_recruitment_prints_the_auto_reason_as_source(self):
        with (
            patch.object(agents, "is_durable", return_value=True),
            patch.object(agents, "executable", return_value="/bin/codex"),
            patch.object(agents, "launch_background") as launch,
            contextlib.redirect_stderr(io.StringIO()) as error,
        ):
            agents.create_crew(
                self.args("--agent", "codex", "--background"), self.pane, self.project
            )
        self.assertEqual(launch.call_args.args[0].record["checkout"], "shared")
        self.assertIn('"checkout": "[crew] checkout auto: read-only crew"', error.getvalue())


if __name__ == "__main__":
    unittest.main()
