"""The crew-only pi session boundary extension."""

import json


def extension(directory, events, incarnation):
    """Record identities, and prevent resuming a session outside the exclusive directory."""
    return """import { appendFileSync, realpathSync } from 'node:fs';
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
    appendFileSync(events, JSON.stringify({
      provider: 'pi', incarnation_id: incarnation,
      type: 'session_start', session_id: ctx.sessionManager.getSessionId(),
      transcript_path: ctx.sessionManager.getSessionFile() ?? null
    }) + '\\n', { mode: 0o600 });
  });
}
""".replace(
        "CONFIG",
        json.dumps({"root": str(directory), "events": str(events), "incarnation": incarnation}),
    )
