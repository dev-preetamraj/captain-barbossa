"""Run the shipped pi extension with Node, without a model or Herdr session."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_barbossa import agents, cli, models, runtime, sessions
from captain_barbossa import instructions as instruction_prompts
from captain_barbossa import pane as panes
from captain_barbossa.pane import Pane
from captain_barbossa.pi_captain import captain_extension
from tests.home_isolation import HERDR, SessionCase

HARNESS = r"""
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";
import { mock } from "node:test";
mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"] });
const path = process.argv[2];
const { default: extension } = await import(pathToFileURL(path));
const calls = [], messages = [], notices = [];
const history = [];
let recordMessages = true;
let tool, shutdown, start;
const pi = {
  on(event, handler) {
    if (event === "session_start") start = handler;
    else { assert.equal(event, "session_shutdown"); shutdown = handler; }
  },
  registerTool(value) { tool = value; },
  exec(command, args, options) {
    return new Promise((resolve, reject) => calls.push({ command, args, options, resolve, reject }));
  },
  sendMessage(message, options) {
    messages.push({ message, options });
    if (recordMessages) history.push({ type: "custom_message", ...message });
  },
};
extension(pi);
assert.equal(tool.name, "captain_wait");
assert.equal(calls.length, 0);
const turn = new AbortController();
const arm = (name = "Jack", timeout, cancel = false) => tool.execute("id", { name, timeout, cancel },
  turn.signal, undefined, { ui: { notify: (...args) => notices.push(args) },
    sessionManager: { getBranch: () => history } });
const flush = () => new Promise(resolve => setImmediate(resolve));
const result = (summary = "focused tests passed", status = "done", delivery_id = "d1", crew = "Jack",
  assignment_id = "a1") => ({ stdout: JSON.stringify({ status, delivery_id, crew, assignment_id,
    summary }), stderr: "", code: 0 });
const tick = async (ms = 1000) => { mock.timers.tick(ms); await flush(); };
const ack = async () => { calls.at(-1).resolve(result("", "timeout", null)); await flush(); };
"""


@unittest.skipUnless(shutil.which("node"), "Node is required to execute pi extensions")
class PiCaptainTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.extension = captain_extension(self.directory)

    def run_js(self, script):
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-", str(self.extension)],
            input=HARNESS + script,
            cwd=self.directory,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wait_returns_immediately_deduplicates_and_delivers_to_pi(self):
        self.run_js(r"""
const armed = await arm();
assert.match(armed.content[0].text, /Wait armed/);
assert.equal(messages.length, 0);
await arm("jack");
await arm("Will", 7);
assert.equal(calls.length, 2);
assert.deepEqual(calls[0].args.slice(0, 3), ["-m", "captain_barbossa", "--session"]);
assert.deepEqual(calls[0].args.slice(-5), ["wait", "Jack", "--json", "--timeout", "60"]);
assert.deepEqual(calls[1].args.slice(-5), ["wait", "Will", "--json", "--timeout", "60"]);
assert.equal(calls[1].options.timeout, 70000);
turn.abort();
assert.equal(calls[0].options.signal.aborted, false);
calls[0].resolve(result());
await flush();
assert.equal(messages.length, 1);
assert.equal(messages[0].message.customType, "captain-wait");
assert.equal(messages[0].message.display, true);
assert.match(messages[0].message.content, /focused tests passed/);
assert.deepEqual(messages[0].options, { triggerTurn: true, deliverAs: "steer" });
assert.deepEqual(calls[2].args.slice(-4), ["--timeout", "0", "--ack", "d1"]);
shutdown();
assert.equal(calls[1].options.signal.aborted, true);
calls[1].resolve({ stdout: "late result", stderr: "", code: 0 });
await flush();
assert.equal(messages.length, 1);
assert.deepEqual(notices, []);
""")

    def test_blocked_followup_timeout_and_failure_can_each_be_rearmed(self):
        self.run_js(r"""
for (const [status, summary] of [["awaiting_approval", "permission needed"],
  ["done", "follow-up finished"]]) {
  await arm();
  calls.at(-1).resolve(result(summary, status, status));
  await flush();
  assert.ok(messages.at(-1).message.content.includes(summary));
  await ack();
  await arm("Jack", undefined, true);
}
assert.match(messages[0].message.content, /permission needed/);
assert.match(messages[1].message.content, /follow-up finished/);
for (const output of [{ stdout: "", stderr: "CLI failed", code: 1 },
  { stdout: "", stderr: "", code: 1, killed: true },
  { stdout: "not JSON", stderr: "", code: 0 }, result("", "bogus"),
  result("", "done", "id", "Will")]) {
  await arm();
  calls.at(-1).resolve(output);
  await flush();
  assert.equal(messages.length, 2);
  assert.match(notices.at(-1)[0], /stopped.*rearm captain_wait/);
}
await arm();
calls.at(-1).reject(new Error("spawn failed"));
await flush();
assert.match(notices.at(-1)[0], /spawn failed/);
await arm();
shutdown();
shutdown();
calls.at(-1).reject(new Error("cancelled"));
await flush();
assert.equal(messages.length, 2);
assert.equal(notices.length, 6);
""")

    def test_full_output_is_preserved_privately_and_context_is_bounded(self):
        self.run_js(r"""
const output = "crew report\n" + "x".repeat(20000) + "final line";
await arm();
calls[0].resolve(result(output));
await flush();
// The whole steered message, wrapper included, is what costs the captain context.
assert.ok(messages[0].message.content.length <= 2000);
assert.match(messages[0].message.content, /crew report/);
assert.match(messages[0].message.content, /Output truncated/);
const [log] = readdirSync(dirname(path)).filter(name => name.startsWith("pi-wait-"));
const logPath = join(dirname(path), log);
assert.equal(JSON.parse(readFileSync(logPath, "utf8").split("\n")[0]).summary, output);
assert.equal(statSync(logPath).mode & 0o777, 0o600);
assert.ok(messages[0].message.content.includes(logPath));
""")

    def test_a_background_wait_shorter_than_the_floor_is_raised_to_it(self):
        self.run_js(r"""
for (const [crew, timeout] of [["Jack", 0], ["Will", 5], ["Gibbs", 60], ["Cotton", 120]]) {
  await arm(crew, timeout);
  const armed = Number(calls.at(-1).args.at(-1));
  assert.equal(armed, 60);
  assert.equal(calls.at(-1).options.timeout, 70000);
}
""")

    def test_short_output_is_delivered_whole_without_a_truncation_marker(self):
        self.run_js(r"""
await arm();
calls[0].resolve(result("reported: done", "done"));
await flush();
assert.match(messages[0].message.content, /reported: done/);
assert.doesNotMatch(messages[0].message.content, /Output truncated/);
""")

    def test_cancelled_tool_does_not_start_a_wait(self):
        self.run_js(r"""
turn.abort();
await assert.rejects(arm(), /abort/i);
assert.equal(calls.length, 0);
""")

    def test_timeouts_and_unchanged_states_rearm_without_model_turns(self):
        self.run_js(r"""
await arm("Jack", 60);
for (const status of ["timeout", "working", "idle", "done"]) {
  const before = calls.length;
  calls.at(-1).resolve(result("unchanged", status, null));
  await flush();
  assert.equal(calls.length, before);
  await tick();
  assert.equal(calls.length, before + 1);
  assert.equal(messages.length, 0);
}
await tick(60000);
calls.at(-1).resolve(result("", "timeout", null));
await flush();
await tick();
assert.equal(messages.length, 0);
assert.match(notices.at(-1)[0], /expired/);
const before = calls.length;
await arm();
assert.equal(calls.length, before + 1);
await arm("Jack", undefined, true);
assert.equal(calls.at(-1).options.signal.aborted, true);
calls.at(-1).resolve(result());
await flush();
assert.equal(messages.length, 0);
""")

    def test_ack_waits_for_ingestion_and_duplicate_ids_survive_reload(self):
        self.run_js(r"""
recordMessages = false;
await arm();
calls[0].resolve(result());
await flush();
assert.equal(messages.length, 1);
assert.equal(calls.length, 1); // Queued is not delivered.
history.push({ type: "custom_message", ...messages[0].message });
await tick(100);
assert.deepEqual(calls.at(-1).args.slice(-2), ["--ack", "d1"]);
await ack();
shutdown();
extension(pi); // New extension state, same private delivery journal.
await arm();
calls.at(-1).resolve(result());
await flush();
assert.equal(messages.length, 1);
assert.deepEqual(calls.at(-1).args.slice(-2), ["--ack", "d1"]);
await ack();
await tick();
recordMessages = true;
calls.at(-1).resolve(result("new assignment done", "done", "d2", "Jack", "a2"));
await flush();
assert.equal(messages.length, 2);
assert.equal(messages[1].message.details.assignment_id, "a2");
""")

    def test_activity_events_are_acknowledged_without_messages_or_model_turns(self):
        self.run_js(r"""
recordMessages = false; // Telemetry never depends on pi history ingestion.
await arm();
for (const status of ["working", "working", "idle", "idle"]) {
  calls.at(-1).resolve(result("native activity only", status, status));
  await flush();
  assert.equal(messages.length, 0);
  assert.equal(messages.filter(entry => entry.options.triggerTurn).length, 0);
  assert.deepEqual(calls.at(-1).args.slice(-2), ["--ack", status]);
  await ack();
  await tick();
}
assert.deepEqual(history, []);
assert.equal(readdirSync(dirname(path)).some(name => name.startsWith("pi-wait-")), false);
assert.deepEqual(notices, []);
""")

    def test_unknown_send_is_retained_without_resend_or_ack(self):
        self.run_js(r"""
recordMessages = false;
await arm();
calls[0].resolve(result());
await flush();
await tick(30000);
assert.equal(messages.length, 1);
assert.equal(calls.length, 1);
assert.match(notices.at(-1)[0], /uncertain/);
shutdown();
extension(pi);
await arm();
calls.at(-1).resolve(result());
await flush();
assert.equal(messages.length, 1);
assert.equal(calls.some(call => call.args.includes("--ack")), false);
const [log] = readdirSync(dirname(path)).filter(name => name.startsWith("pi-wait-"));
assert.equal(JSON.parse(readFileSync(join(dirname(path), log), "utf8")).delivery_id, "d1");
assert.match(notices.at(-1)[0], /not resent or acknowledged/);
""")

    def test_send_throw_and_ack_failure_do_not_cause_redelivery(self):
        self.run_js(r"""
pi.sendMessage = () => { throw new Error("notification failed"); };
await arm();
calls[0].resolve(result());
await flush();
assert.equal(calls.length, 1);
assert.match(notices.at(-1)[0], /notification failed/);
await arm();
calls.at(-1).resolve(result());
await flush();
assert.equal(calls.length, 2);
assert.match(notices.at(-1)[0], /uncertain/);
pi.sendMessage = (message, options) => {
  messages.push({ message, options });
  history.push({ type: "custom_message", ...message });
};
await arm();
calls.at(-1).resolve(result("next", "done", "d2"));
await flush();
calls.at(-1).reject(new Error("ack timeout"));
await flush();
assert.match(notices.at(-1)[0], /ack timeout/);
await arm();
calls.at(-1).resolve(result("next", "done", "d2"));
await flush();
assert.equal(messages.length, 1);
assert.deepEqual(calls.at(-1).args.slice(-2), ["--ack", "d2"]);
""")

    def test_cancel_during_receipt_wait_retains_unknown_outcome(self):
        self.run_js(r"""
recordMessages = false;
await arm();
calls[0].resolve(result());
await flush();
await arm("Jack", undefined, true);
await flush();
await tick(30000);
assert.equal(messages.length, 1);
assert.equal(calls.length, 1);
assert.deepEqual(notices, []);
await arm();
calls.at(-1).resolve(result());
await flush();
assert.equal(messages.length, 1);
assert.match(notices.at(-1)[0], /uncertain/);
""")

    def test_background_crew_watches_arm_themselves_and_survive_reconnect(self):
        self.run_js(r"""
const { writeFileSync } = await import("node:fs");
const ctx = { ui: { notify: (...args) => notices.push(args) },
  sessionManager: { getBranch: () => history } };
const crew = (execution, status = "started") => ({ incarnation_id: "i1", execution, status });
const assignment = (state, ack_seq = 0, notices = []) => ({ incarnation_id: "i1", state,
  pending: null, ack_seq, notices });
const save = (roster, assignments) => {
  writeFileSync(join(dirname(path), "session.json"), JSON.stringify({ crew: roster }));
  writeFileSync(join(dirname(path), "protocol.json"), JSON.stringify({
    active: Object.fromEntries(Object.keys(assignments).map(id => [id, id])), assignments }));
};
save({ jack: crew("background"), will: crew(undefined), gibbs: crew("background", "dismissed"),
  cotton: crew("background") },
  { jack: assignment("working"), will: assignment("working"), gibbs: assignment("working"),
    cotton: assignment("done", 1, [{}]) });
start({ type: "session_start", reason: "startup" }, ctx);
// Only the unresolved, live background crew is watched; nothing was asked of the model.
assert.equal(calls.length, 1);
assert.deepEqual(calls[0].args.slice(-5), ["wait", "jack", "--json", "--timeout", "60"]);
calls[0].resolve(result("found the slow path", "done", "d1", "Jack"));
await flush();
assert.match(messages[0].message.content, /found the slow path/);
assert.deepEqual(calls[1].args.slice(-2), ["--ack", "d1"]);
calls[1].resolve(result("", "idle", null, "Jack"));
await flush();
// Acknowledged done ends the watch; the next sweep finds nothing left to arm.
save({ jack: crew("background") }, { jack: assignment("done", 1, [{}]) });
await tick(5000);
assert.equal(calls.length, 2);
assert.deepEqual(notices, []);
// Reconnect: a new extension runtime restores the watch for an unresolved assignment.
shutdown();
save({ jack: crew("background") }, { jack: assignment("done", 0, [{}]) });
extension(pi);
start({ type: "session_start", reason: "resume" }, ctx);
assert.equal(calls.length, 3);
calls[2].resolve({ stdout: "", stderr: "boom", code: 1 });
await flush();
assert.match(notices.at(-1)[0], /boom/);
// A failed auto watch is reported once, not rearmed on every sweep.
await tick(5000);
assert.equal(calls.length, 3);
shutdown();
""")

    def test_background_auto_sweep_uses_persisted_profile_effective_execution(self):
        self.run_js(r"""
const { writeFileSync } = await import("node:fs");
const ctx = { ui: { notify: (...args) => notices.push(args) },
  sessionManager: { getBranch: () => history } };
const record = { incarnation_id: "i1", provider: "codex", execution: "background",
  status: "started", profile: "nightly",
  effective: { agent: "codex", model: "gpt-test", execution: "background" },
  sources: { execution: "[profiles.nightly] execution from settings" } };
const persisted = JSON.stringify({ crew: { jack: record } });
assert.doesNotMatch(persisted, /--background/);
writeFileSync(join(dirname(path), "session.json"), persisted);
writeFileSync(join(dirname(path), "protocol.json"), JSON.stringify({ active: { jack: "a1" },
  assignments: { a1: { incarnation_id: "i1", state: "working", pending: null,
    ack_seq: 0, notices: [] } } }));
start({ type: "session_start", reason: "startup" }, ctx);
assert.equal(calls.length, 1);
assert.deepEqual(calls[0].args.slice(-5), ["wait", "jack", "--json", "--timeout", "60"]);
shutdown();
""")

    def test_an_explicit_cancel_is_never_reversed_by_the_automatic_sweep(self):
        self.run_js(r"""
const { writeFileSync } = await import("node:fs");
const ctx = { ui: { notify: (...args) => notices.push(args) },
  sessionManager: { getBranch: () => history } };
const save = assignments => {
  writeFileSync(join(dirname(path), "session.json"), JSON.stringify({ crew: {
    jack: { incarnation_id: "i1", execution: "background", status: "started" } } }));
  writeFileSync(join(dirname(path), "protocol.json"), JSON.stringify({
    active: { jack: "x" }, assignments }));
};
save({});
start({ type: "session_start", reason: "startup" }, ctx);
assert.equal(calls.length, 0);
await arm("jack");  // armed by hand before the sweep would arm it
assert.equal(calls.length, 1);
save({ x: { incarnation_id: "i1", state: "working", pending: null, ack_seq: 0, notices: [] } });
await arm("jack", undefined, true);
assert.equal(calls[0].options.signal.aborted, true);
await tick(5000);
await tick(5000);
assert.equal(calls.length, 1);
await arm("jack");  // an explicit rearm lifts it
assert.equal(calls.length, 2);
shutdown();
""")

    def test_stopped_watches_do_not_suppress_a_new_incarnation_of_the_same_name(self):
        for stop in ("failure", "cancel"):
            with self.subTest(stop=stop):
                self.run_js(
                    f"const stop = {json.dumps(stop)};\n"
                    + r"""
const { writeFileSync } = await import("node:fs");
const ctx = { ui: { notify: (...args) => notices.push(args) },
  sessionManager: { getBranch: () => history } };
const save = incarnation => {
  writeFileSync(join(dirname(path), "session.json"), JSON.stringify({ crew: {
    jack: { incarnation_id: incarnation, execution: "background", status: "started" } } }));
  writeFileSync(join(dirname(path), "protocol.json"), JSON.stringify({
    active: { jack: incarnation }, assignments: {
      [incarnation]: { incarnation_id: incarnation, state: "working", pending: null,
        ack_seq: 0, notices: [] } } }));
};
save("old");
start({ type: "session_start", reason: "startup" }, ctx);
assert.equal(calls.length, 1);
if (stop === "failure") {
  calls[0].resolve({ stdout: "", stderr: "boom", code: 1 });
} else {
  await arm("Jack", undefined, true);
  assert.equal(calls[0].options.signal.aborted, true);
  calls[0].reject(new Error("cancelled"));
}
await flush();
await tick(5000);
assert.equal(calls.length, 1); // The old assignment stays stopped.
save("new"); // Dismiss old Jack and recruit another incarnation with a fresh assignment.
await tick(5000);
assert.equal(calls.length, 2);
assert.deepEqual(calls[1].args.slice(-5), ["wait", "jack", "--json", "--timeout", "60"]);
calls[1].resolve(result("new report", "done", `new-${stop}`, "Jack", "new"));
await flush();
assert.equal(messages.length, 1);
assert.equal(messages[0].message.details.assignment_id, "new");
assert.deepEqual(calls.at(-1).args.slice(-2), ["--ack", `new-${stop}`]);
calls.at(-1).resolve(result("", "idle", null));
await flush();
shutdown();
"""
                )

    def test_auto_cancel_then_immediate_rearm_is_not_undone_by_late_abort(self):
        self.run_js(r"""
const { writeFileSync } = await import("node:fs");
const ctx = { ui: { notify: (...args) => notices.push(args) },
  sessionManager: { getBranch: () => history } };
writeFileSync(join(dirname(path), "session.json"), JSON.stringify({ crew: {
  jack: { incarnation_id: "i1", execution: "background", status: "started" } } }));
writeFileSync(join(dirname(path), "protocol.json"), JSON.stringify({ active: { jack: "a1" },
  assignments: { a1: { incarnation_id: "i1", state: "working", pending: null,
    ack_seq: 0, notices: [] } } }));
start({}, ctx);
await arm("Jack", undefined, true);
await tick(5000);
assert.equal(calls.length, 1);
await arm("Jack", 60);
assert.equal(calls.length, 2);
calls[0].reject(new Error("late abort"));
await flush();
await tick(60000);
calls[1].resolve(result("", "timeout", null));
await flush();
await tick(1000);
await tick(5000);
assert.equal(calls.length, 3); // Manual rearm expired; automatic renewal still works.
assert.equal(notices.filter(([, level]) => level === "error").length, 0);
shutdown();
""")

    def test_a_delivery_ingested_after_the_warning_is_acknowledged_without_resend(self):
        # The live incident: the captain sat in a 45s tool call, so the steered delivery
        # reached history about 40s after it was sent and the watch had already given up.
        self.run_js(r"""
const { writeFileSync } = await import("node:fs");
const ctx = { ui: { notify: (...args) => notices.push(args) },
  sessionManager: { getBranch: () => history } };
writeFileSync(join(dirname(path), "session.json"), JSON.stringify({ crew: {
  jack: { incarnation_id: "i1", execution: "background", status: "started" } } }));
writeFileSync(join(dirname(path), "protocol.json"), JSON.stringify({ active: { jack: "x" },
  assignments: { x: { incarnation_id: "i1", state: "done", pending: null, ack_seq: 0,
    notices: [{}] } } }));
recordMessages = false;  // pi is mid-turn: the steer is queued, not yet in history
start({ type: "session_start", reason: "startup" }, ctx);
calls[0].resolve(result("late report", "done", "d1", "Jack"));
await flush();
assert.equal(messages.length, 1);
await tick(30000);
assert.match(notices.at(-1)[0], /uncertain.*not resent/);
assert.equal(notices.at(-1)[1], "warning");
await tick(10000);
assert.equal(calls.length, 1);  // neither acknowledged nor waited on again
history.push({ type: "custom_message", ...messages[0].message });  // the turn ends
await tick(1000);
assert.deepEqual(calls.at(-1).args.slice(-2), ["--ack", "d1"]);
assert.equal(messages.length, 1);  // never resent
calls.at(-1).resolve(result("", "idle", null, "Jack"));
await flush();
const [log] = readdirSync(dirname(path)).filter(name => name.startsWith("pi-wait-"));
assert.match(readFileSync(join(dirname(path), log), "utf8"), /\{"delivered":true\}\n$/);
assert.equal(notices.filter(([, level]) => level === "error").length, 0);
shutdown();
""")

    def test_a_delivery_never_ingested_is_never_acknowledged_and_rearm_reconciles(self):
        self.run_js(r"""
const acked = () => calls.some(call => call.args.includes("--ack"));
recordMessages = false;
await arm("Jack", 60);
calls[0].resolve(result());
await flush();
await tick(30000);
assert.equal(notices.at(-1)[1], "warning");
await tick(31000);  // the watch expires with the delivery still absent
assert.match(notices.at(-1)[0], /uncertain.*not resent or acknowledged/);
assert.equal(notices.at(-1)[1], "error");
assert.equal(acked(), false);
// A fresh delivery whose watch is cancelled while it waits for history acknowledges
// nothing either, now or later.
await arm("Will", 60);
calls.at(-1).resolve(result("will report", "done", "d2", "Will"));
await flush();
await tick(30000);
await arm("Will", undefined, true);
history.push({ type: "custom_message", ...messages[1].message });
await tick(30000);
assert.equal(acked(), false);
// Once pi has ingested it, an explicit rearm acknowledges it without resending.
await arm("Will");
calls.at(-1).resolve(result("will report", "done", "d2", "Will"));
await flush();
assert.deepEqual(calls.at(-1).args.slice(-2), ["--ack", "d2"]);
assert.equal(messages.length, 2);
""")

    def test_generated_extensions_preserve_previous_launches(self):
        original = self.extension.read_bytes()
        second = captain_extension(self.directory)
        self.assertNotEqual(second, self.extension)
        self.assertEqual(self.extension.read_bytes(), original)
        self.assertEqual(second.stat().st_mode & 0o777, 0o600)

    def test_installed_pi_loads_schema_and_delivers_busy_and_idle_messages(self):
        binary = shutil.which("pi")
        if not binary:
            self.skipTest("pi is not installed")
        package = Path(binary).resolve().parents[2]
        loader = package / "dist/core/extensions/loader.js"
        if not loader.exists():
            self.skipTest("pi install does not expose its extension loader")
        self.run_js(
            f"const packagePath = {json.dumps(str(package))};\n"
            + r"""
const { loadExtensions } = await import(pathToFileURL(join(packagePath,
  "dist/core/extensions/loader.js")));
const { AgentSession } = await import(pathToFileURL(join(packagePath,
  "dist/core/agent-session.js")));
const { validateToolArguments } = await import(pathToFileURL(join(packagePath,
  "node_modules/@earendil-works/pi-ai/dist/utils/validation.js")));
const loaded = await loadExtensions([path], dirname(path));
assert.deepEqual(loaded.errors, []);
const definition = loaded.extensions[0].tools.get("captain_wait").definition;
assert.ok(definition);
for (const args of [{ name: "Jack" }, { name: "Jack-2", timeout: 0 }]) {
  assert.deepEqual(validateToolArguments(definition, { arguments: args }), args);
}
for (const args of [{ name: "--help" }, { name: "Jack; false" },
  { name: "Jack", timeout: -1 }, { name: "Jack", timeout: Infinity }]) {
  assert.throws(() => validateToolArguments(definition, { arguments: args }));
}
await arm();
calls[0].resolve(result("reported: FIN"));
await flush();
const { message, options } = messages[0];
for (const busy of [true, false]) {
  let delivered;
  await AgentSession.prototype.sendCustomMessage.call({
    isStreaming: busy,
    agent: { steer(value) { delivered = value; } },
    async _startRun(value) {
      assert.equal(value.messageType, "custom");
      assert.equal(busy, false);
      delivered = value.message;
    },
  }, message, options);
  assert.equal(delivered.role, "custom");
  assert.match(delivered.content, /reported: FIN/);
}
"""
        )


class PiCaptainLaunchTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, HERDR))
        self.enterContext(patch.object(panes, "READY_POLLS", 1))
        # nudge_block/nudge read a realistic agent status these ad hoc herdr fakes don't model;
        # a mail doorbell is not what these tests exercise, so give delivery a clean ring by
        # default. test_submit.py covers nudge_block/nudge themselves against real fakes.
        self.enterContext(patch.object(Pane, "nudge_block", return_value=None))
        self.enterContext(patch.object(Pane, "nudge"))
        self.directory, self.meta = sessions.session(self.project, self.pane, create=True)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def args(self, *args):
        return cli.parser().parse_args(["--session", self.meta["id"], *args])

    def test_pi_captain_loads_private_extension_and_rearming_guidance(self):
        with (
            patch.object(runtime, "herdr"),
            patch.object(agents, "executable", return_value="/bin/pi"),
            patch.object(os, "execvpe") as execute,
            patch.object(sys.stdin, "isatty", return_value=True),
        ):
            agents.launch(
                self.args("--agent", "pi", "--prompt", "Inspect"), self.pane, self.project
            )
        binary, argv, env = execute.call_args.args
        self.assertEqual(binary, "/bin/pi")
        self.assertEqual(argv[-2:], ["--", "Inspect"])
        extension = Path(argv[argv.index("--extension") + 1])
        self.assertEqual(extension.parent, self.directory)
        self.assertEqual(extension.stat().st_mode & 0o777, 0o600)
        self.assertIn(env["CAPTAIN_SESSION"], extension.read_text())
        text = argv[argv.index("--append-system-prompt") + 1]
        self.assertIn("Use the captain_wait tool", text)
        self.assertIn("rearm pane watches after answers", text)
        self.assertNotIn("Run every\nwait in the background", text)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_pi_delivery_instructions_do_not_change_other_roles(self):
        legacy = instruction_prompts.agent_instructions(self.directory, "Captain Barbossa")
        for provider in ("codex", "claude"):
            self.assertEqual(
                instruction_prompts.agent_instructions(
                    self.directory, "Captain Barbossa", provider
                ),
                legacy,
            )
        default_crew = instruction_prompts.agent_instructions(self.directory, "crew member Jack")
        fetch = "Read your mail at the start of every turn"
        for provider in models.PROVIDERS:
            crew = instruction_prompts.agent_instructions(
                self.directory, "crew member Jack", provider
            )
            if provider in models.HOOK_DELIVERED:
                # Its own hook already delivered; dropping that line is the only
                # permitted difference.
                self.assertNotIn(fetch, crew)
                self.assertEqual(
                    crew.splitlines(),
                    [line for line in default_crew.splitlines() if fetch not in line],
                )
            else:
                self.assertEqual(crew, default_crew)
