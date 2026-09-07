"""Small keyboard selectors for native agent launches."""

import sys

import questionary

from .runtime import CaptainError

LABELS = {"claude": "Claude Code", "codex": "Codex", "pane": "New pane", "tab": "New tab"}
STYLE = questionary.Style(
    [
        ("question", "bold"),
        ("pointer", "fg:#8bd5b5 bold"),
        ("highlighted", "fg:#8bd5b5 bold noreverse"),
        ("answer", "fg:#8bd5b5 bold"),
        ("separator", "fg:#808890"),
    ]
)


def choose(value, options, question, flag):
    if value:
        return value
    if not sys.stdin.isatty():
        raise CaptainError(
            f"Ask the user: {question} ({' / '.join(options)}). Wait for their answer, "
            f"then rerun with {flag} <choice>. Nothing was created."
        )
    choices = [
        questionary.Separator(" "),
        *(questionary.Choice(LABELS[option], value=option) for option in options),
        questionary.Separator(" "),
        questionary.Separator("↑↓ / j k   move"),
        questionary.Separator("Enter select · Esc cancel"),
        questionary.Separator(" "),
    ]
    prompt = questionary.select(
        question,
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
