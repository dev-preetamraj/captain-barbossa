"""Native agent terminal interaction and pane parsing."""

import re
import time

from . import runtime
from .runtime import HERDR_ERRORS, CaptainError

POLL_INTERVAL = 0.2
# Consecutive idle polls before a freshly drawn native TUI accepts a submitted prompt.
READY_POLLS = 6
PROMPT_TIMEOUT = 5
# Native prompt ingestion can lag the send; observe longer without sending again.
SUBMIT_TIMEOUT = 20
TAIL_LINES = 40
TAIL_LIMIT = 1500
MODEL_INTERVAL = 1
MODEL_TIMEOUT = 20
# Each CLI's own line after a switch: "Set model to Sonnet 5 …" / "Model changed to …".
MODEL_CONFIRMATIONS = ("set model to", "model changed to")
# pi instead echoes "Model: <id>" at line start; its status bar never carries "model:".
PI_MODEL_CONFIRMATION = re.compile(r"^\s*Model:\s")
CODEX_EFFORT_HEADER = "Select Reasoning Level"
# Input-prompt glyphs of the native TUIs, which also open their status bars.
PROMPT_GLYPHS = ("❯", "›")
INTERRUPT_MARKER = "esc to interrupt"
# Pane chrome shared by the Claude Code and Codex TUIs: a bare box-drawing rule, an
# empty input prompt (provider placeholders need separate recognition),
# and the bottom status bar, which is always the last line and never starts like
# real transcript content (a bullet, a tree glyph, a spinner).
PANE_RULE = re.compile(r"^[─\-=━]+$")
PANE_EMPTY_PROMPT = re.compile(rf"^[{''.join(PROMPT_GLYPHS)}]\s*(Ask Codex to do anything)?$")
PANE_STATUS_BAR_PREFIXES = ("•", "⏺", "└", "│", "✻", "✳", "?", "…", *PROMPT_GLYPHS, "⎿")
# Codex draws rate-limit and approval choices as a numbered list closed by a confirm
# line and keeps reporting the pane idle while one is showing. Enter would accept the
# default (silently switching the model), so a modal is needs-attention, not output.
PANE_MODAL_CONFIRM = re.compile(r"^Press enter to confirm or esc to \w+")
PANE_MODAL_OPTION = re.compile(r"^[›»]?\s*([1-9])\.\s+(\S+)")
PANE_MODAL_LINES = 20


def modal_start(lines):
    """Index of a trailing choice modal's first option, or None when there is none.

    The lines above it are the modal's question, which a blocked crew's tail must keep.
    """
    if not lines or not PANE_MODAL_CONFIRM.match(lines[-1]):
        return None
    window = range(max(len(lines) - PANE_MODAL_LINES, 0), len(lines) - 1)
    options = [index for index in window if PANE_MODAL_OPTION.match(lines[index])]
    return options[0] if options else None


class Pane:
    """One named native agent's terminal."""

    def __init__(self, agent_name):
        self.agent_name = agent_name

    def lines(self, keep_status=False, keep_blank=False):
        """The non-blank tail of the crew's pane, as stripped lines, without the status bar.

        Composer checks retain chrome and blank rows to avoid mistaking a draft for a footer.
        """
        output = runtime.herdr(
            "agent", "read", self.agent_name, "--lines", str(TAIL_LINES), raw=True, timeout=10
        )
        lines = [line.strip() for line in output.splitlines() if keep_blank or line.strip()]
        if (
            not keep_status
            and lines
            and "·" in lines[-1]
            and not lines[-1].startswith(PANE_STATUS_BAR_PREFIXES)
        ):
            lines = lines[:-1]
        return lines

    def tail(self):
        """The end of the crew's terminal output, for crew that recorded no report."""
        try:
            lines = self.lines()
        except HERDR_ERRORS as exc:
            return f"unreadable ({exc})"
        start = modal_start(lines)
        if start is not None:
            # Drop the option list and confirm line; the question above them is the point.
            lines = lines[:start]
        lines = [
            line
            for line in lines
            if not PANE_RULE.match(line) and not PANE_EMPTY_PROMPT.match(line)
        ]
        tail = "\n".join(lines)
        return tail[-TAIL_LIMIT:] or "empty"

    def draft_pending(self, provider=None):
        """Treat an unreadable composer as potentially holding a user's draft."""
        try:
            return self.composer(provider) != ""
        except HERDR_ERRORS:
            return True

    def composer(self, provider=None):
        """Return a visible composer, or None when the screen cannot prove its contents."""
        lines = self.lines(keep_status=True, keep_blank=provider == "pi")
        while lines and not lines[-1]:
            lines.pop()
        if modal_start(lines) is not None:
            return None
        if provider == "pi":
            # Markdown rules are draft text; extra native rulers make the boundary ambiguous.
            borders = [
                index for index, line in enumerate(lines) if re.fullmatch(r"─{3,}|── .+─+", line)
            ]
            if (
                len(lines) >= 5
                and lines[-2].startswith(("/", "~"))
                and re.search(r"(?:[\d.]+%|\?)/[\d.]+[kKmM]?(?:\s|$)", lines[-1])
                and re.fullmatch(r"─{3,}", lines[-3])
                and borders == [len(lines) - 5, len(lines) - 3]
                and len(lines[-5]) == len(lines[-3])
                and not lines[-4]
            ):
                return ""
            return None
        if (
            provider == "claude"
            and len(lines) >= 4
            and lines[-1] == "⏸ manual mode on · ? for shortcuts · ← for agents"
        ):
            # Only this captured native placeholder; arbitrary "Try ..." text is a draft.
            if (
                lines[-3] == '❯\u00a0Try "how do I log an error?"'
                and re.fullmatch(r"─{3,}", lines[-4])
                and lines[-4] == lines[-2]
                and sum(bool(re.fullmatch(r"─{3,}", line)) for line in lines) == 2
            ):
                return ""
            lines.pop()
        if (
            provider == "codex"
            and len(lines) >= 3
            and re.fullmatch(r"\? for shortcuts(?:\s+⚠ \d+ warnings? · f2 to view)?", lines[-1])
            and re.match(r"^gpt-\S+ .* · [~/]", lines[-2], re.IGNORECASE)
        ):
            lines.pop()
        if lines and (
            re.match(r"^gpt-\S+ .* · [~/]", lines[-1], re.IGNORECASE)
            or (len(lines) > 1 and PANE_RULE.fullmatch(lines[-2]) and lines[-1].startswith("⏵⏵ "))
        ):
            lines.pop()
        # ponytail: recognize only an unobscured, single-line composer; fail closed otherwise.
        while lines and (PANE_RULE.fullmatch(lines[-1]) or lines[-1] == "? for shortcuts"):
            lines.pop()
        if not lines or lines[-1][:1] not in PROMPT_GLYPHS:
            return None
        return "" if PANE_EMPTY_PROMPT.fullmatch(lines[-1]) else lines[-1][1:].strip()

    def agent_status(self):
        """Herdr's own view of the agent: idle, working, done, or blocked."""
        agent = runtime.herdr("agent", "get", self.agent_name, timeout=5).get("agent", {})
        return agent.get("agent_status")

    def settled_status(self, timeout, provider=None):
        """Poll until the agent reports working, done, or blocked; return the last status seen.

        Codex reports working while it renames its own thread, so its activity counts as the
        task starting only once the composer no longer holds the draft.
        """
        deadline = time.monotonic() + timeout
        status = None
        while time.monotonic() < deadline:
            status = self.agent_status()
            if status == "blocked":
                return status
            if self.choice_modal():
                return "blocked"
            if status in ("working", "done"):
                if not self.draft_pending(provider):
                    return status
                status = "idle"
            time.sleep(POLL_INTERVAL)
        return status

    def task_landed(self, provider, timeout=SUBMIT_TIMEOUT):
        """Observe submission without typing into a potentially changed composer."""
        return self.settled_status(timeout, provider)

    def submit_task(self, task, provider, attempts=2):
        """Submit at most once; attempts is retained for compatibility, never for retries.

        Pane observations are not an execution receipt. In particular, idle after sending
        cannot distinguish a dropped prompt from a fast completed task.
        """
        status = self.agent_status()
        if status == "blocked" or self.choice_modal():
            raise CaptainError(
                f"{self.agent_name} is waiting for input or approval; no task was sent. "
                "Read its pane before sending any keys."
            )
        if status not in ("idle", "working", "done") or self.draft_pending(provider):
            raise CaptainError(
                f"{self.agent_name} has a draft or unreadable composer; no task was sent. "
                f"Inspect it with: herdr agent read {self.agent_name}"
            )
        try:
            # herdr has no "--" terminator; a leading space defuses a task that starts with "-".
            guarded = f" {task}" if task.startswith("-") else task
            runtime.herdr("agent", "prompt", self.agent_name, guarded)
            status = self.task_landed(provider)
        except HERDR_ERRORS as exc:
            raise CaptainError(
                f"{self.agent_name} delivery outcome is unknown ({exc}); no resend was attempted. "
                f"Inspect it with: herdr agent read {self.agent_name}"
            ) from exc
        if status in ("working", "done"):
            return
        raise CaptainError(
            f"{self.agent_name} delivery outcome is unknown (status {status!r}); "
            "the task may have run or remain an unsent draft. No resend or Enter was attempted. "
            f"Inspect it with: herdr agent read {self.agent_name}"
        )

    def wait_for_crew(self, pane_id, provider, timeout=30):
        """Wait for the native CLI to hold a settled idle state, not just to be detected.

        A TUI that has only just drawn itself silently drops a submitted prompt, so idle is
        trusted only after READY_POLLS consecutive polls.
        """
        deadline = time.monotonic() + timeout
        idle_polls = 0
        while time.monotonic() < deadline:
            pane = runtime.herdr("pane", "get", pane_id, timeout=5).get("pane", {})
            if pane.get("agent") == provider:
                status = pane.get("agent_status")
                idle_polls = idle_polls + 1 if status == "idle" else 0
                if status in ("done", "blocked") or idle_polls >= READY_POLLS:
                    runtime.herdr("agent", "rename", pane_id, self.agent_name)
                    actual_name = (
                        runtime.herdr("agent", "get", pane_id).get("agent", {}).get("name")
                    )
                    if actual_name != self.agent_name:
                        raise CaptainError(
                            f"Agent rename failed for pane {pane_id}: "
                            f"expected {self.agent_name!r}, got {actual_name!r}."
                        )
                    if status == "blocked":
                        raise CaptainError("The native agent is waiting for input or approval.")
                    return
            time.sleep(POLL_INTERVAL)
        raise CaptainError(f"{provider} did not become ready within {timeout} seconds.")

    def await_(self, match, timeout):
        """Poll the crew's pane until match(lines) returns something, or None on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            found = match(self.lines())
            if found is not None:
                return found
            if time.monotonic() >= deadline:
                return None
            time.sleep(MODEL_INTERVAL)

    def choice_modal(self):
        """Whether the pane is showing a native choice modal, which reads as idle to Herdr."""
        try:
            return modal_start(self.lines()) is not None
        except HERDR_ERRORS:
            return True

    def model_landed(self, names, timeout, provider=None):
        """Whether the pane shows the native CLI's own switch confirmation for this model.

        Each CLI echoes the new model, but Claude Code prints its display name ("Sonnet 5"),
        so any of the model's known names counts.
        """

        def confirmation(lines):
            for line in reversed(lines):
                lowered = line.casefold()
                confirmed = (
                    PI_MODEL_CONFIRMATION.match(line)
                    if provider == "pi"
                    else any(marker in lowered for marker in MODEL_CONFIRMATIONS)
                )
                if confirmed:
                    return any(name in lowered for name in names) or None
            return None

        return bool(self.await_(confirmation, timeout))

    def codex_pick_model(self, model, timeout):
        """Drive Codex's /model picker, which takes no argument and lists models by number."""

        def option(lines):
            for line in lines:
                found = PANE_MODAL_OPTION.match(line)
                if found and found.group(2) == model:
                    return found.group(1)
            return None

        def effort(lines):
            return any(line.startswith(CODEX_EFFORT_HEADER) for line in lines) or None

        digit = self.await_(option, timeout)
        if digit is None:
            raise CaptainError(
                f"Codex did not list {model} in its /model picker. Read the pane and close "
                f"the picker with esc: herdr agent read {self.agent_name}"
            )
        runtime.herdr("agent", "send-keys", self.agent_name, digit)
        # Choosing a model opens a reasoning-level list; Enter keeps the highlighted default.
        if self.await_(effort, timeout):
            runtime.herdr("agent", "send-keys", self.agent_name, "enter")
