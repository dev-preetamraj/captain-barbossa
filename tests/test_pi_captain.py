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
const path = process.argv[2];
const { default: extension } = await import(pathToFileURL(path));
const calls = [], messages = [], notices = [];
let tool, shutdown;
const pi = {
  on(event, handler) { assert.equal(event, "session_shutdown"); shutdown = handler; },
  registerTool(value) { tool = value; },
  exec(command, args, options) {
    return new Promise((resolve, reject) => calls.push({ command, args, options, resolve, reject }));
  },
  sendMessage(message, options) { messages.push({ message, options }); },
};
extension(pi);
assert.equal(tool.name, "captain_wait");
assert.equal(calls.length, 0);
const turn = new AbortController();
const arm = (name = "Jack", timeout) => tool.execute("id", { name, timeout }, turn.signal,
  undefined, { ui: { notify: (...args) => notices.push(args) } });
const flush = () => new Promise(resolve => setImmediate(resolve));
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
const result = await arm();
assert.match(result.content[0].text, /Wait armed/);
assert.equal(messages.length, 0);
await arm("jack");
await arm("Will", 7);
assert.equal(calls.length, 2);
assert.deepEqual(calls[0].args.slice(0, 3), ["-m", "captain_barbossa", "--session"]);
assert.deepEqual(calls[0].args.slice(-4), ["wait", "Jack", "--timeout", "900"]);
assert.deepEqual(calls[1].args.slice(-4), ["wait", "Will", "--timeout", "60"]);
assert.equal(calls[1].options.timeout, 90000);
turn.abort();
assert.equal(calls[0].options.signal.aborted, false);
calls[0].resolve({ stdout: "Jack done.\nreported: focused tests passed", stderr: "", code: 0 });
await flush();
assert.equal(messages.length, 1);
assert.equal(messages[0].message.customType, "captain-wait");
assert.equal(messages[0].message.display, true);
assert.match(messages[0].message.content, /focused tests passed/);
assert.deepEqual(messages[0].options, { triggerTurn: true, deliverAs: "steer" });
shutdown();
assert.equal(calls[1].options.signal.aborted, true);
calls[1].resolve({ stdout: "late result", stderr: "", code: 0 });
await flush();
assert.equal(messages.length, 1);
assert.deepEqual(notices, []);
""")

    def test_blocked_followup_timeout_and_failure_can_each_be_rearmed(self):
        self.run_js(r"""
for (const output of [
  { stdout: "Jack blocked.\npermission needed", stderr: "", code: 0 },
  { stdout: "Jack done.\nfollow-up finished", stderr: "", code: 0 },
  { stdout: "", stderr: "Jack was still working. Wait again.", code: 1 },
  { stdout: "", stderr: "", code: 1, killed: true },
]) {
  await arm();
  calls.at(-1).resolve(output);
  await flush();
  assert.match(messages.at(-1).message.content, /Rearm captain_wait/);
  assert.match(messages.at(-1).message.content, new RegExp(`exit ${output.code}`));
}
assert.equal(calls.length, 4);
assert.match(messages[0].message.content, /permission needed/);
assert.match(messages[1].message.content, /follow-up finished/);
assert.match(messages[2].message.content, /still working/);
assert.match(messages[3].message.content, /killed/);
await arm();
calls.at(-1).reject(new Error("spawn failed"));
await flush();
assert.match(messages.at(-1).message.content, /spawn failed/);
await arm();
assert.equal(calls.length, 6);
shutdown();
shutdown();
calls.at(-1).reject(new Error("cancelled"));
await flush();
assert.equal(messages.length, 5);
assert.deepEqual(notices, []);
""")

    def test_full_output_is_preserved_privately_and_context_is_bounded(self):
        self.run_js(r"""
const output = "crew report\n" + "x".repeat(20000) + "final line";
await arm();
calls[0].resolve({ stdout: output, stderr: "", code: 0 });
await flush();
// The whole steered message, wrapper included, is what costs the captain context.
assert.ok(messages[0].message.content.length <= 2000);
assert.match(messages[0].message.content, /crew report/);
assert.match(messages[0].message.content, /Output truncated/);
const [log] = readdirSync(dirname(path)).filter(name => name.startsWith("pi-wait-"));
const logPath = join(dirname(path), log);
assert.equal(readFileSync(logPath, "utf8"), output);
assert.equal(statSync(logPath).mode & 0o777, 0o600);
assert.ok(messages[0].message.content.includes(logPath));
""")

    def test_a_background_wait_shorter_than_the_floor_is_raised_to_it(self):
        self.run_js(r"""
for (const [crew, timeout] of [["Jack", 0], ["Will", 5], ["Gibbs", 60], ["Cotton", 120]]) {
  await arm(crew, timeout);
  const armed = Number(calls.at(-1).args.at(-1));
  assert.equal(armed, Math.max(timeout, 60));
  assert.equal(calls.at(-1).options.timeout, (armed + 30) * 1000);
}
""")

    def test_short_output_is_delivered_whole_without_a_truncation_marker(self):
        self.run_js(r"""
await arm();
calls[0].resolve({ stdout: "Jack idle.\nidle; reported: done", stderr: "", code: 0 });
await flush();
assert.match(messages[0].message.content, /idle; reported: done/);
assert.doesNotMatch(messages[0].message.content, /Output truncated/);
""")

    def test_cancelled_tool_does_not_start_a_wait(self):
        self.run_js(r"""
turn.abort();
await assert.rejects(arm(), /abort/i);
assert.equal(calls.length, 0);
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
calls[0].resolve({ stdout: "Jack done.\nreported: FIN", stderr: "", code: 0 });
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
