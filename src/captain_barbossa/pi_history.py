"""The crew-only pi session boundary extension."""

import json

from .approval import WORD_BREAKS

# A JavaScript port of approval.denied; the matrix test runs both over the same commands.
GATE = """const DENIALS = __RULES__;
const WORD_BREAKS = new RegExp(__BREAKS__);
function inOrder(needed, words) {
  let at = 0;
  for (const word of needed) {
    at = words.indexOf(word, at);
    if (at < 0) return false;
    at += 1;
  }
  return true;
}
function denied(command) {
  const text = Array.isArray(command) ? command.map(String).join(' ') : command;
  if (typeof text !== 'string') return null;
  const words = text.split(WORD_BREAKS).map((word) => word.split('/').pop().split('.')[0]);
  for (const [program, ...rest] of DENIALS) {
    for (let index = 0; index < words.length; index += 1) {
      if (words[index] === program && inOrder(rest, words.slice(index + 1))) return [program, ...rest];
    }
  }
  return null;
}
"""

BLOCK = """  pi.on('tool_call', async (event) => {
    if (denied(event.input?.command) === null) return;
    return { block: true, reason: 'Captain approval [plugin-denied]: a captain plugin forbids crew this command; report back to the captain instead' };
  });
"""


def gate(denials):
    """The JavaScript matcher with these denials and approval's word breaks baked in."""
    return GATE.replace("__RULES__", json.dumps(denials)).replace(
        "__BREAKS__", json.dumps(WORD_BREAKS)
    )


def extension(directory, events, incarnation, denials=()):
    """Record identities, prevent resuming a session outside the exclusive directory, and,
    when a plugin forbids crew any command, block every tool call that would run one."""
    source = """import { closeSync, constants, openSync, realpathSync, writeSync } from 'node:fs';
import { dirname } from 'node:path';
export default function(pi) {
  const { root, events, incarnation } = CONFIG;
  pi.on('session_before_switch', async (event) => {
    if (event.reason !== 'resume') return;
    try {
      if (dirname(realpathSync(event.targetSessionFile)) === root) return;
    } catch {}
    return { cancel: true };
  });
  pi.on('session_start', async (event, ctx) => {
    const line = Buffer.from(JSON.stringify({
      provider: 'pi', incarnation_id: incarnation,
      type: 'session_start', session_id: ctx.sessionManager.getSessionId(),
      transcript_path: ctx.sessionManager.getSessionFile() ?? null
    }) + '\\n');
    const { O_APPEND, O_CREAT, O_NOFOLLOW, O_WRONLY } = constants;
    const fd = openSync(events, O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW, 0o600);
    try { writeSync(fd, line); } finally { closeSync(fd); }
  });
}
""".replace(
        "CONFIG",
        json.dumps({"root": str(directory), "events": str(events), "incarnation": incarnation}),
    )
    if not denials:
        return source
    head, body = source.split("export default function(pi) {\n", 1)
    return f"{head}{gate(list(denials))}export default function(pi) {{\n{BLOCK}{body}"
