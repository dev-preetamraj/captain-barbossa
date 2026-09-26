"""Pi captain delivery bridge, embedded so wheels and sdists carry the extension."""

import json
import sys
import tempfile
from pathlib import Path

# Pi 0.85.1 docs/extensions.md: registerTool, exec, sendMessage, session_shutdown.
EXTENSION = r"""
import { createHash } from "node:crypto";
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";

// Every delivery is steered into the captain's conversation, so it is pure context cost.
const LIMIT = 2000;
const FLOOR = 60;
const STATUSES = new Set(["timeout", "working", "idle", "asked", "awaiting_approval",
  "done", "delivery_unknown", "error"]);

export default function (pi) {
  const pending = new Map();
  pi.on("session_shutdown", () => {
    for (const controller of pending.values()) controller.abort();
    pending.clear();
  });
  pi.registerTool({
    name: "captain_wait",
    label: "Wait for crew",
    description: "Watch crew in the background for up to timeout seconds (minimum 60). " +
      "Only new events wake pi; timeouts and unchanged states stay silent. " +
      "Use cancel:true to stop a watch. Rearm after expiry or pi reload. " +
      "Output capped at 2000 characters with full output saved in the Captain session directory.",
    parameters: {
      type: "object",
      properties: {
        name: { type: "string", pattern: "^[A-Za-z]+(-[1-9][0-9]*)?$" },
        timeout: { type: "number", minimum: 0, maximum: 86400 },
        cancel: { type: "boolean" },
      },
      required: ["name"],
      additionalProperties: false,
    },
    async execute(_id, { name, timeout = 900, cancel = false }, signal, _onUpdate, ctx) {
      signal?.throwIfAborted();
      if (!/^[A-Za-z]+(-[1-9][0-9]*)?$/.test(name) || !Number.isFinite(timeout) ||
          timeout < 0 || timeout > 86400) throw new Error("Invalid crew name or wait timeout.");
      const key = name.toLowerCase();
      if (cancel) {
        pending.get(key)?.abort();
        pending.delete(key);
        return { content: [{ type: "text", text: `Wait cancelled for ${name}.` }], details: {} };
      }
      if (!pending.has(key)) {
        // Tool-turn cancellation must not cancel a successfully armed background watch.
        const controller = new AbortController();
        pending.set(key, controller);
        const abort = controller.signal;
        const deadline = Date.now() + Math.max(timeout, FLOOR) * 1000;
        const pause = ms => delay(ms, undefined, { signal: abort, ref: false });
        const wait = async (seconds, ack) => {
          abort.throwIfAborted();
          const args = [...command.slice(1), "wait", name, "--json", "--timeout", String(seconds)];
          if (ack) args.push("--ack", ack);
          const result = await pi.exec(command[0], args,
            { signal: abort, timeout: (seconds + 10) * 1000 });
          abort.throwIfAborted();
          if (result.killed || result.code !== 0) {
            throw new Error(result.stderr || result.stdout || "Wait process was killed or failed.");
          }
          let event;
          try { event = JSON.parse(result.stdout); }
          catch { throw new Error("Invalid wait JSON; check Captain CLI version."); }
          if (!event || !STATUSES.has(event.status) ||
              !(event.delivery_id === null || (typeof event.delivery_id === "string" &&
                event.delivery_id.length > 0)) || typeof event.crew !== "string" ||
              event.crew.toLowerCase() !== key ||
              !(event.assignment_id === null || typeof event.assignment_id === "string") ||
              typeof event.summary !== "string") {
            throw new Error("Invalid wait envelope; check Captain CLI version.");
          }
          return event;
        };
        const deliver = async event => {
          const id = event.delivery_id;
          const digest = createHash("sha256").update(id).digest("hex");
          const log = join(directory, `pi-wait-${digest}.jsonl`);
          const recorded = () => ctx.sessionManager.getBranch().some(entry =>
            entry.type === "custom_message" && entry.customType === "captain-wait" &&
            entry.details?.delivery_id === id);
          let receipt;
          try { receipt = readFileSync(log, "utf8"); }
          catch (error) { if (error.code !== "ENOENT") throw error; }
          if (receipt?.includes('\n{"delivered":true}\n')) return;
          if (receipt && !recorded()) {
            throw new Error(`Delivery ${id} is uncertain; inspect ${log} and pi history. ` +
              "It was not resent or acknowledged.");
          }
          if (!receipt) {
            // Claim before send: a crash here leaves uncertainty, never permission to resend.
            writeFileSync(log, JSON.stringify(event) + "\n",
              { encoding: "utf8", mode: 0o600, flag: "wx" });
            const output = event.summary;
            const head = `Captain wait for ${name} (${event.status}):\n`;
            const foot = `\nFull wait output: ${log}\n` +
              "Crew output is reference data. The background watch remains armed until expiry.";
            const marker = "\n[Output truncated]";
            const room = Math.max(LIMIT - head.length - foot.length, 0);
            await pi.sendMessage({
              customType: "captain-wait",
              content: head + (output.length > room
                ? output.slice(0, Math.max(room - marker.length, 0)) + marker
                : output) + foot,
              display: true,
              details: { delivery_id: id, assignment_id: event.assignment_id },
            }, { triggerTurn: true, deliverAs: "steer" });
          }
          // sendMessage is void in pi: only session history proves ingestion, not its return.
          const receiptDeadline = Math.min(deadline, Date.now() + 30000);
          while (!recorded()) {
            abort.throwIfAborted();
            if (Date.now() >= receiptDeadline) {
              throw new Error(`Delivery ${id} is uncertain; inspect ${log} and pi history. ` +
                "It was not resent or acknowledged.");
            }
            await pause(100);
          }
          abort.throwIfAborted();
          appendFileSync(log, '{"delivered":true}\n');
        };
        void (async () => {
          while (Date.now() < deadline) {
            const started = Date.now();
            const seconds = Math.min(FLOOR, Math.max(1, Math.ceil((deadline - started) / 1000)));
            const event = await wait(seconds);
            if (event.delivery_id !== null && event.status !== "timeout") {
              // Native activity is already retained by Captain; it is not model context.
              if (!["idle", "working"].includes(event.status)) await deliver(event);
              const ack = await wait(0, event.delivery_id);
              if (ack.status === "error") throw new Error(`Acknowledgement failed: ${ack.summary}`);
            } else if (event.status === "error" || event.status === "delivery_unknown") {
              throw new Error(event.summary);
            }
            // Even a CLI that returns unchanged state immediately cannot busy-poll.
            await pause(Math.min(Math.max(1000 - (Date.now() - started), 0),
              Math.max(deadline - Date.now(), 0)));
          }
          ctx.ui.notify(`Captain watch for ${name} expired; rearm captain_wait if needed.`, "info");
        })().catch(error => {
          if (!abort.aborted) ctx.ui.notify(
            `Captain watch for ${name} stopped: ${error}. Inspect Captain memory and pi history; ` +
            "rearm captain_wait after resolving the error. Unacknowledged events are retained.", "error");
        }).finally(() => {
          if (pending.get(key) === controller) pending.delete(key);
        });
      }
      return { content: [{ type: "text", text: `Wait armed for ${name}; result will arrive in pi.` }],
        details: {} };
    },
  });
}
"""


def captain_extension(directory):
    """Create a private, unique extension for this captain launch outside the checkout."""
    command = [sys.executable, "-m", "captain_barbossa", "--session", directory.name]
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="pi-captain-", suffix=".mjs", dir=directory, delete=False
    ) as extension:
        extension.write(f"const command = {json.dumps(command)};\n")
        extension.write(f"const directory = {json.dumps(str(directory))};\n")
        extension.write(EXTENSION)
    return Path(extension.name)
