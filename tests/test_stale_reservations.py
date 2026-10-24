"""Dead-workspace reservations require explicit, successful dismissal to release."""

import contextlib
import io
import os
from types import SimpleNamespace
from unittest.mock import patch

from captain_barbossa import agents, execution, lifetime, protocol, runtime, sessions, store
from captain_barbossa.crew import Crew
from tests.home_isolation import SessionCase


class StaleReservationsTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.enterContext(patch.dict(os.environ, {"CAPTAIN_ROLE": "captain"}))
        self.old = sessions.session(self.project, self.pane, create=True)
        self.crew = Crew(
            "jack",
            {
                "id": "jack",
                "name": "Jack",
                "agent": "old-jack",
                "pane": "w1:p2",
                "tab": "w1:t2",
                "terminal_id": "crew-terminal",
                "incarnation_id": "b" * 32,
            },
            self.old,
        )
        self.old.meta["crew"]["jack"] = self.crew.record
        store.write_json(self.old.meta_path, self.old.meta)
        self.assignment = protocol.begin(self.crew, self.project, "edit src", ["src"], ["edit"])
        message = protocol.deliver(self.crew, "edit src", initial=True, queue_only=True)
        protocol.receipt(self.old.directory, "jack", [message])
        self.new_pane = {"workspace_id": "w2", "tab_id": "w2:t1", "pane_id": "w2:p1"}
        self.current = sessions.session(self.project, self.new_pane, create=True)
        self.replacement = Crew("will", {"name": "Will", "incarnation_id": "new"}, self.current)
        self.args = SimpleNamespace(session=self.old.meta["id"], name="Jack")
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def recruit(self):
        return protocol.begin(self.replacement, self.project, "edit src", ["src"], ["edit"])

    def test_explicit_dead_workspace_dismissal_releases_a_read_working_assignment(self):
        with self.assertRaisesRegex(runtime.CaptainError, "owned-path-conflict"):
            self.recruit()
        with (
            patch.object(sessions, "herdr", return_value={"workspaces": [{"workspace_id": "w2"}]}),
            patch.object(agents.runtime, "herdr") as api,
            patch.object(protocol, "unlanded", return_value=[]),
        ):
            agents.dismiss_crew(self.args, self.new_pane, self.project)
        api.assert_not_called()  # Never close a recycled pane ID in the new workspace.
        self.assertEqual(protocol.active_assignment(self.crew)["state"], "done")
        self.assertEqual(store.read_json(self.old.meta_path)["crew"]["jack"]["status"], "dismissed")
        self.recruit()

    def test_live_or_unverifiable_workspace_keeps_reservation(self):
        for response in (
            {"workspaces": [{"workspace_id": "w1"}, {"workspace_id": "w2"}]},
            {},
            {"workspaces": [{}]},
        ):
            with (
                self.subTest(response=response),
                patch.object(sessions, "herdr", return_value=response),
                self.assertRaisesRegex(runtime.CaptainError, "another project or Herdr workspace"),
            ):
                agents.dismiss_crew(self.args, self.new_pane, self.project)
        with (
            patch.object(sessions, "herdr", side_effect=runtime.CaptainError("offline")),
            self.assertRaisesRegex(runtime.CaptainError, "another project or Herdr workspace"),
        ):
            agents.dismiss_crew(self.args, self.new_pane, self.project)
        with self.assertRaisesRegex(runtime.CaptainError, "owned-path-conflict"):
            self.recruit()

    def managed(self):
        ref = lifetime.prepare(self.old, self.crew.record, ["native"], {}, self.project)
        self.crew.record["lifetime_id"] = ref
        store.write_json(self.old.meta_path, self.old.meta)
        lifetime.bind(self.old, ref, self.crew.record)
        with lifetime.claim(self.old, ref):
            pass  # Launched, with no exit receipt yet.
        store.write_json(
            self.old.directory / "captain.json",
            {"pane": "w1:p1", "terminal_id": "captain-terminal"},
        )
        return ref

    def exited(self, ref):
        path = self.old.directory / "lifetimes" / f"{ref}.json"
        record = store.read_json(path)
        record.update(
            state="exited",
            receipt_version=2,
            native_returncode=0,
            shutdown={
                "scope": "managed-process-group",
                "state": "complete",
                "guard_reaped": True,
                "group_absent": True,
                "escape_evidence": "descendant-scan",
                "escape_state": "none-observed",
            },
        )
        store.write_json(path, record)

    def dismiss_in_dead_workspace(self):
        with (
            patch.object(sessions, "herdr", return_value={"workspaces": []}),
            patch.object(
                agents.runtime, "herdr", side_effect=runtime.CaptainError("pane_not_found")
            ) as api,
            patch.object(lifetime, "_duration", return_value=0),
            patch.object(protocol, "unlanded", return_value=[]),
        ):
            try:
                agents.dismiss_crew(self.args, self.new_pane, self.project)
            finally:
                # Never close a recycled pane ID; the gone pane is only looked up.
                for call in api.call_args_list:
                    self.assertEqual(call.args[:2], ("pane", "get"))

    def test_dead_workspace_releases_a_managed_writer_only_once_stopped(self):
        ref = self.managed()
        with self.assertRaisesRegex(runtime.CaptainError, "cleanup-pending"):
            self.dismiss_in_dead_workspace()
        self.assertEqual(
            store.read_json(self.old.meta_path)["crew"]["jack"]["status"], "cleanup_pending"
        )
        with self.assertRaisesRegex(runtime.CaptainError, "owned-path-conflict"):
            self.recruit()
        self.exited(ref)
        self.dismiss_in_dead_workspace()
        self.assertEqual(lifetime.read(self.old, ref)["close_state"], "closed")
        self.assertFalse(lifetime.pending(self.old.directory))
        assignment = protocol.active_assignment(self.crew)
        self.assertEqual(assignment["report"], "Dismissed after Herdr workspace disappeared.")
        self.assertEqual(store.read_json(self.old.meta_path)["crew"]["jack"]["status"], "dismissed")
        self.recruit()

    def test_live_workspace_without_the_pane_requires_shutdown_proof(self):
        ref = self.managed()
        with (
            patch.object(sessions, "herdr", return_value={"workspaces": [{"workspace_id": "w1"}]}),
            patch.object(
                lifetime.runtime, "herdr", side_effect=runtime.CaptainError("pane_not_found")
            ) as api,
            patch.object(lifetime, "_duration", return_value=0),
        ):
            self.assertFalse(execution.stop(self.old, ref).ok)
            self.assertEqual(lifetime.read(self.old, ref)["close_state"], "failed")
            api.assert_not_called()
            with self.assertRaisesRegex(runtime.CaptainError, "owned-path-conflict"):
                self.recruit()
            self.exited(ref)
            self.assertTrue(execution.stop(self.old, ref).ok)
            api.assert_called_once_with("pane", "get", "w1:p2", timeout=5)
        self.assertEqual(lifetime.read(self.old, ref)["close_state"], "closed")

    def test_dead_workspace_dismissal_keeps_a_real_done_report(self):
        with protocol.checkpoint(self.old.directory) as state:
            protocol.active(state, self.crew).update(state="done", report="Edited src.")
        self.dismiss_in_dead_workspace()
        self.assertEqual(protocol.active_assignment(self.crew)["report"], "Edited src.")
        self.assertEqual(store.read_json(self.old.meta_path)["crew"]["jack"]["status"], "dismissed")

    def test_dead_workspace_still_protects_unlanded_work(self):
        with (
            patch.object(sessions, "herdr", return_value={"workspaces": []}),
            patch.object(protocol, "unlanded", return_value=["[uncommitted] src/a.py"]),
            self.assertRaisesRegex(runtime.CaptainError, "confirm-unlanded"),
        ):
            agents.dismiss_crew(self.args, self.new_pane, self.project)
        self.assertEqual(protocol.active_assignment(self.crew)["state"], "working")

    def test_other_commands_cannot_adopt_a_dead_workspace(self):
        with (
            patch.object(sessions, "herdr", return_value={"workspaces": []}) as api,
            self.assertRaisesRegex(runtime.CaptainError, "another project or Herdr workspace"),
        ):
            sessions.session(self.project, self.new_pane, self.old.meta["id"])
        api.assert_not_called()
