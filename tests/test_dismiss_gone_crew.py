import contextlib
import io
import os
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, lifetime, protocol, retirement, runtime, sessions, store
from captain_barbossa.crew import Crew
from tests.home_isolation import SessionCase


class DismissGoneCrewTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        self.current = sessions.session(self.project, self.pane, create=True)
        self.crew = Crew(
            "jack",
            {
                "name": "Jack",
                "agent": "jack",
                "provider": "codex",
                "id": "jack",
                "incarnation_id": "b" * 32,
                "pane": "w1:p2",
            },
            self.current,
        )
        self.current.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.current.meta_path, self.current.meta)
        protocol.begin(self.crew, self.project, "edit src", ["src"], ["edit"])
        with protocol.checkpoint(self.current.directory) as state:
            protocol.active(state, self.crew)["messages"] = [{"id": "m", "delivery": "read"}]
        self.enterContext(patch.object(agents, "relayout_session"))
        self.enterContext(patch.object(agents.Placement, "even_after_close", return_value=[]))

    def dismiss(self, api, **flags):
        args = SimpleNamespace(session=self.current.meta["id"], name="Jack", **flags)
        with (
            patch.object(runtime, "herdr", side_effect=api),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            agents.dismiss_crew(args, self.pane, self.project)
        return output.getvalue()

    def test_missing_pane_releases_owned_paths_without_report(self):
        def missing(*args, **kwargs):
            if args[:2] in (("pane", "get"), ("pane", "close")):
                raise runtime.CaptainError("pane_not_found")
            return {}

        output = self.dismiss(missing)
        assignment = protocol.active_assignment(self.crew)
        self.assertEqual(assignment["state"], "done")
        self.assertEqual(assignment["outcome"], "lost")
        self.assertIn("without a report", output)
        self.assertIn("Owned paths released", output)
        other = Crew("will", {"incarnation_id": "second", "agent": "will"}, self.current)
        protocol.begin(other, self.project, "take over", ["src"], ["edit"])

    def test_live_or_unreadable_pane_still_requires_report(self):
        for response in ({}, runtime.CaptainError("connection failed")):
            with self.subTest(response=response):

                def api(*args, **kwargs):
                    if isinstance(response, Exception):
                        raise response
                    return response

                with self.assertRaisesRegex(runtime.CaptainError, "no done report"):
                    self.dismiss(api)
        self.assertNotEqual(protocol.active_assignment(self.crew)["state"], "done")

    def test_confirmed_process_exit_allows_dismissal_with_pane_present(self):
        with patch.object(retirement, "gone", return_value=True):
            self.assertIn("without a report", self.dismiss(lambda *a, **k: {}))
        self.assertEqual(protocol.active_assignment(self.crew)["outcome"], "lost")

    def test_managed_shutdown_failure_keeps_assignment_reserved(self):
        self.crew.record["lifetime_id"] = "managed"
        store.write_json(self.current.meta_path, self.current.meta)
        with (
            patch.object(retirement, "gone", return_value=True),
            patch.object(retirement, "retire", return_value={"lifetime": {"ok": False}}),
            self.assertRaisesRegex(runtime.CaptainError, "cleanup-pending"),
        ):
            self.dismiss(lambda *a, **k: {})
        self.assertNotEqual(protocol.active_assignment(self.crew)["state"], "done")

    def test_claimed_lifetime_after_herdr_restart_dismisses(self):
        ref = lifetime.prepare(self.current, self.crew.record, ["native"], {}, self.project)
        self.crew.record.update(lifetime_id=ref, tab="w1:t1", terminal_id="old-terminal")
        lifetime.bind(self.current, ref, self.crew.record)
        store.write_json(self.current.meta_path, self.current.meta)
        store.write_json(
            self.current.directory / "captain.json",
            {"pane": "captain", "terminal_id": "captain-terminal"},
        )
        record = lifetime.read(self.current, ref)
        record.update(state="claimed", group=12345)
        store.write_json(self.current.directory / "lifetimes" / f"{ref}.json", record)

        def missing(*args, **kwargs):
            raise runtime.CaptainError("pane_not_found")

        with (
            patch.object(lifetime.os, "killpg", side_effect=ProcessLookupError),
            patch.object(retirement.native_history, "cleanup", return_value={"state": "pending"}),
        ):
            self.assertIn("without a report", self.dismiss(missing))
        self.assertEqual(lifetime.read(self.current, ref)["close_state"], "closed")
        self.assertEqual(protocol.active_assignment(self.crew)["outcome"], "lost")

    def test_lost_owner_group_absence_does_not_authorize_worktree_removal(self):
        record = {"state": "claimed", "group": 12345}
        with patch.object(lifetime.os, "killpg", side_effect=ProcessLookupError):
            self.assertTrue(lifetime._shutdown_verdict(record, "group").ok)
            self.assertFalse(lifetime._shutdown_verdict(record, "worktree").ok)

    def test_retirement_separates_dismissal_from_retained_artifacts(self):
        self.crew.record.update(lifetime_id="managed", worktree_id="tree")
        with (
            patch.object(
                retirement, "stop", return_value=lifetime.Verdict(True, "owner-lost-group-absent")
            ) as stop,
            patch.object(retirement.native_history, "cleanup", return_value={"state": "pending"}),
            patch.object(retirement, "_final_result", return_value={"id": "result"}),
            patch.object(retirement, "_no_cwd_holders"),
            patch.object(
                retirement, "dispose", side_effect=runtime.CaptainError("unverified descendants")
            ),
        ):
            result = retirement.retire(self.crew)
        stop.assert_called_once_with(self.current, "managed", scope="group")
        self.assertTrue(result["lifetime"]["ok"])
        self.assertIn("retained", result["worktree"])
