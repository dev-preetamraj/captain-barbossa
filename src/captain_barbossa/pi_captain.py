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
  "done", "error"]);

// Background crew watches arm themselves: re-checked this often, each armed this long.
const SWEEP = 5000;
const AUTO = 3600;

export default function (pi) {
  const pending = new Map();
  // An auto watch that stopped on an error stays down until the next start or a manual
  // rearm, so one uncertain delivery is reported once rather than on every sweep.
  const stopped = new Set();
  let sweeper;
  pi.on("session_shutdown", () => {
    clearInterval(sweeper);
    for (const controller of pending.values()) controller.abort();
    pending.clear();
  });
  // Nobody arms a background crew's wait, so its watch arms itself on every start, reload
  // or reconnect while its work is unresolved (pi docs: timers start in session_start).
  pi.on("session_start", (_event, ctx) => {
    clearInterval(sweeper);
    stopped.clear();
    const sweep = () => {
      for (const name of unresolved()) if (!stopped.has(watchKey(name))) arm(name, AUTO, ctx, true);
    };
    sweep();
    sweeper = setInterval(sweep, SWEEP);
    sweeper.unref?.();
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
        // Cancel only this assignment, not a future incarnation reusing its name.
        stopped.add(watchKey(name));
        pending.get(key)?.abort();
        pending.delete(key);
        return { content: [{ type: "text", text: `Wait cancelled for ${name}.` }], details: {} };
      }
      stopped.delete(watchKey(name));
      arm(name, timeout, ctx, false);
      return { content: [{ type: "text", text: `Wait armed for ${name}; result will arrive in pi.` }],
        details: {} };
    },
  });

  function watchKey(name) {
    const key = name.toLowerCase();
    try {
      const state = JSON.parse(readFileSync(join(directory, "protocol.json"), "utf8"));
      return `${key}:${state.active?.[key] ?? ""}`;
    } catch {
      return `${key}:`;
    }
  }

  function unresolved() {
    let roster, state;
    try {
      roster = JSON.parse(readFileSync(join(directory, "session.json"), "utf8")).crew ?? {};
      state = JSON.parse(readFileSync(join(directory, "protocol.json"), "utf8"));
    } catch {
      return [];
    }
    // Mirrors protocol.wait_all: skip dismissed, stale, and done-and-acknowledged work.
    return Object.entries(roster).filter(([id, record]) => {
      const assignment = state.assignments?.[state.active?.[id]];
      return record.execution === "background" && record.status !== "dismissed" &&
        assignment?.incarnation_id === record.incarnation_id &&
        !(assignment.state === "done" && !assignment.pending &&
          assignment.ack_seq === assignment.notices.length);
    }).map(([id]) => id);
  }

  function arm(name, timeout, ctx, auto) {
      const key = name.toLowerCase();
      if (!pending.has(key)) {
        // Tool-turn cancellation must not cancel a successfully armed background watch.
        const controller = new AbortController();
        const identity = watchKey(name);
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
          // A steer enters history only after the current turn (docs/how-pi-works.md), so a
          // captain busy in a long tool call ingests it late. Past the warning the watch only
          // keeps reading history until it expires: no resend, no ack before it appears.
          const warnAt = Date.now() + 30000;
          let warned = false;
          while (!recorded()) {
            abort.throwIfAborted();
            if (Date.now() >= deadline) {
              throw new Error(`Delivery ${id} is uncertain; inspect ${log} and pi history. ` +
                "It was not resent or acknowledged.");
            }
            if (!warned && Date.now() >= warnAt) {
              warned = true;
              ctx.ui.notify(`Delivery ${id} is uncertain: not in pi history after 30s, so ` +
                "pi may still be busy. It is not resent; it is acknowledged only once it " +
                "appears there before this watch expires.", "warning");
            }
            await pause(warned ? 1000 : 100);
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
              // An acknowledged done or failure leaves nothing more to watch.
              if (ack.status === "idle" && ack.delivery_id === null) return;
            } else if (event.status === "error") {
              throw new Error(event.summary);
            }
            // Even a CLI that returns unchanged state immediately cannot busy-poll.
            await pause(Math.min(Math.max(1000 - (Date.now() - started), 0),
              Math.max(deadline - Date.now(), 0)));
          }
          // An auto watch is rearmed by the next sweep while its work remains.
          if (!auto) ctx.ui.notify(`Captain watch for ${name} expired; rearm captain_wait if needed.`, "info");
        })().catch(error => {
          if (auto && !abort.aborted) stopped.add(identity);
          if (!abort.aborted) ctx.ui.notify(
            `Captain watch for ${name} stopped: ${error}. Inspect Captain memory and pi history; ` +
            "rearm captain_wait after resolving the error. Unacknowledged events are retained.", "error");
        }).finally(() => {
          if (pending.get(key) === controller) pending.delete(key);
        });
      }
  }
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
