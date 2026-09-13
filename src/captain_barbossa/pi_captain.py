"""Pi captain delivery bridge, embedded so wheels and sdists carry the extension."""

import json
import sys
import tempfile
from pathlib import Path

# Pi 0.85.1 docs/extensions.md: registerTool, exec, sendMessage, session_shutdown.
EXTENSION = r"""
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { join } from "node:path";

// Every delivery is steered into the captain's conversation, so it is pure context cost.
const LIMIT = 2000;
// A wait that returns instantly burns a captain turn per poll; background waits sit out
// at least this long. The CLI still honours --timeout 0 as an immediate event check.
const FLOOR = 60;

export default function (pi) {
  const pending = new Map();
  pi.on("session_shutdown", () => {
    for (const controller of pending.values()) controller.abort();
    pending.clear();
  });
  pi.registerTool({
    name: "captain_wait",
    label: "Wait for crew",
    description: "Arm one background Captain wait; deliver its result into pi. " +
      "Use the crew display name. Returns immediately. Timeouts below 60s are raised to 60s. " +
      "Output capped at 2000 characters with full output saved in the Captain session directory.",
    parameters: {
      type: "object",
      properties: {
        name: { type: "string", pattern: "^[A-Za-z]+(-[1-9][0-9]*)?$" },
        timeout: { type: "number", minimum: 0, maximum: 86400 },
      },
      required: ["name"],
      additionalProperties: false,
    },
    async execute(_id, { name, timeout = 900 }, signal, _onUpdate, ctx) {
      signal?.throwIfAborted();
      const key = name.toLowerCase();
      if (!pending.has(key)) {
        // The wait outlives this tool turn; only session shutdown cancels it.
        const controller = new AbortController();
        pending.set(key, controller);
        const seconds = Math.max(timeout, FLOOR);
        const deliver = (output, code) => {
          if (controller.signal.aborted) return;
          pending.delete(key);
          const log = join(directory, `pi-wait-${randomUUID()}.txt`);
          writeFileSync(log, output, { encoding: "utf8", mode: 0o600, flag: "wx" });
          const head = `Captain wait for ${name} (exit ${code}):\n`;
          const foot = `\nFull wait output: ${log}\n` +
            "Wait ended. Rearm captain_wait now; act only if this changed something.";
          const marker = "\n[Output truncated]";
          const room = Math.max(LIMIT - head.length - foot.length, 0);
          pi.sendMessage({
            customType: "captain-wait",
            content: head + (output.length > room
              ? output.slice(0, Math.max(room - marker.length, 0)) + marker
              : output) + foot,
            display: true,
          }, { triggerTurn: true, deliverAs: "steer" });
        };
        void pi.exec(command[0], [...command.slice(1), "wait", name, "--timeout", String(seconds)],
          { signal: controller.signal, timeout: (seconds + 30) * 1000 })
          .then(result => deliver(
            [result.stdout, result.stderr, result.killed ? "Wait process was killed." : ""]
              .filter(Boolean).join("\n"), result.code),
            error => deliver(String(error), 1))
          .catch(error => {
            if (!controller.signal.aborted) {
              pending.delete(key);
              ctx.ui.notify(`Captain wait delivery failed: ${error}. Read Captain memory.`, "error");
            }
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
