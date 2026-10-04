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
# Each CLI's own line after a switch: "Set model to Sonnet 5 …" / "Model changed to …" /
# Grok's "Switched to …".
MODEL_CONFIRMATIONS = ("set model to", "model changed to", "switched to")
# pi instead echoes "Model: <id>" at line start; its status bar never carries "model:".
PI_MODEL_CONFIRMATION = re.compile(r"^\s*Model:\s")
CODEX_EFFORT_HEADER = "Select Reasoning Level"
# Claude Code says which of its two switches it made, and a retier may only ever be the
# one that left the user's own settings alone.
CLAUDE_SESSION_ONLY = "for this session only"
# Its /model picker, which has no search of its own: a numbered list of display names,
# opened focused on the model in use, and a footer naming the one key that switches
# without also saving. Several versions of a family are listed, so a row is the model
# asked for only when its whole label is, "Opus 5.5" never answering for "Opus 5".
CLAUDE_PICKER_HEADER = "Select model"
CLAUDE_PICKER_SESSION_ONLY = "use this session only"
CLAUDE_PICKER_SESSION_KEY = "s"
# A row, marked "❯" when it is the focused one. The scroll arrow of a truncated list sits
# in the same column and is not focus, and the model in use carries a mark of its own.
CLAUDE_PICKER_ROW = re.compile(
    r"^(?P<mark>❯)?\s*(?:[↓↑]\s*)?(?P<number>\d+)\.\s+(?P<label>.+?)(?:\s{2,}|$)"
)
CLAUDE_PICKER_IN_USE = "✔"
# The confirm modal Claude Code opens when a switch re-reads the conversation: its header,
# the "Yes, switch to <model>" option that names what is switched to, and the option that
# backs out. Yes is answered with Enter, since the digit lands in the composer.
CLAUDE_MODAL_HEADER = "Switch model?"
CLAUDE_MODAL_YES = re.compile(r"^(?P<mark>❯)?\s*1\.\s+Yes, switch to (?P<name>.+?)\s*$")
# What a truncated list keeps out of view ("… +2 models"), which moving down brings in.
CLAUDE_PICKER_MORE = re.compile(r"\+(\d+)\s+models?")
# One keypress' worth of redraw, polled rather than waited out.
CLAUDE_PICKER_STEP = 2
# How long a picker that has just opened may take to paint its focused row.
CLAUDE_PICKER_PAINT = 2
# Input-prompt glyphs of the native TUIs, which also open their status bars.
PROMPT_GLYPHS = ("❯", "›")
INTERRUPT_MARKER = "esc to interrupt"
# Claude Code's own signature for a prompt queued mid-turn, not a draft or a drop.
QUEUED_SEND_NOW = "ctrl+enter to send now"
QUEUED_COMPOSER = "Press up to edit queued messages"
# Pane chrome shared by the Claude Code and Codex TUIs: a bare box-drawing rule, an
# empty input prompt (provider placeholders need separate recognition),
# and the bottom status bar, which is always the last line and never starts like
# real transcript content (a bullet, a tree glyph, a spinner).
PANE_RULE = re.compile(r"^[─\-=━]+$")
PANE_EMPTY_PROMPT = re.compile(rf"^[{''.join(PROMPT_GLYPHS)}]\s*(Ask Codex to do anything)?$")
# Grok boxes its composer, so the prompt row is never the pane's last line: the box's
# bottom border is, carrying the model label ("╰── Grok 4.6 (high) ─╯").
GROK_COMPOSER_END = re.compile(r"^╰─+.*╯$")
GROK_COMPOSER_ROW = re.compile(rf"^│\s*[{''.join(PROMPT_GLYPHS)}]\s*(.*?)\s*│$")
PANE_STATUS_BAR_PREFIXES = ("•", "⏺", "└", "│", "✻", "✳", "?", "…", *PROMPT_GLYPHS, "⎿")
# Codex draws rate-limit and approval choices as a numbered list closed by a confirm
# line and keeps reporting the pane idle while one is showing. Enter would accept the
# default (silently switching the model), so a modal is needs-attention, not output.
PANE_MODAL_CONFIRM = re.compile(r"^Press enter to confirm or esc to \w+")
PANE_MODAL_OPTION = re.compile(r"^[›»]?\s*([1-9])\.\s+(\S+)")
PANE_MODAL_LINES = 20
# A contextual suggestion is drawn dim (SGR 2); a typed draft carries no styling at all.
# Group captures the whole parameter list so a multi-parameter code (a truecolor prefix,
# "38;2;153;153;153") is consumed as one sequence instead of failing to match at all.
ANSI_SGR = re.compile(r"\x1b\[([\d;]*)m")
# A shell startup hook (oh-my-zsh's update check, etc.) asks a yes/no question that
# swallows the first keystroke of anything typed before it is answered. A bare
# trailing "?" is not enough on its own; a normal prompt can end in one too.
SHELL_QUESTION = re.compile(r"(\[[YyNn]/[YyNn]\]|\([YyNn]/[YyNn]\))\s*$")
SHELL_READY_TIMEOUT = 3
# nudge_block's gate for unsubmitted human text, which protocol escalates rather than
# typing over.
DRAFT_GATE = "user draft"
# A composer the screen cannot prove the contents of is its own gate, not a draft: a TUI
# still painting at launch reads exactly like a human mid-sentence, and calling it a draft
# held one crew's whole task behind DRAFT_EXPIRY.
UNREADABLE_GATE = "unreadable composer"
# How long one unchanged draft may hold mail before the gate stops holding, from
# munder-difflin's STALE_INPUT_MS (terminalAutomation.ts:50). Without it an occupied
# composer holds mail for the rest of the session; protocol rings content-free once it
# expires, so an expiry never fuses a body onto a human's half-written sentence.
DRAFT_EXPIRY = 1800
# An unreadable composer only has to outlast the paint; an unrecognised composer shape
# never resolves at all, so waiting out DRAFT_EXPIRY for it buys nothing. Short enough
# that the next drain rings, long enough that a launching TUI is not typed over.
UNREADABLE_EXPIRY = 15
# Each gate protocol may age out, with how long it holds first.
HOLD_EXPIRY = {DRAFT_GATE: DRAFT_EXPIRY, UNREADABLE_GATE: UNREADABLE_EXPIRY}


# A hook-delivered crew's doorbell: one fixed string, safe to repeat and safe to drop,
# because its own hook supplies the body. Dropping it delays a turn, it loses nothing.
WAKE_LINE = "mail from the captain is waiting; keep working"


def inbox_line(crew):
    """The one line every ring ends with. Defined once: nudge types it, and nudge_block has to
    recognise it on screen to tell our own echo from a human's draft."""
    from .instructions import captain_command
    from .models import HOOK_DELIVERED

    if crew.record.get("provider") in HOOK_DELIVERED:
        return WAKE_LINE
    return (
        f"read your mail with `{captain_command(crew.session.directory.name)} inbox {crew.crew_id}`"
    )


def modal_start(lines):
    """Index of a trailing choice modal's first option, or None when there is none.

    The lines above it are the modal's question, which a blocked crew's tail must keep.
    """
    if not lines or not PANE_MODAL_CONFIRM.match(lines[-1]):
        return None
    window = range(max(len(lines) - PANE_MODAL_LINES, 0), len(lines) - 1)
    options = [index for index in window if PANE_MODAL_OPTION.match(lines[index])]
    return options[0] if options else None


def claude_picker_rows(lines):
    """Claude Code's /model picker rows as (number, label, focused), in listed order.

    A label is what the picker lists the model under, casefolded and without the mark it
    puts on the one in use, and never its description: the recommended row describes
    itself with another model's name.
    """
    rows = []
    for line in lines:
        row = CLAUDE_PICKER_ROW.match(line)
        if row:
            label = row.group("label").replace(CLAUDE_PICKER_IN_USE, "").strip().casefold()
            rows.append((int(row.group("number")), label, bool(row.group("mark"))))
    return rows


def claude_switch_state(lines, label):
    """Where Claude Code's switch to `label` stands: "modal", "applied", or None.

    Read from the bottom up and stopped at the picker's own header, so a confirmation left
    in the tail by an earlier switch can never stand in for this one.
    """
    for line in reversed(lines):
        if CLAUDE_MODAL_HEADER in line:
            return "modal"
        lowered = line.casefold()
        if "set model to" in lowered:
            return "applied" if f"set model to {label} for this session only" in lowered else None
        if CLAUDE_PICKER_HEADER in line:
            return None
    return None


def claude_picker_focused(rows):
    return next((row for row in rows if row[2]), None)


def claude_picker_steps(lines):
    """Twice the list's length, counting the rows a truncated list is holding out of view."""
    hidden = CLAUDE_PICKER_MORE.search(" ".join(lines))
    return 2 * (len(claude_picker_rows(lines)) + (int(hidden.group(1)) if hidden else 0))


def claude_picker_key(rows, label, focused):
    """Which way to move focus: toward the wanted row, or on down to the rows out of view."""
    wanted = next((row for row in rows if row[1] == label), None)
    return "up" if wanted and wanted[0] < focused[0] else "down"


def shell_ready_for_input(pane_id, timeout=SHELL_READY_TIMEOUT):
    """Whether a brand-new shell pane shows no persistent yes/no question.

    Only reports: a shell startup question is left for a human, never answered or
    typed into. Blank, changing, or unreadable panes fail open so launch is not
    blocked by inconclusive observations.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            output = runtime.herdr("pane", "read", pane_id, "--lines", "5", raw=True, timeout=10)
        except HERDR_ERRORS:
            return True
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if not lines or not SHELL_QUESTION.search(lines[-1]):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)


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
        """Whether the composer must not be typed into: any gate, including an unreadable one."""
        try:
            return self._gate(provider) is not None
        except HERDR_ERRORS:
            return True

    def _gate(self, provider=None, echo=None):
        """Which gate holds the composer, or None when it is provably safe to type.

        Empty and unreadable are different facts: an empty composer is no gate at all, and
        an unreadable one gets its own, which ages out on UNREADABLE_EXPIRY rather than
        posing as a human's draft for half an hour.
        """
        text, styled = self._composer(provider)
        if text is None:
            return UNREADABLE_GATE
        # A composer still showing the ring we typed is ours, not a draft; otherwise a landed
        # ring gates the next one. Only an exact match counts, so a wrong guess parks mail
        # rather than typing over a human. The wake line is ours whoever asks: a hook-delivered
        # crew's pane keeps rendering it after submission, and every reader but the ring itself
        # passes no echo, so without this a landed doorbell gates `model` for the whole session.
        if not text or text == echo or text == WAKE_LINE:
            return None
        return None if self._dim_suggestion(text, styled) else DRAFT_GATE

    def _dim_suggestion(self, text, lines):
        """Whether the composer's plain-text content is really a dim native suggestion.

        The native TUI dims a suggestion (SGR 2) between the prompt glyph and the text,
        and never dims what the user actually typed. Only that span decides it; a coloured
        glyph or a trailing reset elsewhere on the line must not change the verdict.
        """
        for line in lines:
            stripped = ANSI_SGR.sub("", line)
            if stripped[:1] not in PROMPT_GLYPHS or stripped[1:].strip() != text:
                continue
            after = line[line.index(stripped[0]) + 1 :].lstrip("\xa0")
            codes = []
            while True:
                match = ANSI_SGR.match(after)
                if not match:
                    break
                codes.extend(match.group(1).split(";") if match.group(1) else ["0"])
                after = after[match.end() :]
            return bool(codes) and "2" in codes and all(code in ("0", "2") for code in codes)
        return False

    def composer(self, provider=None):
        """Return a visible composer, or None when the screen cannot prove its contents."""
        return self._composer(provider)[0]

    def _composer(self, provider=None):
        """Read the composer once, retaining styling for the suggestion verdict."""
        output = runtime.herdr(
            "agent",
            "read",
            self.agent_name,
            "--lines",
            str(TAIL_LINES),
            "--format",
            "ansi",
            raw=True,
            timeout=10,
        )
        styled = output.splitlines()
        lines = [
            ANSI_SGR.sub("", line).strip()
            for line in styled
            if provider == "pi" or ANSI_SGR.sub("", line).strip()
        ]
        while lines and not lines[-1]:
            lines.pop()
        if modal_start(lines) is not None:
            return None, styled
        if provider == "grok":
            # A wrapped draft leaves no prompt row above the border, so it reads unreadable.
            row = GROK_COMPOSER_ROW.fullmatch(lines[-2]) if len(lines) >= 2 else None
            if row and GROK_COMPOSER_END.fullmatch(lines[-1]):
                return row.group(1), styled
            return None, styled
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
                return "", styled
            return None, styled
        if (
            provider == "claude"
            and len(lines) >= 4
            and lines[-1]
            in (
                "⏸ manual mode on · ? for shortcuts · ← for agents",
                "⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
            )
        ):
            # Only this captured native placeholder; arbitrary "Try ..." text is a draft.
            if (
                lines[-3] == '❯\u00a0Try "how do I log an error?"'
                and re.fullmatch(r"─{3,}", lines[-4])
                and lines[-4] == lines[-2]
                and sum(bool(re.fullmatch(r"─{3,}", line)) for line in lines) == 2
            ):
                return "", styled
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
            return None, styled
        text = "" if PANE_EMPTY_PROMPT.fullmatch(lines[-1]) else lines[-1][1:].strip()
        return text, styled

    def agent_status(self):
        """Herdr's own view of the agent: idle, working, done, or blocked."""
        agent = runtime.herdr("agent", "get", self.agent_name, timeout=5).get("agent", {})
        return agent.get("agent_status")

    def nudge_block(self, crew):
        """Return the live pane gate holding a mail nudge, if any."""
        status = self.agent_status()
        if status is None:
            raise CaptainError(f"{self.agent_name} is not registered.")
        if status == "blocked" or modal_start(self.lines()) is not None:
            return "approval prompt"
        # Herdr reports a crew that just finished a turn as "done", not "idle"; it is as
        # ringable as idle, and every other status check here already pairs the two.
        # A busy crew holds the ring whatever its provider: a mid-turn pane has no
        # provable composer, so _gate below would hold it regardless, and
        # overriding that too means typing into a line that may hold a human's text.
        # Its mail arrives at the turn boundary, via the `done` refusal or the Stop hook.
        if status not in ("idle", "done"):
            return "agent not idle"
        return self._gate(crew.record.get("provider"), echo=inbox_line(crew))

    def draft(self, provider=None):
        """What a reported gate is holding, for the stamp that ages it out.

        None when the screen cannot prove the composer's contents; that is the unreadable
        gate's own hold, aged on its own expiry, never a draft an editing human owns.
        """
        try:
            return self._composer(provider)[0]
        except HERDR_ERRORS:
            return None

    def nudge(self, crew, text=None):
        """Ring an idle crew. A first ring may carry the mail body as a trusted prompt."""
        body = f"{text}\n{inbox_line(crew)}" if text else inbox_line(crew)
        runtime.herdr(
            "agent",
            "prompt",
            self.agent_name,
            f" {body}" if body.startswith("-") else body,
        )

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
            elif self.message_queued():
                return "working"
            time.sleep(POLL_INTERVAL)
        return status

    def message_queued(self):
        """Whether the pane shows Claude Code's own mid-turn message-queue signature.

        A prompt that arrives while the previous turn is still running gets queued and
        flushed once it ends, so it landed even though agent_status reads idle for the
        whole window; this composer/footer trio is the only proof of that.
        """
        try:
            lines = self.lines()
        except HERDR_ERRORS:
            return False
        return (
            any(QUEUED_SEND_NOW in line for line in lines)
            and any(QUEUED_COMPOSER in line for line in lines)
            and any(INTERRUPT_MARKER in line for line in lines)
        )

    def submit_task(self, task, provider):
        """Submit at most once; never retry.

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
            status = self.settled_status(SUBMIT_TIMEOUT, provider)
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

    def await_(self, match, timeout, keep_status=False):
        """Poll the crew's pane until match(lines) returns something, or None on timeout.

        A native picker draws its key hints on the last line, which reads exactly like a
        status bar, so looking for one needs the tail kept whole.
        """
        deadline = time.monotonic() + timeout
        while True:
            found = match(self.lines(keep_status=keep_status))
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

    def model_landed(self, names, timeout, provider=None, after=0):
        """Whether the pane shows the native CLI's own switch confirmation for this model.

        Each CLI echoes the new model, but Claude Code prints its display name ("Sonnet 5"),
        so any of the model's known names counts. Claude Code's own line also says whether
        it saved the model as the user's default, and that switch is never a confirmation.
        """

        def confirmation(lines):
            # A tail that scrolled past the picker can still hold an earlier session-only
            # line, so a claude switch counts only once a new one has printed since `after`.
            if provider == "claude" and sum(CLAUDE_SESSION_ONLY in line for line in lines) <= after:
                return None
            for line in reversed(lines):
                if provider == "claude" and CLAUDE_PICKER_HEADER in line:
                    return None
                lowered = line.casefold()
                confirmed = (
                    PI_MODEL_CONFIRMATION.match(line)
                    if provider == "pi"
                    else any(marker in lowered for marker in MODEL_CONFIRMATIONS)
                )
                if confirmed:
                    # A line saying the model was saved as the default means Claude Code
                    # already wrote it to the user's settings; that is refused, not counted.
                    if provider == "claude" and CLAUDE_SESSION_ONLY not in lowered:
                        return None
                    return any(name in lowered for name in names) or None
            return None

        return bool(self.await_(confirmation, timeout))

    def claude_session_count(self):
        """How many session-only confirmations the pane's tail holds right now."""
        return sum(CLAUDE_SESSION_ONLY in line for line in self.lines(keep_status=True))

    def claude_picker_open(self, timeout):
        """Claude Code's /model picker once it is up and listing rows, or no lines at all."""

        def opened(lines):
            listed = claude_picker_rows(lines) and any(
                CLAUDE_PICKER_HEADER in line for line in lines
            )
            return lines if listed else None

        return self.await_(opened, timeout, keep_status=True) or []

    def claude_pick_model(self, label, timeout):
        """Drive Claude Code's /model picker to a switch this crew's session alone.

        `/model <id>` sets the model and saves it as the user's default for new sessions;
        the picker's own session-only key is the one delivery that does not. It has no
        search, and it opens focused on the model in use, so focus is moved one keypress
        at a time and that key is pressed only on a row the pane has just shown as focused
        and labelled exactly the model asked for. A move selects nothing, so every other
        outcome refuses having changed which row is focused and nothing else.
        """
        screen = self.claude_picker_open(timeout)
        if not screen:
            raise CaptainError(
                f"Claude Code did not open its /model picker. Read the pane before "
                f"retrying: herdr agent read {self.agent_name}"
            )
        if not claude_picker_focused(claude_picker_rows(screen)):
            # A picker that has just opened may still be painting its focus marker.
            screen = (
                self.await_(
                    lambda lines: (
                        lines if claude_picker_focused(claude_picker_rows(lines)) else None
                    ),
                    CLAUDE_PICKER_PAINT,
                    keep_status=True,
                )
                or screen
            )
        rows = claude_picker_rows(screen)
        focused = claude_picker_focused(rows)
        for _ in range(claude_picker_steps(screen)):
            if not focused:
                raise CaptainError(
                    f"Claude Code's picker is drawing no focused row, so none of them can be "
                    f"pressed. Read the pane and close the picker with esc: "
                    f"herdr agent read {self.agent_name}"
                )
            if focused[1] == label:
                if not self.await_(
                    lambda lines: any(CLAUDE_PICKER_SESSION_ONLY in line for line in lines) or None,
                    CLAUDE_PICKER_STEP,
                    keep_status=True,
                ):
                    raise CaptainError(
                        f"Claude Code's picker offered no session-only switch on its "
                        f"'{label}' row, so it was never pressed. Read the pane and close "
                        f"the picker with esc: herdr agent read {self.agent_name}"
                    )
                baseline = self.claude_session_count()
                runtime.herdr("agent", "send-keys", self.agent_name, CLAUDE_PICKER_SESSION_KEY)
                self.claude_confirm(label, timeout)
                return baseline
            runtime.herdr(
                "agent", "send-keys", self.agent_name, claude_picker_key(rows, label, focused)
            )
            moved = self.claude_picker_moved(focused)
            if not moved:
                raise CaptainError(
                    f"Claude Code's picker kept focus on its '{focused[1]}' row instead of "
                    f"moving towards '{label}', so nothing was pressed. Read the pane and "
                    f"close the picker with esc: herdr agent read {self.agent_name}"
                )
            rows, focused = moved, claude_picker_focused(moved)
        raise CaptainError(
            f"Claude Code's picker never focused a '{label}' row, stopping on "
            f"'{focused[1]}'. Nothing was pressed; read the pane and close the picker "
            f"with esc: herdr agent read {self.agent_name}"
        )

    def claude_confirm(self, label, timeout):
        """Settle Claude Code's answer to `s`: an applied switch, or its confirm modal.

        With no modal the switch is already applied and its own line names it, so there is
        nothing to answer. A modal is answered only when it names the model asked for and its
        "Yes" is the focused option, with Enter: the digit lands in the composer instead.
        Nothing here proves the switch stayed in this session. model_landed reads the line
        Claude Code prints after it, which must say "for this session only".
        """

        def settled(lines):
            state = claude_switch_state(lines, label)
            return (state, lines) if state else None

        found = self.await_(settled, timeout, keep_status=True)
        if not found or found[0] == "applied":
            return
        option = next(
            (
                match
                for match in (CLAUDE_MODAL_YES.match(line) for line in reversed(found[1]))
                if match
            ),
            None,
        )
        if option is None:
            raise CaptainError(
                "Claude Code's confirm could not be read: no 'Yes, switch to' option is on "
                "the pane, so nothing was confirmed. Read the pane and press esc to go back: "
                f"herdr agent read {self.agent_name}"
            )
        if option.group("name").casefold() != label:
            raise CaptainError(
                f"Claude Code's confirm asks to switch to {option.group('name')}, not "
                f"'{label}', so nothing was confirmed. Read the pane and press esc to go "
                f"back: herdr agent read {self.agent_name}"
            )
        if not option.group("mark"):
            raise CaptainError(
                f"Claude Code's confirm has its Yes option unfocused, so nothing was "
                f"confirmed. Read the pane and press esc to go back: "
                f"herdr agent read {self.agent_name}"
            )
        runtime.herdr("agent", "send-keys", self.agent_name, "enter")

    def claude_picker_moved(self, off):
        """The picker's rows once its focus has left the `off` row, or none on timeout."""

        def moved(lines):
            rows = claude_picker_rows(lines)
            return rows if rows and claude_picker_focused(rows) not in (None, off) else None

        return self.await_(moved, CLAUDE_PICKER_STEP, keep_status=True) or []

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
