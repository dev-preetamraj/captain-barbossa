"""Small keyboard selectors for native agent launches."""

import os
import sys

import questionary
from prompt_toolkit.utils import get_cwidth

from .runtime import CaptainError

LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "pi": "Pi",
    "grok": "Grok",
    "pane": "New pane",
    "tab": "New tab",
    "vertical": "Vertical (side by side)",
    "horizontal": "Horizontal (stacked)",
    "auto": "Auto (next slot in the tab shape)",
}
PLACEMENTS = ("pane", "tab")
SESSION_CARDS = 5
STYLE = questionary.Style(
    [
        ("question", "bold"),
        ("pointer", "fg:#8bd5b5 bold"),
        ("highlighted", "fg:#8bd5b5 bold noreverse"),
        ("answer", "fg:#8bd5b5 bold"),
        ("separator", "fg:#808890"),
    ]
)

# Use the terminal's foreground/background and ANSI palette, including on light themes.
SESSION_STYLE = questionary.Style(
    [
        ("heading", "bold"),
        ("accent", "fg:ansired"),
        ("selected", "bold noreverse"),
        ("muted", "fg:ansibrightblack"),
    ]
)


def session_frame(project, cards, query, cursor, top, rows, size=SESSION_CARDS, width=80):
    """Style/text fragments of the resume picker; cards are the (value, title, meta) matches."""
    width = max(4, width)
    search = query or "search"
    while get_cwidth(search) > width - 2:
        search = search[1:] if query else search[:-1]
    out = [
        ("class:accent", "☠ "),
        ("class:heading", "resume"),
        ("class:muted", f" {project} · {len(cards)}/{rows}\n\n"),
        ("class:muted", "/ "),
        ("" if query else "class:muted", search),
        ("", "\n\n"),
    ]
    shown = cards[top : top + size]
    below = len(cards) - top - len(shown)
    out.append(("class:muted", f"  ↑ {top} more\n" if top else "\n"))
    for index, (_, title, meta) in enumerate(shown, top):
        picked = index == cursor
        marker = ("class:accent", " ▌ ") if picked else ("", "   ")
        out += [
            marker,
            ("class:selected" if picked else "", f"{title}\n"),
            marker,
            ("class:muted", f"{meta}\n\n"),
        ]
    if not shown:
        out.append(("class:muted", "   No matching sessions\n\n"))
    out.append(("class:muted", f"  ↓ {below} more\n" if below else "\n"))
    out.append(("class:muted", "↑↓ move · Enter resume · Esc cancel"))
    return out


def pick_session(project, cards):
    """Searchable two-line session cards; returns the chosen value or None on cancel."""
    from prompt_toolkit import Application
    from prompt_toolkit.application import get_app
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.output import ColorDepth

    state = {"query": "", "cursor": 0, "top": 0}

    def matches():
        needle = state["query"].casefold()
        return [card for card in cards if needle in card[1].casefold()]

    def render():
        found = matches()
        # Reserve chrome and a spare terminal line; each card takes 3 lines.
        terminal = get_app().output.get_size()
        size = max(1, (terminal.rows - 9) // 3)
        state["cursor"] = max(0, min(state["cursor"], len(found) - 1))
        top = min(state["top"], state["cursor"])
        state["top"] = max(top, state["cursor"] - size + 1)
        return session_frame(
            project,
            found,
            state["query"],
            state["cursor"],
            state["top"],
            len(cards),
            size,
            terminal.columns,
        )

    keys = KeyBindings()

    def move(step):
        def handler(event):
            state["cursor"] += step

        return handler

    keys.add("up")(move(-1))
    keys.add("down")(move(1))

    @keys.add("<any>")
    def type_char(event):
        char = event.data
        if char.isprintable():
            state.update(query=state["query"] + char, cursor=0, top=0)

    @keys.add("backspace")
    def erase(event):
        state.update(query=state["query"][:-1], cursor=0, top=0)

    @keys.add("enter")
    def accept(event):
        found = matches()
        event.app.exit(result=found[state["cursor"]][0] if found else None)

    @keys.add("escape")
    @keys.add("c-c")
    @keys.add("c-d")
    def cancel(event):
        event.app.exit(result=None)

    app = Application(
        layout=Layout(Window(FormattedTextControl(render, show_cursor=False), wrap_lines=False)),
        key_bindings=keys,
        style=SESSION_STYLE,
        color_depth=ColorDepth.DEPTH_1_BIT if os.environ.get("NO_COLOR") else None,
        full_screen=False,
    )
    return app.run()


def choose(value, options, question, flag, labels=None, groups=None, setting=None):
    """Pick one of options; groups is an optional list of (title, options) for grouped display.

    setting names the settings key that would answer this for good, e.g. "[crew] agent".
    """
    if value:
        return value
    grouped = groups or [(None, options)]
    if not sys.stdin.isatty():
        listed = []
        for title, members in grouped:
            items = " / ".join(o if labels is None else f"{o} {labels[o]}" for o in members)
            listed.append(f"{title}: {items}" if title else items)
        lasting = (
            f" Setting {setting} in .captain/settings.toml stops this question." if setting else ""
        )
        raise CaptainError(
            f"Ask the user: {question} ({'; '.join(listed)}). Wait for their answer, "
            f"then rerun with {flag} <choice>. Nothing was created.{lasting}"
        )
    choices = [questionary.Separator(" ")]
    for title, members in grouped:
        if title:
            choices.append(questionary.Separator(title))
        choices.extend(
            questionary.Choice((labels or LABELS)[option], value=option) for option in members
        )
    choices += [
        questionary.Separator(" "),
        questionary.Separator("↑↓ / j k   move"),
        questionary.Separator("Enter select · Esc cancel"),
        questionary.Separator(" "),
    ]
    prompt = questionary.select(
        f"{question} ({setting} in .captain/settings.toml skips this)" if setting else question,
        choices=choices,
        qmark="\n ",
        pointer="   ›",
        style=STYLE,
        instruction=" ",
        use_arrow_keys=True,
        use_jk_keys=True,
        use_emacs_keys=False,
    )

    @prompt.application.key_bindings.add("escape")
    @prompt.application.key_bindings.add("c-d")
    def cancel(event):
        event.app.exit(result=None)

    answer = prompt.unsafe_ask()
    if answer is None:
        raise CaptainError("Launch cancelled. Nothing was created.")
    return answer
