"""The crew-only pi session boundary extension."""

import json

from .approval import QUOTING, WORD_BREAKS

# A JavaScript port of approval.denied; the matrix test runs both over the same commands.
GATE = r"""const DENIALS = __RULES__;
const WORD_BREAKS = new RegExp(__BREAKS__);
const QUOTING = new RegExp(__QUOTING__, 'g');
function names(word, wanted) {
  if (word === wanted) return true;
  if (!/[*?[]/.test(word) || !/[a-z0-9]/.test(word)) return false;
  const source = word.replace(/[.+^${}()|\\]/g, '\\$&').replace(/\*/g, '.*')
    .replace(/\?/g, '.').replace(/\[!/g, '[^');
  try { return new RegExp(`^${source}$`, 's').test(wanted); } catch { return false; }
}
function inOrder(needed, words) {
  let at = 0;
  for (const item of needed) {
    while (at < words.length && !names(words[at], item)) at += 1;
    if (at >= words.length) return false;
    at += 1;
  }
  return true;
}
function denied(command) {
  const raw = Array.isArray(command) ? command.map(String).join(' ') : command;
  if (typeof raw !== 'string') return null;
  const text = raw.toLowerCase().replace(QUOTING, '');
  const words = text.split(WORD_BREAKS).map((word) => word.split('/').pop().split('.')[0]);
  for (const rule of DENIALS) {
    const [program, ...rest] = rule.map((word) => word.toLowerCase());
    for (let index = 0; index < words.length; index += 1) {
      if (names(words[index], program) && inOrder(rest, words.slice(index + 1))) return rule;
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


# The same Claude-shaped PreToolUse the other providers' hook command answers. It fails
# closed: a missing hook, timeout, non-zero exit or invalid output blocks the call.
HOOK_BLOCK = """  pi.on('tool_call', async (event, ctx) => {
    const stop = (why) => ({ block: true, reason: `Captain hook failed (${why}): the call is blocked; run captain ask.` });
    const run = spawnSync(HOOK[0], HOOK.slice(1), { encoding: 'utf8', timeout: 30000, input: JSON.stringify({
      hook_event_name: 'PreToolUse', tool_name: event.toolName, tool_input: event.input,
      tool_use_id: event.toolCallId, session_id: ctx.sessionManager.getSessionId(), cwd: ctx.cwd
    }) });
    if (run.error) return stop(run.error.code || 'could not run');
    if (run.status !== 0) return stop(run.signal ? `signal ${run.signal}` : `exit ${run.status}`);
    // A clean run that prints nothing is the hook's own "no verdict"; anything else must parse.
    if (!run.stdout.trim()) return;
    let verdict = null;
    try { verdict = JSON.parse(run.stdout).hookSpecificOutput; } catch {}
    const decision = verdict?.permissionDecision;
    if (decision === 'deny') {
      return { block: true, reason: verdict.permissionDecisionReason || 'Denied by the captain: run captain ask.' };
    }
    if (decision !== 'allow') return stop('invalid output');
  });
"""


def gate(denials):
    """The JavaScript matcher with these denials and approval's word breaks baked in."""
    fixed = GATE.replace("__BREAKS__", json.dumps(WORD_BREAKS))
    # Rules go in last, so a declared word can never match a later placeholder.
    return fixed.replace("__QUOTING__", json.dumps(QUOTING)).replace(
        "__RULES__", json.dumps(denials)
    )


def extension(directory, events, incarnation, denials=(), hook=None):
    """Record identities, prevent resuming a session outside the exclusive directory, and,
    when a plugin forbids crew any command, block every tool call that would run one.
    hook: argv of the captain hook command, which then answers every tool call too."""
    source = """import { spawnSync } from 'node:child_process';
import { closeSync, constants, openSync, realpathSync, writeSync } from 'node:fs';
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
    if not denials and not hook:
        return source
    head, body = source.split("export default function(pi) {\n", 1)
    prefix = f"{head}const HOOK = {json.dumps(list(hook))};\n" if hook else head
    if denials:
        prefix += gate(list(denials))
    blocks = (BLOCK if denials else "") + (HOOK_BLOCK if hook else "")
    return f"{prefix}export default function(pi) {{\n{blocks}{body}"
