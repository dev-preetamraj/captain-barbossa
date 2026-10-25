import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import cli, prompts, sessions
from captain_barbossa.runtime import CaptainError
from captain_barbossa.sessions import all_sessions, prune_sessions, session
from captain_barbossa.store import write_json

PANE = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}


class SessionResumeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        roots = {
            "CAPTAIN_STATE_ROOT": str(self.root / "state"),
            "CAPTAIN_TEMP_ROOT": str(self.root / "temp"),
        }
        self.enterContext(patch.dict(os.environ, roots))
        for name in [n for n in os.environ if n.startswith("CAPTAIN_") and n not in roots]:
            self.enterContext(patch.dict(os.environ)).pop(name)
        self.enterContext(patch.object(os, "chdir"))
        self.here = self.root / "here"
        self.other = self.root / "other"
        self.here.mkdir()
        self.other.mkdir()
        self.project = self.here

    def make(self, project):
        return session(project, PANE, create=True)

    def run_main(self, *args, tty=False):
        with (
            patch.object(cli, "project_root", side_effect=lambda: self.project),
            patch.object(cli, "current_pane", return_value=PANE),
            patch.object(cli, "launch") as launch,
            patch("sys.stdin.isatty", return_value=tty),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            code = cli.main(list(args))
        return code, out.getvalue(), launch

    def test_if_unnamed_titles_only_an_unnamed_session_and_rename_wins(self):
        current = self.make(self.here)
        session_id = current.meta["id"]
        self.run_main("--session", session_id, "--rename", "Login fix", "--if-unnamed")
        self.run_main("--session", session_id, "--rename", "Second title", "--if-unnamed")
        self.assertEqual(all_sessions(self.here)[0][1], "Login fix")
        self.run_main("--session", session_id, "--rename", "alpha")
        self.run_main("--session", session_id, "--rename", "Late title", "--if-unnamed")
        self.assertEqual(all_sessions(self.here)[0][1], "alpha")

    def test_naming_an_old_session_is_not_activity(self):
        old = self.make(self.here)
        (old.directory / "session.lock").unlink()  # made before sessions were locked
        stamp = time.time() - 30 * 86400
        for path in (*old.directory.rglob("*"), old.directory):
            os.utime(path, (stamp, stamp))
        before = all_sessions(self.here)[0][3]
        self.run_main("--session", old.meta["id"], "--rename", "alpha")
        self.run_main("--session", old.meta["id"], "--rename", "beta", "--if-unnamed")
        self.assertEqual(all_sessions(self.here)[0][1:4], ("alpha", old.meta["id"], before))
        with patch.object(sessions, "live_agents", return_value=(set(), set())):
            removed = prune_sessions(self.here)
        self.assertEqual([path.name for path in removed], [old.meta["id"]])

    def test_rename_names_the_session_and_lists_it(self):
        current = self.make(self.here)
        self.run_main("--session", current.meta["id"], "--rename", "alpha")
        self.assertEqual(all_sessions(self.here)[0][1], "alpha")

    def test_rename_and_resume_are_flags_not_subcommands(self):
        for argv in (["rename", "alpha"], ["resume"], ["--rename", "a", "--resume"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cli.parser().parse_args(argv)

    def test_flags_refuse_a_subcommand(self):
        current = self.make(self.here)
        session_id = current.meta["id"]
        with contextlib.redirect_stderr(io.StringIO()) as error:
            code, _, launch = self.run_main("--session", session_id, "--rename", "a", "status")
            self.assertEqual(code, 1)
            self.assertEqual(self.run_main("--resume", "x", "status")[0], 1)
        self.assertIn("take no subcommand", error.getvalue())
        self.assertIsNone(all_sessions(self.here)[0][1])
        launch.assert_not_called()

    def test_if_unnamed_without_rename_never_launches(self):
        session_id = self.make(self.here).meta["id"]
        for argv in (["--if-unnamed"], ["--if-unnamed", "--resume"], ["--if-unnamed", "status"]):
            with (
                self.subTest(argv=argv),
                patch.object(cli, "bootstrap") as bootstrap,
                contextlib.redirect_stderr(io.StringIO()) as error,
            ):
                code, _, launch = self.run_main("--session", session_id, *argv)
                self.assertEqual(code, 1)
                self.assertIn("--if-unnamed needs --rename", error.getvalue())
                bootstrap.assert_not_called()
                launch.assert_not_called()
        self.assertIsNone(all_sessions(self.here)[0][1])

    def test_rename_rejects_an_id_shaped_name(self):
        current = self.make(self.here)
        with self.assertRaises(CaptainError):
            cli.rename_session(current.directory, "a" * 32)

    def test_list_is_flat_and_only_contains_current_project_sessions(self):
        far = self.make(self.other)
        near = self.make(self.here)
        write_json(far.directory / "session.json", {"id": "x"})  # no project recorded
        rows = all_sessions(self.here)
        self.assertEqual([row[2] for row in rows], [near.meta["id"], far.meta["id"]])
        self.assertEqual(rows[1][0], "-")
        _, out, launch = self.run_main("--resume")
        self.assertNotIn(str(self.here), out)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertTrue(out.startswith(f"-  {near.meta['id']}  "))
        self.assertNotIn(str(self.other), out)
        self.assertNotIn("-\n", out)
        launch.assert_not_called()

    def test_resume_reports_when_current_project_has_no_sessions(self):
        self.make(self.other)
        code, out, launch = self.run_main("--resume")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        launch.assert_not_called()

    def test_resume_by_name_launches_from_that_project(self):
        far = self.make(self.other)
        self.project = self.other
        self.run_main("--session", far.meta["id"], "--rename", "boat")
        self.project = self.here
        code, _, launch = self.run_main("--resume", "boat")
        self.assertEqual(code, 0)
        args, project = launch.call_args.args[0], launch.call_args.args[2]
        self.assertEqual((args.session, project), (far.meta["id"], self.other))
        os.chdir.assert_called_once_with(str(self.other))

    def test_resume_by_id(self):
        far = self.make(self.other)
        _, _, launch = self.run_main("--resume", far.meta["id"])
        self.assertEqual(launch.call_args.args[0].session, far.meta["id"])

    def test_ambiguous_name_errors(self):
        for project in (self.here, self.other):
            current = self.make(project)
            self.project = project
            self.run_main("--session", current.meta["id"], "--rename", "twin")
        self.project = self.here
        with self.assertRaisesRegex(CaptainError, "ambiguous-session"):
            cli.find_session(self.here, "twin")

    def test_tty_picks_only_from_the_current_project(self):
        self.make(self.other)
        near = self.make(self.here)
        with patch.object(cli, "pick_session", return_value=near.meta["id"]) as pick:
            _, _, launch = self.run_main("--resume", tty=True)
        self.assertEqual([card[0] for card in pick.call_args.args[1]], [near.meta["id"]])
        self.assertEqual(launch.call_args.args[0].session, near.meta["id"])

    def test_picker_cards_show_title_age_and_crew_but_never_the_id(self):
        current = self.make(self.here)
        self.run_main("--session", current.meta["id"], "--rename", "Login fix")
        roster = {k: {"name": k} for k in ("Jack", "Elizabeth", "Will", "Anna")}
        write_json(current.directory / "session.json", {**current.meta, "crew": roster})
        row = all_sessions(self.here)[0]
        title, meta = sessions.session_card(row, now=row[3] + 10 * 3600)
        self.assertEqual((title, meta), ("Login fix", "10h ago · Jack, Elizabeth +2"))
        bare = self.make(self.here)
        row = next(r for r in all_sessions(self.here) if r[2] == bare.meta["id"])
        self.assertEqual(sessions.session_card(row, now=row[3]), ("Untitled session", "just now"))
        cards = [(current.meta["id"], title, meta)]
        text = "".join(t for _, t in prompts.session_frame("here", cards, "", 0, 0, 3))
        self.assertIn("Resume session (1 of 3)", text)
        self.assertIn("❯ Login fix", text)
        self.assertNotIn(current.meta["id"], text)
        self.assertIn("Esc cancel", text)

    def test_picker_hides_terminal_cursor(self):
        from prompt_toolkit import Application
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import DummyInput
        from prompt_toolkit.output import DummyOutput

        with (
            create_app_session(input=DummyInput(), output=DummyOutput()),
            patch.object(Application, "run", autospec=True) as run,
        ):
            prompts.pick_session("here", [("session", "Login fix", "just now")])
            app = run.call_args.args[0]
            content = app.layout.current_control.create_content(width=80, height=24)
            self.assertFalse(content.show_cursor)

    def test_picker_search_box_and_header_styles(self):
        from prompt_toolkit.utils import get_cwidth

        cards = [("session", "Login fix", "just now")]
        for query in ("", "Login", "Log", "no match", "界" * 40):
            with self.subTest(query=query):
                frame = prompts.session_frame("here", cards, query, 0, 0, 1, width=30)
                lines = "".join(text for _, text in frame).splitlines()
                self.assertEqual(lines[0], "─" * 30)
                self.assertIn(("class:question", "Resume session"), frame)
                self.assertIn(("class:separator", " (1 of 1)\n"), frame)
                self.assertEqual(lines[2], "╭" + "─" * 28 + "╮")
                self.assertEqual(get_cwidth(lines[3]), 30)
                self.assertEqual(lines[4], "╰" + "─" * 28 + "╯")
                self.assertEqual(lines[5], "   here")
                self.assertFalse(any("SetCursorPosition" in style for style, _ in frame))
                if query:
                    self.assertNotIn("Search", lines[3])
                    self.assertIn(query[-13:], lines[3])
                else:
                    self.assertIn(("class:separator", "⌕ Search…"), frame)

    def test_picker_separates_cards_and_shows_only_size_cards_with_cues(self):
        cards = [(str(i), f"t{i}", f"m{i}") for i in range(6)]
        text = "".join(t for _, t in prompts.session_frame("here", cards, "", 3, 2, 6, 2))
        self.assertIn("m2\n\n", text)
        self.assertIn("↑ 2 more", text)
        self.assertIn(" ❯ t3\n", text)
        self.assertNotIn(" ↓ t3\n", text)
        unpicked = "".join(t for _, t in prompts.session_frame("here", cards, "", 2, 2, 6, 2))
        self.assertIn(" ❯ t2\n", unpicked)
        self.assertIn(" ↓ t3\n", unpicked)
        self.assertNotIn("↓ 2 more", text)
        final = "".join(t for _, t in prompts.session_frame("here", cards, "", 5, 4, 6, 2))
        self.assertNotIn("↓", final.split("↑↓ move")[0])
        self.assertNotIn("t1", text)
        self.assertNotIn("t4", text)
