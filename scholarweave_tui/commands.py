"""Slash commands for the composer.

The cockpit keeps its chrome quiet, so anything that would otherwise need a
control lives here: type `/` in the composer and the same list the help sheet
shows becomes searchable. Commands carry their own argument vocabulary so the
menu can complete `/effort thorough` without a second screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Command:
    """One slash command: what it is called, what it does, what it accepts."""

    name: str
    summary: str
    arguments: tuple[str, ...] = field(default=())
    argument_hint: str = ""

    @property
    def usage(self) -> str:
        return f"/{self.name}" + (f" {self.argument_hint}" if self.argument_hint else "")


COMMANDS: tuple[Command, ...] = (
    Command("model", "Choose the model for new conversations"),
    Command(
        "reasoning", "Set how hard the model thinks", argument_hint="[level]",
    ),
    Command(
        "effort", "Effort for your next message",
        arguments=("auto", "quick", "thorough"), argument_hint="auto | quick | thorough",
    ),
    Command(
        "web", "Allow web sources for your next message",
        arguments=("on", "off"), argument_hint="on | off",
    ),
    Command("focus", "Toggle focus mode and the side rails"),
    Command("new", "Start a new conversation or note"),
    Command("papers", "Open the paper library"),
    Command("notes", "Open your knowledge notes"),
    Command("chat", "Back to the conversation"),
    Command("stop", "Stop the run that is streaming"),
    Command("refresh", "Reload everything from your local backend"),
    Command("theme", "Change the colour theme"),
    Command("status", "Show what this message will use"),
    Command("help", "Keyboard shortcuts and commands"),
    Command("quit", "Leave the cockpit"),
)
BY_NAME = {command.name: command for command in COMMANDS}


def is_command(text: str) -> bool:
    """True while the composer holds a single-line slash command."""

    return text.startswith("/") and "\n" not in text


def parse(text: str) -> tuple[str, str]:
    """Split composer text into a command name and its remaining argument."""

    name, _, argument = text[1:].strip().partition(" ")
    return name.casefold(), argument.strip()


def matches(text: str) -> list[tuple[Command, str]]:
    """Commands to offer for the typed text, each with the argument to apply.

    Typing `/eff` offers the effort command; `/effort q` narrows to its `quick`
    value so one Enter both completes and runs it.
    """

    name, argument = parse(text)
    if not argument and not text.endswith(" "):
        return [(command, "") for command in COMMANDS if command.name.startswith(name)]
    command = BY_NAME.get(name)
    if command is None:
        return []
    if not command.arguments:
        return [(command, argument)]
    values = [value for value in command.arguments if value.startswith(argument.casefold())]
    return [(command, value) for value in values] or [(command, "")]
