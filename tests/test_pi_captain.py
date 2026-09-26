"""Run the shipped pi extension with Node, without a model or Herdr session."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from captain_barbossa.pi_captain import captain_extension

HARNESS = r"""
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";
import { mock } from "node:test";
mock.timers.enable({ apis: ["setTimeout", "Date"] });
const path = process.argv[2];
const { default: extension } = await import(pathToFileURL(path));
const calls = [], messages = [], notices = [];
const history = [];
let recordMessages = true;
let tool, shutdown;
const pi = {
  on(event, handler) { assert.equal(event, "session_shutdown"); shutdown = handler; },
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
    async _runAgentPrompt(value) { delivered = value; },
  }, message, options);
  assert.equal(delivered.role, "custom");
  assert.match(delivered.content, /reported: FIN/);
}
"""
        )
