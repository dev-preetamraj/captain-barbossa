import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import lifetime, native_history, pi_history, sessions, store, usage
from captain_barbossa.crew import Crew
from captain_barbossa.runtime import CaptainError


class NativeHistoryCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.session = SimpleNamespace(directory=self.root / "session")
        self.session.directory.mkdir()
        self.session.events = lambda name: self.session.directory / "events" / f"{name}.jsonl"
        self.incarnation = uuid.uuid4().hex
        self.crew = Crew(
            "jack", {"provider": "codex", "incarnation_id": self.incarnation}, self.session
        )
        self.addCleanup(patch.stopall)
        patch("pathlib.Path.home", return_value=self.home).start()
        patch.dict(
            "os.environ",
            {
                "CODEX_HOME": str(self.home / ".codex"),
                "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                "GROK_HOME": str(self.home / ".grok"),
            },
        ).start()
        patch.object(native_history.shutil, "which", return_value="/mock/native").start()
        patch.object(usage, "_prices", return_value={}).start()
        self.lifetime_check = patch.object(
            lifetime, "check", return_value=lifetime.Verdict(True, "stopped")
        ).start()
        self.lifetime_read = patch.object(
            lifetime, "read", return_value={"argv": ["/mock/native", "exec", "--ephemeral", "task"]}
        ).start()

    def prepare(self, provider="codex", background=False):
        self.crew.record["provider"] = provider
        return native_history.prepare(
            self.crew, incarnation=self.incarnation, project=self.root, background=background
        )

    def record(self):
        return store.read_json(native_history._path(self.crew))

    def observe(self, session_id=None, **extra):
        session_id = session_id or str(uuid.uuid4())
        native_history.observe(
            self.crew,
            {
                "hook_event_name": "SessionStart",
                "incarnation_id": self.incarnation,
                "session_id": session_id,
                **extra,
            },
        )
        return session_id

    def test_prelaunch_policy_is_crew_only_and_does_not_replace_native_home(self):
        policy = self.prepare()
        self.assertEqual(policy["argv"], ["-c", 'history.persistence="none"'])
        self.assertEqual(policy["env"]["CODEX_HOME"], str(self.home / ".codex"))
        self.assertEqual(self.record()["state"], "pending")
        self.assertTrue(native_history.pending(self.session.directory))
        with self.assertRaises(CaptainError):
            self.prepare()
        self.crew.crew_id = "captain"
        with self.assertRaises(CaptainError):
            self.prepare()

    def test_running_owner_cannot_delete(self):
        self.prepare()
        self.observe()
        self.lifetime_check.return_value = lifetime.Verdict(False, "owner-running")
        with patch.object(native_history.subprocess, "run") as run:
            self.assertEqual(native_history.cleanup(self.crew)["state"], "pending")
            run.assert_not_called()
        self.assertFalse(self.crew.events.with_suffix(".usage.json").exists())

    def test_missing_lifetime_cannot_delete(self):
        self.prepare()
        self.observe()
        with patch.object(lifetime, "check", return_value=lifetime.Verdict(False, "missing")):
            with patch.object(native_history.subprocess, "run") as run:
                result = native_history.cleanup(self.crew)
                self.assertEqual(result["state"], "pending")
                run.assert_not_called()

    def test_legacy_observe_is_a_noop_without_creating_state(self):
        self.crew.record.pop("incarnation_id")
        native_history.observe(self.crew, {"hook_event_name": "SessionStart"})
        self.assertFalse((self.session.directory / "native-history").exists())

    def test_foreground_never_accepts_background_evidence(self):
        self.prepare(background=False)
        self.observe()
        self.lifetime_check.return_value = lifetime.Verdict(False, "unresolved-close")
        with patch.object(native_history.subprocess, "run") as run:
            result = native_history.cleanup(self.crew)
            self.assertEqual(result["state"], "pending")
            run.assert_not_called()

    def test_codex_freezes_before_exact_native_delete_and_retry_is_idempotent(self):
        self.prepare()
        identity = self.observe()

        def delete(argv, **kwargs):
            self.assertTrue(self.crew.events.with_suffix(".usage.json").exists())
            self.assertEqual(argv, ["/mock/native", "delete", identity, "--force"])
            self.assertEqual(kwargs["env"]["CODEX_HOME"], str(self.home / ".codex"))
            return SimpleNamespace(returncode=0)

        with patch.object(native_history.subprocess, "run", side_effect=delete) as run:
            self.assertEqual(
                native_history.cleanup(self.crew)["state"],
                "complete",
            )
            native_history.cleanup(self.crew)
            self.assertEqual(run.call_count, 1)
        self.lifetime_check.assert_called_once_with(
            self.session,
            self.incarnation,
            crew_id="jack",
            incarnation_id=self.incarnation,
            root=str(self.root),
            worktree=None,
            closure=False,
            scope="native",
        )
        self.assertFalse(native_history.pending(self.session.directory))

    def test_failed_native_delete_remains_retryable(self):
        self.prepare()
        self.observe()
        with patch.object(
            native_history.subprocess, "run", return_value=SimpleNamespace(returncode=1)
        ):
            self.assertEqual(
                native_history.cleanup(self.crew)["state"],
                "partial",
            )
        self.assertTrue(native_history.pending(self.session.directory))
        self.assertEqual(self.record()["deleted_ids"], [])

    def test_resumed_or_changed_identity_is_not_owned(self):
        self.prepare()
        self.observe(source="resume")
        with patch.object(native_history.subprocess, "run") as run:
            native_history.cleanup(self.crew)
            run.assert_not_called()
        self.assertTrue(self.record()["ambiguous"])

    def test_external_resume_never_enters_deletion_ownership(self):
        self.prepare()
        external_id = str(uuid.uuid4())
        transcript = self.home / ".codex" / "sessions" / "external" / f"{external_id}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("unrelated session")
        self.observe(external_id, source="resume", transcript_path=str(transcript))
        self.assertEqual(self.record()["session_ids"], [])
        self.assertEqual(self.record()["transcripts"], [])
        with patch.object(native_history.subprocess, "run") as run:
            result = native_history.cleanup(self.crew)
            self.assertIn(result["state"], {"pending", "partial"})
            run.assert_not_called()
        self.assertEqual(transcript.read_text(), "unrelated session")

    def test_trusted_root_aliases_are_pinned_before_subtree_checks(self):
        alias = self.root / "var-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.session.directory = alias / "session"
        with patch.dict("os.environ", {"CODEX_HOME": str(alias / "home" / ".codex")}):
            policy = self.prepare()
        self.assertEqual(policy["env"]["CODEX_HOME"], str(self.home / ".codex"))
        self.assertEqual(Path(self.record()["events"]).parent, self.root / "session" / "events")
        self.assertTrue(native_history.pending(self.session.directory))
        identity = str(uuid.uuid4())
        transcript = alias / "home" / ".codex" / "sessions" / f"rollout-{identity}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n")
        self.observe(identity, source="startup", transcript_path=str(transcript))
        self.assertEqual(self.record()["transcripts"], [str(transcript.resolve())])
        self.assertFalse(self.record()["ambiguous"])

    def test_symlink_inside_pinned_native_root_is_not_an_alias(self):
        self.prepare()
        native_root = self.home / ".codex"
        native_root.mkdir()
        outside = self.root / "unrelated"
        outside.mkdir()
        (native_root / "sessions").symlink_to(outside, target_is_directory=True)
        identity = str(uuid.uuid4())
        external = outside / f"rollout-{identity}.jsonl"
        external.write_text("unrelated")
        self.observe(
            identity,
            source="startup",
            transcript_path=str(native_root / "sessions" / external.name),
        )
        self.assertTrue(self.record()["ambiguous"])
        with patch.object(native_history.subprocess, "run") as run:
            native_history.cleanup(self.crew)
            run.assert_not_called()
        self.assertEqual(external.read_text(), "unrelated")

    def test_symlink_root_substitution_blocks_native_delete(self):
        self.prepare()
        self.observe()
        other = self.root / "other"
        other.mkdir()
        (self.home / ".codex").symlink_to(other, target_is_directory=True)
        with patch.object(native_history.subprocess, "run") as run:
            self.assertEqual(
                native_history.cleanup(self.crew)["state"],
                "partial",
            )
            run.assert_not_called()

    def test_claude_only_removes_exact_owned_transcript_and_reports_partial(self):
        self.prepare("claude")
        identity = self.record()["session_ids"][0]
        directory = self.home / ".claude" / "projects" / "project"
        directory.mkdir(parents=True)
        transcript = directory / f"{identity}.jsonl"
        transcript.write_text("{}\n")
        unrelated = directory / "other.jsonl"
        unrelated.write_text("private")
        recall = self.home / ".claude" / "history.jsonl"
        recall.write_text("shared prompt recall")
        sidecar = transcript.with_suffix("")
        sidecar.mkdir()
        self.observe(identity, transcript_path=str(transcript))
        result = native_history.cleanup(self.crew)
        self.assertEqual(result["state"], "partial")
        self.assertFalse(transcript.exists())
        self.assertEqual(unrelated.read_text(), "private")
        self.assertEqual(result["conversation_cleanup"], "removed")
        self.assertEqual(result["remaining_artifacts"]["shared_prompt_recall"], str(recall))
        self.assertEqual(result["remaining_artifacts"]["session_sidecars"], [str(sidecar)])
        self.assertEqual(recall.read_text(), "shared prompt recall")
        self.assertTrue(sidecar.is_dir())

    def test_pi_background_preserves_no_session_and_unknown_usage(self):
        self.lifetime_read.return_value = {"argv": ["/mock/native", "-p", "--no-session", "task"]}
        self.assertEqual(self.prepare("pi", background=True)["argv"], [])
        self.assertEqual(native_history.cleanup(self.crew)["state"], "complete")
        self.assertIsNone(usage.usage_for_events(self.crew.events))

    def test_ephemeral_codex_without_hooks_settles_after_lifecycle_and_accounting(self):
        self.prepare(background=True)
        with patch.object(native_history.subprocess, "run") as run:
            result = native_history.cleanup(self.crew)
            run.assert_not_called()
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["history_policy"], "ephemeral")
        self.assertEqual(result["accounting"], "unknown")
        self.assertEqual(result["session_ids"], [])
        self.assertTrue(self.crew.events.with_suffix(".usage.json").exists())
        self.assertFalse(native_history.pending(self.session.directory))

    def test_ephemeral_policy_is_verified_from_actual_argv(self):
        self.prepare(background=True)
        self.lifetime_read.return_value = {"argv": ["/mock/native", "exec", "--ephemeral"]}
        result = native_history.cleanup(self.crew)
        self.assertEqual(result["state"], "pending")
        self.assertFalse(self.crew.events.with_suffix(".usage.json").exists())

    def test_legacy_background_manifest_can_verify_actual_no_history_launch(self):
        self.prepare(background=True)
        record = self.record()
        del record["history_policy"]
        store.write_json(native_history._path(self.crew), record)
        result = native_history.cleanup(self.crew)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["session_ids"], [])

    def grok_launch(self):
        self.prepare("grok")
        identity = self.record()["session_ids"][0]
        self.lifetime_read.return_value = {
            "argv": ["/mock/native", "--no-subagents", "--session-id", identity, "task"],
            "state": "exited",
            "receipt_version": 2,
            "native_returncode": 0,
        }
        return identity

    def test_grok_deletes_only_initial_proven_uuid_in_recorded_context(self):
        native_home = self.root / "custom-grok"
        with patch.dict("os.environ", {"GROK_HOME": str(native_home)}):
            identity = self.grok_launch()

            def delete(argv, **kwargs):
                self.assertEqual(argv, ["/mock/native", "sessions", "delete", identity])
                self.assertEqual(kwargs["env"]["GROK_HOME"], str(native_home))
                self.assertEqual(kwargs["cwd"], str(self.root))
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(kwargs["timeout"], 30)
                self.assertTrue(self.crew.events.with_suffix(".usage.json").exists())
                return SimpleNamespace(returncode=0)

            with patch.object(native_history.subprocess, "run", side_effect=delete) as run:
                result = native_history.cleanup(self.crew)
                native_history.cleanup(self.crew)
                self.assertEqual(run.call_count, 1)
        self.assertEqual(result["deleted_ids"], [identity])
        self.assertEqual(result["state"], "partial")
        self.assertEqual(result["conversation_cleanup"], "native-reported-success")
        self.assertTrue(native_history.pending(self.session.directory))

    def test_grok_preserves_explicit_original_leader_socket(self):
        identity = self.grok_launch()
        self.lifetime_read.return_value["argv"][1:1] = ["--leader-socket", "local/leader.sock"]
        with patch.object(
            native_history.subprocess, "run", return_value=SimpleNamespace(returncode=0)
        ) as run:
            result = native_history.cleanup(self.crew)
        self.assertEqual(
            run.call_args.args[0],
            [
                "/mock/native",
                "sessions",
                "delete",
                "--leader-socket",
                "local/leader.sock",
                identity,
            ],
        )
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.root))
        self.assertEqual(result["state"], "partial")

    def test_grok_ambiguous_leader_socket_refuses_deletion(self):
        self.grok_launch()
        self.lifetime_read.return_value["argv"][1:1] = [
            "--leader-socket",
            "first.sock",
            "--leader-socket=second.sock",
        ]
        with patch.object(native_history.subprocess, "run") as run:
            self.assertEqual(native_history.cleanup(self.crew)["state"], "pending")
            run.assert_not_called()

    def test_grok_resumed_launch_does_not_authorize_deletion(self):
        self.grok_launch()
        self.lifetime_read.return_value["argv"].insert(1, "--resume")
        with patch.object(native_history.subprocess, "run") as run:
            result = native_history.cleanup(self.crew)
            run.assert_not_called()
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["deleted_ids"], [])

    def test_grok_failed_initial_launch_is_not_proof_of_session_creation(self):
        self.grok_launch()
        self.lifetime_read.return_value["native_returncode"] = 1
        with patch.object(native_history.subprocess, "run") as run:
            self.assertEqual(native_history.cleanup(self.crew)["state"], "pending")
            run.assert_not_called()

    def test_grok_changed_home_context_does_not_authorize_deletion(self):
        self.grok_launch()
        with patch.dict("os.environ", {"GROK_HOME": str(self.root / "different")}):
            with patch.object(native_history.subprocess, "run") as run:
                result = native_history.cleanup(self.crew)
                run.assert_not_called()
        self.assertEqual(result["state"], "pending")

    def test_grok_nonzero_and_timeout_remain_pending_without_inferred_absence(self):
        self.grok_launch()
        with patch.object(
            native_history.subprocess, "run", return_value=SimpleNamespace(returncode=1)
        ):
            self.assertEqual(native_history.cleanup(self.crew)["state"], "pending")
        with patch.object(
            native_history.subprocess, "run", side_effect=subprocess.TimeoutExpired("grok", 30)
        ):
            self.assertEqual(native_history.cleanup(self.crew)["state"], "pending")
        self.assertEqual(self.record()["deleted_ids"], [])

    def test_grok_changed_observed_identity_is_not_adopted(self):
        identity = self.grok_launch()
        self.observe(str(uuid.uuid4()), source="resume")
        with patch.object(native_history.subprocess, "run") as run:
            result = native_history.cleanup(self.crew)
            run.assert_not_called()
        self.assertEqual(result["session_ids"], [identity])
        self.assertEqual(result["state"], "partial")

    def test_pi_extension_records_identity_and_blocks_outside_resume(self):
        policy = self.prepare("pi", background=False)
        self.assertIn("--session-dir", policy["argv"])
        extension = native_history._path(self.crew).with_suffix(".ts").read_text()
        self.assertIn("session_start", extension)
        self.assertIn("session_before_switch", extension)
        self.assertIn("return { cancel: true }", extension)
        self.assertIn(json.dumps(self.incarnation), extension)
        self.assertEqual(
            extension,
            pi_history.extension(
                self.record()["native_root"],
                native_history._path(self.crew).with_suffix(".pi.jsonl"),
                self.incarnation,
            ),
        )

    def test_pi_boundary_handlers_with_mock_native_context(self):
        self.prepare("pi", background=False)
        path = native_history._path(self.crew)
        script = """
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
const [extension, root, events] = process.argv.slice(1);
const mod = await import('data:text/javascript,' + encodeURIComponent(readFileSync(extension, 'utf8')));
const handlers = {};
mod.default({ on: (kind, handler) => { handlers[kind] = handler; } });
const inside = join(root, 'inside.jsonl');
writeFileSync(inside, '');
assert.deepEqual(await handlers.session_before_switch({reason:'resume', targetSessionFile:extension}), {cancel:true});
assert.equal(await handlers.session_before_switch({reason:'resume', targetSessionFile:inside}), undefined);
assert.equal(await handlers.session_before_switch({reason:'new'}), undefined);
await handlers.session_start({}, {sessionManager: {
  getSessionId: () => 'owned', getSessionFile: () => inside
}});
const event = JSON.parse(readFileSync(events, 'utf8'));
assert.equal(event.provider, 'pi');
assert.equal(event.session_id, 'owned');
assert.equal(event.transcript_path, inside);
"""
        try:
            subprocess.run(
                [
                    "node",
                    "--input-type=module",
                    "-e",
                    script,
                    str(path.with_suffix(".ts")),
                    self.record()["native_root"],
                    str(path.with_suffix(".pi.jsonl")),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            self.skipTest("Node is unavailable for the isolated pi extension fixture")

    def test_retained_incarnation_blocks_pruning_after_name_reuse(self):
        self.prepare()
        self.crew.record["incarnation_id"] = uuid.uuid4().hex
        self.assertTrue(native_history.pending(self.session.directory))


class ManagedGrokChainTests(SessionCase):
    """The real grok preparation chain and its real v2 exit receipt, without mocks."""

    def setUp(self):
        super().setUp()
        self.session = sessions.session(
            self.project,
            {"workspace_id": "workspace", "pane_id": "captain"},
            session_id="a" * 32,
            create=True,
        )
        self.record = {"id": "jack", "provider": "grok", "incarnation_id": "b" * 32}
        self.session.meta["crew"]["jack"] = self.record
        store.write_json(self.session.meta_path, self.session.meta)
        self.crew = Crew("jack", self.record, self.session)
        self.enterContext(
            patch.dict(
                "os.environ",
                {
                    "GROK_HOME": str(self.root / "grok"),
                    "XDG_CONFIG_HOME": str(self.root / "config"),
                },
            )
        )
        self.enterContext(patch.object(native_history.shutil, "which", return_value="/mock/grok"))
        self.enterContext(patch.object(usage, "_prices", return_value={}))

    def test_grok_launch_prepares_and_v2_receipt_authorizes_initial_delete(self):
        history = native_history.prepare(
            self.crew, incarnation=self.record["incarnation_id"], project=self.project
        )
        self.assertEqual(set(history["env"]), {"GROK_HOME", "HOME", "XDG_CONFIG_HOME"})
        ref = lifetime.prepare(
            self.session,
            self.record,
            ["/mock/grok", *history["argv"]],
            {"CAPTAIN_CREW": "jack", **history["env"]},
            self.project,
        )
        with lifetime.claim(self.session, ref) as job:
            # The exact state shutdown leaves after a reaped guard and an absent group.
            job._guard, job._reaped = object(), True
            job.native_returncode = 0
            job._shutdown = lifetime.Verdict(True, "group-absent")
            self.assertTrue(job.finish().ok)
        lifetime.revoke(self.session, ref)
        receipt = lifetime.read(self.session, ref)
        self.assertNotIn("returncode", receipt)
        with patch.object(
            native_history.subprocess, "run", return_value=SimpleNamespace(returncode=0)
        ) as run:
            result = native_history.cleanup(self.crew)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][:3], ["/mock/grok", "sessions", "delete"])
        self.assertEqual(result["deleted_ids"], result["session_ids"])
