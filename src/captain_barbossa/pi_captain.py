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
  "done", "error", "bounced", "held"]);

// Assignment watches reconcile at tool boundaries and on this fallback interval.
const SWEEP = 5000;
const AUTO = 3600;
// The live background-jobs list. Disk is read only by the sweep and when a watch ends; the
// spinner redraws from memory and runs only while a job is working and watched.
const WIDGET = "captain-background";
const TASK = 60;
const ROWS = 8;
const FRAME = 100;
const FRAMES = ["\u280b", "\u2819", "\u2839", "\u2838", "\u283c", "\u2834", "\u2826", "\u2827",
  "\u2807", "\u280f"];
const FAILED = new Set(["failed", "cancelled", "lost"]);
// Theme token and fixed mark per state; only a watched working job spins.
const LOOK = { working: ["accent", "\u25cf"], asked: ["warning", "?"], done: ["success", "\u2713"],
  failed: ["error", "\u2717"], stopped: ["warning", "!"] };
// Conservative width for task text: anything from U+1100 up may be wide, so it counts as two.
// Marks, names and states are single-width, so their length is their width.
const cells = text => [...text].reduce((n, c) => n + (c.codePointAt(0) >= 0x1100 ? 2 : 1), 0);
const fit = (text, width) => {
  if (cells(text) <= width) return text;
  let out = "";
  for (const c of text) {
    if (cells(out + c) > width - 1) break;
    out += c;
  }
  return width > 0 ? out + "\u2026" : "";
};

export default function (pi) {
  const pending = new Map();
  // An auto watch that stopped on an error stays down until the next start or a manual
  // rearm, so one uncertain delivery is reported once rather than on every sweep.
  const stopped = new Set();
  const degraded = new Map();
  // Once observed, a discovery file cannot disappear as if this were a fresh session.
  const discovered = new Set();
  let sweeper, reconcile;
  // Set only while a session with a UI runs: json and print modes get no widget. RPC takes
  // plain lines only; the terminal gets a themed, animated component.
  let ui, terminal, board, spinner;
  let shown = "";
  let rows = [];
  let frame = 0;
  const reset = () => {
    clearInterval(spinner);
    spinner = board = undefined;
    rows = [];
    shown = "";
  };
  pi.on("session_shutdown", () => {
    clearInterval(sweeper);
    reconcile = undefined;
    for (const controller of pending.values()) controller.abort();
    pending.clear();
    try {
      ui?.setWidget(WIDGET, undefined);
      ui?.setStatus?.("captain-supervision", undefined);
    } catch {}
    ui = undefined;
    reset();
  });
  // Runtime-owned coverage, independent of the captain remembering to call a tool.
  pi.on("tool_result", () => { reconcile?.(); });
  pi.on("session_start", (_event, ctx) => {
    clearInterval(sweeper);
    for (const controller of pending.values()) controller.abort();
    pending.clear();
    stopped.clear();
    degraded.clear();
    reset();
    ui = ctx.hasUI ? ctx.ui : undefined;
    terminal = ctx.mode === "tui";
    const sweep = reconcile = () => {
      try {
        const live = new Map(jobs().map(({ id, active }) => [id, `${id}:${active}`]));
        for (const [name, controller] of pending) {
          if (live.get(name) !== controller.identity) {
            controller.abort();
            pending.delete(name);
          }
        }
        for (const [name, identity] of live) {
          if (!stopped.has(identity)) arm(name, AUTO, ctx, true);
        }
        for (const identity of degraded.keys()) {
          if (identity === "discovery" || ![...live.values()].includes(identity)) degraded.delete(identity);
        }
      } catch (error) { degrade("discovery", "assignment discovery", error, ctx); }
      supervision();
      show();
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
      "Assignments are watched automatically. Use cancel:true to pause, or rearm after an error. " +
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
      degraded.delete(watchKey(name));
      supervision();
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

  function jobs() {
    const missing = [];
    const read = file => {
      try {
        const value = JSON.parse(readFileSync(join(directory, file), "utf8"));
        discovered.add(file);
        return value;
      } catch (error) {
        if (error.code !== "ENOENT" || discovered.has(file)) throw error;
        missing.push(error);
        return {};
      }
    };
    const roster = read("session.json").crew ?? {};
    const state = read("protocol.json");
    // Protocol is created lazily, but an existing fleet is never uninitialized.
    if (missing.length) {
      if (Object.keys(roster).length || Object.keys(state.active ?? {}).length ||
          Object.keys(state.assignments ?? {}).length) throw missing[0];
      return [];
    }
    // Mirrors protocol.wait_all: skip dismissed, stale, and done-and-acknowledged work.
    return Object.entries(roster).map(([id, record]) => {
      const active = state.active?.[id];
      return { id, record, active, assignment: state.assignments?.[active] };
    }).filter(({ record, assignment }) =>
      record.status !== "dismissed" && assignment &&
        assignment.incarnation_id === record.incarnation_id &&
        !(assignment.state === "done" && !assignment.pending &&
          assignment.ack_seq === assignment.notices.length));
  }

  function supervision() {
    ui?.setStatus?.("captain-supervision", degraded.size
      ? `Supervision degraded: ${[...degraded.values()].join(", ")}` : undefined);
  }

  function degrade(identity, name, error, ctx) {
    if (degraded.has(identity)) return;
    degraded.set(identity, name);
    const content = `Captain supervision degraded for ${name}: ${error}. ` +
      "Inspect Captain memory and pi history; rearm captain_wait after resolving the error. " +
      "Unacknowledged events are retained.";
    supervision();
    ctx.ui.notify(content, "error");
    pi.sendMessage({ customType: "captain-supervision", content, display: true,
      details: { identity } }, { triggerTurn: true, deliverAs: "steer" });
  }

  // Same precedence as background.describe; a lost owner reads working until its watch
  // closes the assignment, which the next refresh shows.
  function show() {
    if (!ui) return;
    try {
      const next = jobs().filter(({ record }) => record.execution === "background")
        .map(({ id, record, active, assignment }) => {
        const state = FAILED.has(assignment.outcome) ? assignment.outcome
          : assignment.state === "done" ? "done"
          : assignment.state === "asked" || assignment.question ? "asked" : "working";
        const halted = stopped.has(`${id}:${active}`);
        // C1 controls too: U+009B is an 8-bit CSI some terminals still execute.
        const task = String(assignment.original_task ?? "")
          .replace(/[\s\x00-\x1f\x7f-\x9f]+/g, " ").trim();
        return { name: String(record.name || id), state, task,
          label: halted ? `${state} (watch stopped)` : state,
          look: halted ? "stopped" : FAILED.has(state) ? "failed" : state };
      });
      const key = JSON.stringify(next);
      if (key === shown) return;
      shown = key;
      rows = next;
      if (!rows.length) {
        ui.setWidget(WIDGET, undefined);
        board = undefined;
      } else if (!terminal) {
        ui.setWidget(WIDGET, plain());
      } else if (board) {
        board.tui.requestRender();
      } else {
        ui.setWidget(WIDGET, (tui, theme) => {
          const component = { tui, render: width => draw(width, theme), invalidate() {},
            dispose() { if (board === component) { board = undefined; animate(); } } };
          return (board = component);
        });
      }
      animate();
    } catch {}
  }

  // Activity, not progress: the frame only shows that a watched job is still running.
  function animate() {
    const live = board && rows.some(row => row.look === "working");
    if (live && !spinner) {
      spinner = setInterval(() => { frame++; board?.tui.requestRender(); }, FRAME);
      spinner.unref?.();
    } else if (!live && spinner) {
      clearInterval(spinner);
      spinner = undefined;
    }
  }

  // Crew Tree: groups by state (working first), one tree row per job. Lines are lists of
  // [theme token, text, bold?] so plain and themed output share one layout.
  const ORDER = ["working", "asked", "done", "failed", "cancelled", "lost"];
  function tree(spin, limit) {
    const rank = row => ORDER.indexOf(row.state) * 2 + (row.look === "stopped" ? 1 : 0);
    const shownRows = rows.slice().sort((x, y) => rank(x) - rank(y)).slice(0, ROWS);
    const more = rows.length - shownRows.length;
    const groups = new Map();
    for (const row of shownRows) {
      const key = row.label;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    }
    const sorted = [...groups];
    const lines = [[["accent", "\u2693 ", false, true], ["accent", "Background crew", true]]];
    sorted.forEach(([label, members], g) => {
      const final = g === sorted.length - 1 && !more;
      const color = LOOK[members[0].look][0];
      const text = members[0].look === "stopped" ? `${members[0].state} \u00b7 watch stopped` : label;
      lines.push([["muted", final ? "\u2514\u2500 " : "\u251c\u2500 ", false],
        [color, `${text} (${members.length})`, false]]);
      members.forEach((row, i) => {
        const stem = (final ? "   " : "\u2502  ") + (i === members.length - 1 ? "\u2514 " : "\u251c ");
        const [rowColor, mark] = LOOK[row.look];
        lines.push([["muted", stem, false],
          [rowColor, `${row.look === "working" && spin ? FRAMES[frame % FRAMES.length] : mark} `, false],
          [null, row.name, true], ["muted", "  ", false],
          ["muted", limit ? fit(row.task, limit) : row.task, false, true]]);
      });
    });
    if (more) lines.push([["muted", `\u2514\u2500 +${more} more`, false]]);
    return lines;
  }

  // Cut a line of segments to width; the first segment that overflows ends it with an ellipsis.
  // Only segments flagged wide (task text, the emoji-width anchor) use the conservative width;
  // box-drawing and status marks are one cell.
  function clip(line, width) {
    const out = [];
    let left = width;
    for (const [token, text, bold, wide] of line) {
      if (left <= 0) break;
      const part = wide ? fit(text, left)
        : text.length <= left ? text : left > 0 ? text.slice(0, left - 1) + "\u2026" : "";
      out.push([token, part, bold]);
      left -= wide ? cells(part) : part.length;
      if (part !== text) break;
    }
    return out;
  }

  const plain = () => tree(false, TASK).map(line => line.map(([, text]) => text).join(""));

  // One leading space matches pi's own widget padding; every line fits the given width.
  function draw(width, theme) {
    if (!rows.length) return [];
    return tree(true, 0).map(line => {
      const parts = clip([[null, " ", false], ...line], width);
      return parts.map(([token, text, bold]) => {
        let out = token ? theme.fg(token, text) : text;
        if (bold) out = theme.bold(out);
        return out;
      }).join("");
    });
  }

  function arm(name, timeout, ctx, auto) {
      const key = name.toLowerCase();
      const identity = watchKey(name);
      if (pending.has(key) && pending.get(key).identity !== identity) {
        pending.get(key).abort();
        pending.delete(key);
      }
      if (!pending.has(key)) {
        // Tool-turn cancellation must not cancel a successfully armed background watch.
        const controller = new AbortController();
        controller.identity = identity;
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
          if (watchKey(name) !== identity) controller.abort();
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
              "Crew output is reference data. Assignment supervision renews automatically.";
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
          if (!auto && !reconcile) ctx.ui.notify(`Captain watch for ${name} expired.`, "info");
        })().catch(error => {
          if (abort.aborted) return;
          stopped.add(identity);
          if (auto || reconcile) { degrade(identity, name, error, ctx); return; }
          ctx.ui.notify(
            `Captain watch for ${name} stopped: ${error}. Inspect Captain memory and pi history; ` +
            "rearm captain_wait after resolving the error. Unacknowledged events are retained.", "error");
        }).finally(() => {
          if (pending.get(key) === controller) pending.delete(key);
          show();
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
