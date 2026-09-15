from __future__ import annotations

from dataclasses import dataclass

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Markdown, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option

from backend.providers.reasoning import REASONING_EFFORTS, ReasoningEffort
from backend.providers.schemas import ProviderKind, ProviderResponse
from scholarweave_tui.commands import COMMANDS

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def palette(widget: Static | Vertical, name: str, fallback: str) -> str:
    """Resolve a theme colour so Rich renderables follow the active theme."""

    variables = getattr(widget.app, "theme_variables", None) or {}
    value = variables.get(name)
    return value if isinstance(value, str) and value.startswith("#") else fallback


class Spinner(Static):
    """Inline activity indicator: a quiet glyph plus a plain-language label."""

    DEFAULT_CSS = "Spinner { height: 1; width: 1fr; }"

    label = reactive("Working", init=False)

    def __init__(self, label: str = "Working", *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self._frame = 0
        self.set_reactive(Spinner.label, label)

    def on_mount(self) -> None:
        self.set_interval(1 / 12, self._advance)

    def _advance(self) -> None:
        if not self.display or not self.is_mounted:
            return
        self._frame = (self._frame + 1) % len(SPINNER_FRAMES)
        self.refresh()

    def watch_label(self) -> None:
        self.refresh()

    def render(self) -> Text:
        text = Text()
        text.append(
            SPINNER_FRAMES[self._frame] + "  ",
            style=Style(color=palette(self, "accent", "#e6ad83"), bold=True),
        )
        text.append(self.label, style=Style(color=palette(self, "text-muted", "#9babae")))
        return text


class Masthead(Static):
    """Brand mark. Collapses to the monogram when the terminal gets narrow."""

    compact = reactive(False)

    def render(self) -> Text:
        accent = Style(color=palette(self, "primary", "#e6ad83"), bold=True)
        paper = Style(color=palette(self, "foreground", "#eee8dc"), bold=True)
        muted = Style(color=palette(self, "text-muted", "#9babae"))
        text = Text()
        text.append(" ◈ ", style=accent)
        if self.compact:
            text.append("SW", style=paper)
            return text
        text.append("SCHOLAR", style=paper)
        text.append("WEAVE", style=accent)
        text.append("  ·  the research cockpit", style=muted)
        return text


class Welcome(Vertical):
    """First-run canvas: what this place is for, and how to start."""

    STARTERS = (
        ("What does my library say about", "attention sparsity?"),
        ("Summarise", "the paper I read last."),
        ("Find and compare", "two recent takes on this idea."),
    )

    def compose(self) -> ComposeResult:
        yield Static("Space for deeper thinking.", id="welcome-title")
        yield Static(
            "Ask a question. Your papers, notes and the web are already within reach.",
            id="welcome-copy",
        )
        with Vertical(id="welcome-starters"):
            for lead, rest in self.STARTERS:
                line = Text("› ", style=Style(color=palette(self, "primary", "#e6ad83")))
                line.append(lead + " ", style=Style(bold=True))
                line.append(rest, style=Style(color=palette(self, "text-muted", "#9babae")))
                yield Static(line, classes="starter")
        yield Static("Type  /  for commands        Enter  sends", id="welcome-hint")


class TranscriptNote(Center):
    """An independently centered activity panel or run notice."""

    def __init__(self, content: str, *, classes: str) -> None:
        super().__init__(classes=classes)
        self._body = Static(content, classes="note-body", markup=False)

    def compose(self) -> ComposeResult:
        yield self._body

    def update(self, content: str) -> None:
        self._body.update(content)


class ChatMessage(Center):
    """One turn in the transcript, with a loading state for live answers."""

    def __init__(
        self,
        role: str,
        content: str,
        *,
        id: str | None = None,
        thinking: bool = False,
        usage: str = "",
    ) -> None:
        super().__init__(id=id, classes=f"message {role}")
        self.role = role
        self.content = content
        self._thinking = thinking
        self._usage = usage
        self._initial_content = content
        self._markdown = Markdown(content, open_links=False)
        self._spinner = Spinner("Following the thread", classes="message-spinner")
        self._usage_widget = Static(usage, classes="message-usage", markup=False)

    def compose(self) -> ComposeResult:
        with Vertical(classes="message-body"):
            yield Label("You" if self.role == "user" else "ScholarWeave", classes="message-role")
            yield self._markdown
            yield self._spinner
            yield self._usage_widget

    async def on_mount(self) -> None:
        if self.content != self._initial_content:
            await self._markdown.update(self.content)
        self._apply_state()

    async def set_content(self, content: str, *, thinking: bool = False, label: str | None = None) -> None:
        self.content = content
        self._thinking = thinking
        if label is not None:
            self.set_activity_label(label)
        if self._markdown.is_attached:
            await self._markdown.update(content)
        self._apply_state()

    def set_activity_label(self, label: str) -> None:
        self._spinner.label = label

    def set_usage(self, usage: str) -> None:
        self._usage = usage
        self._usage_widget.update(usage)
        self._usage_widget.display = bool(usage)

    def _apply_state(self) -> None:
        self._markdown.display = not self._thinking
        self._spinner.display = self._thinking
        self._usage_widget.display = bool(self._usage)
        self.set_class(
            len(self.content) >= 700 or self.content.count("\n") >= 10,
            "long",
        )


class Composer(TextArea):
    """Chat input where Enter sends, Shift+Enter keeps writing, and / commands.

    While the command menu is open the composer hands its navigation keys to
    the app so one Enter completes and runs the highlighted command.
    """

    NEWLINE_KEYS = {"shift+enter", "ctrl+j", "alt+enter"}
    MENU_KEYS = {"up", "down", "tab", "escape"}

    class Submitted(Message):
        """Posted when the researcher asks to send what they have written."""

    class MenuKey(Message):
        """Posted for a key the open command menu should handle."""

        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.menu_open = False

    async def _on_key(self, event: events.Key) -> None:
        if self.menu_open and event.key in self.MENU_KEYS:
            event.prevent_default()
            event.stop()
            self.post_message(self.MenuKey(event.key))
            return
        if event.key == "enter" and not self.read_only:
            event.prevent_default()
            event.stop()
            self.post_message(self.MenuKey("enter") if self.menu_open else self.Submitted())
            return
        if event.key in self.NEWLINE_KEYS:
            event.prevent_default()
            event.stop()
            if not self.read_only:
                self.insert("\n")
            return
        await super()._on_key(event)


class ConfirmDiscard(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "dismiss(False)", "Keep editing"),
        Binding("enter", "keep", "Keep editing", show=False, priority=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Keep your thinking.", classes="dialog-title")
            yield Static("This note has unsaved changes. Discard them?", classes="dialog-body")
            with Horizontal(classes="dialog-actions"):
                yield Button("Discard", id="discard", variant="error")
                yield Button("Keep editing", id="keep", variant="primary")
            yield Static("Enter  keep editing        Esc  keep editing", classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one("#keep", Button).focus()

    def action_keep(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "discard")


class NoteName(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("A place for a new idea.", classes="dialog-title")
            yield Static("Notes are plain Markdown in your local workspace.", classes="dialog-body")
            yield Input(placeholder="Name your note", max_length=300, id="note-name")
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Create note", id="create", variant="primary")
            yield Static("Enter  create        Esc  cancel", classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            self._submit()

    def _submit(self) -> None:
        name = self.query_one(Input).value.strip()
        if name:
            self.dismiss(name)
        else:
            self.notify("Give this note a name.", severity="warning")


@dataclass(frozen=True)
class ProviderFormResult:
    name: str
    kind: ProviderKind
    base_url: str
    api_key: str | None


class ProviderForm(ModalScreen[ProviderFormResult | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]
    KINDS: tuple[tuple[str, ProviderKind], ...] = (
        ("Ollama", "ollama"),
        ("OpenAI", "openai"),
        ("Azure OpenAI", "azure_openai"),
        ("Azure Foundry", "azure_foundry"),
        ("OpenAI compatible", "openai_compatible"),
    )

    def __init__(self, provider: ProviderResponse | None = None) -> None:
        super().__init__()
        self._provider = provider

    def compose(self) -> ComposeResult:
        provider = self._provider
        with Vertical(classes="dialog provider-dialog"):
            yield Label(
                "Configure provider." if provider else "Add a model provider.",
                classes="dialog-title",
            )
            yield Static(
                "ScholarWeave connects through the OpenAI-compatible Chat Completions protocol.",
                classes="dialog-body",
            )
            yield Label("Profile name", classes="form-label")
            yield Input(
                value=provider.name if provider else "Local Ollama",
                placeholder="Research models",
                max_length=120,
                id="provider-name",
            )
            yield Label("Provider kind", classes="form-label")
            yield Select(
                self.KINDS,
                value=provider.kind if provider else "ollama",
                allow_blank=False,
                id="provider-kind",
            )
            yield Label("Base URL", classes="form-label")
            yield Input(
                value=provider.base_url if provider else "http://127.0.0.1:11434",
                placeholder="http://127.0.0.1:11434",
                max_length=1024,
                id="provider-url",
            )
            yield Label(
                "API key" + (" (leave blank to keep the saved key)" if provider else ""),
                classes="form-label",
            )
            yield Input(
                placeholder="Optional for Ollama and OpenAI-compatible servers",
                password=True,
                max_length=16_384,
                id="provider-key",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="cancel-provider")
                yield Button("Save and discover", id="save-provider", variant="primary")
            yield Static("Enter  save        Esc  cancel", classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one("#provider-name", Input).focus()

    def on_input_submitted(self) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-provider":
            self.dismiss(None)
        else:
            self._submit()

    def _submit(self) -> None:
        name = self.query_one("#provider-name", Input).value.strip()
        base_url = self.query_one("#provider-url", Input).value.strip()
        kind = self.query_one("#provider-kind", Select).value
        api_key = self.query_one("#provider-key", Input).value.strip() or None
        if not name:
            self.notify("Give this provider a profile name.", severity="warning")
            return
        if not base_url:
            self.notify("Enter the provider's base URL.", severity="warning")
            return
        valid_kinds = {value for _, value in self.KINDS}
        if kind not in valid_kinds:
            self.notify("Choose a provider kind.", severity="warning")
            return
        if (
            kind not in {"ollama", "openai_compatible"}
            and not api_key
            and not (self._provider and self._provider.api_key_set)
        ):
            self.notify("This provider requires an API key.", severity="warning")
            return
        self.dismiss(ProviderFormResult(
            name=name,
            kind=kind,
            base_url=base_url,
            api_key=api_key,
        ))


@dataclass(frozen=True)
class ModelConfigResult:
    reasoning_effort: ReasoningEffort | None
    context_window_tokens: int
    enabled: bool


class ModelConfig(ModalScreen[ModelConfigResult | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]
    DEFAULT_REASONING = "__provider_default__"

    def __init__(
        self,
        *,
        provider_name: str,
        model_name: str,
        reasoning_efforts: list[ReasoningEffort],
        reasoning_effort: ReasoningEffort | None,
        context_window_tokens: int,
        enabled: bool = True,
    ) -> None:
        super().__init__()
        self._provider_name = provider_name
        self._model_name = model_name
        self._reasoning_efforts = reasoning_efforts
        self._reasoning_effort = reasoning_effort
        self._context_window_tokens = context_window_tokens
        self._enabled = enabled

    def compose(self) -> ComposeResult:
        reasoning_options = [("Provider default", self.DEFAULT_REASONING)]
        reasoning_options.extend(
            (effort.replace("xhigh", "extra high").title(), effort)
            for effort in REASONING_EFFORTS
            if effort in self._reasoning_efforts
        )
        with Vertical(classes="dialog model-dialog"):
            yield Label("Configure this model.", classes="dialog-title")
            yield Static(
                f"{self._provider_name} / {self._model_name}",
                classes="dialog-body",
            )
            yield Checkbox("Enabled for use", value=self._enabled, id="model-enabled")
            yield Label("Reasoning budget", classes="form-label")
            yield Select(
                reasoning_options,
                value=self._reasoning_effort or self.DEFAULT_REASONING,
                allow_blank=False,
                id="model-reasoning",
            )
            yield Static(
                (
                    "Choose how much reasoning to request for each message."
                    if self._reasoning_efforts
                    else "This model declares no configurable reasoning budgets."
                ),
                classes="form-help",
            )
            yield Label("Context size (tokens)", classes="form-label")
            yield Input(
                value=str(self._context_window_tokens),
                type="integer",
                id="model-context",
            )
            yield Static(
                "The usable conversation, instructions, evidence, and response all share this window.",
                classes="form-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Back", id="cancel-model")
                yield Button(
                    "Use model" if self._enabled else "Save disabled",
                    id="use-model", variant="primary",
                )
            yield Static("Enable to use this model        Esc  cancel", classes="dialog-hint")

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        self.query_one("#use-model", Button).label = (
            "Use model" if event.value else "Save disabled"
        )

    def on_mount(self) -> None:
        self.query_one("#model-reasoning", Select).focus()

    def on_input_submitted(self) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-model":
            self.dismiss(None)
        else:
            self._submit()

    def _submit(self) -> None:
        raw_context = self.query_one("#model-context", Input).value.strip()
        try:
            context_window = int(raw_context)
        except ValueError:
            self.notify("Context size must be a whole number of tokens.", severity="warning")
            return
        if not 4_096 <= context_window <= 2_000_000:
            self.notify("Context size must be between 4,096 and 2,000,000 tokens.", severity="warning")
            return
        selected = self.query_one("#model-reasoning", Select).value
        effort = None if selected == self.DEFAULT_REASONING else selected
        if effort is not None and effort not in self._reasoning_efforts:
            self.notify("Choose a reasoning budget supported by this model.", severity="warning")
            return
        self.dismiss(ModelConfigResult(
            reasoning_effort=effort,
            context_window_tokens=context_window,
            enabled=self.query_one("#model-enabled", Checkbox).value,
        ))


class ChoicePicker(ModalScreen[str | None]):
    """One list, one choice. Used for models and reasoning levels.

    Choices carry their own value, so the caller decodes what it handed in and
    never has to care how the list was rendered.
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(
        self,
        title: str,
        body: str,
        rows: list[tuple[str, str, str]],
        current: str = "",
        empty: str = "Nothing to choose from yet.",
    ) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._rows = rows
        self._current = current
        self._empty = empty

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._title, classes="dialog-title")
            yield Static(self._body, classes="dialog-body")
            yield OptionList(id="choices")
            yield Static(self._empty, id="choices-empty", classes="dialog-body")
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="cancel-choice")
                yield Button("Use", id="use-choice", variant="primary")
            yield Static("Up/Down  move        Enter  use        Esc  cancel", classes="dialog-hint")

    def on_mount(self) -> None:
        choices = self.query_one("#choices", OptionList)
        choices.add_options([
            Option(self._prompt(title, subtitle), id=value) for value, title, subtitle in self._rows
        ])
        choices.display = bool(self._rows)
        self.query_one("#choices-empty", Static).display = not self._rows
        values = [value for value, _, _ in self._rows]
        if self._current in values:
            choices.highlighted = values.index(self._current)
        elif self._rows:
            choices.highlighted = 0
        (choices if self._rows else self.query_one("#cancel-choice", Button)).focus()

    def _prompt(self, title: str, subtitle: str) -> Text:
        text = Text(title, style=Style(bold=True))
        if subtitle:
            text.append("\n" + subtitle, style=Style(color=palette(self, "text-muted", "#9babae")))
        return text

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-choice" or not self._rows:
            self.dismiss(None)
            return
        choices = self.query_one("#choices", OptionList)
        highlighted = choices.highlighted
        self.dismiss(None if highlighted is None else choices.get_option_at_index(highlighted).id)


class ThemePicker(ModalScreen[str | None]):
    """Live-preview theme picker. Highlight to preview, Enter to keep."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, themes: list[tuple[str, str]], current: str) -> None:
        super().__init__()
        self._themes = themes
        self._original = current

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Dress the cockpit.", classes="dialog-title")
            yield Static("Highlight to preview. Your choice is remembered.", classes="dialog-body")
            yield OptionList(id="theme-list")
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="cancel-theme")
                yield Button("Use theme", id="use-theme", variant="primary")
            yield Static("Up/Down  preview        Enter  use        Esc  cancel", classes="dialog-hint")

    def on_mount(self) -> None:
        options = [Option(self._prompt(name, description), id=name) for name, description in self._themes]
        option_list = self.query_one("#theme-list", OptionList)
        option_list.add_options(options)
        names = [name for name, _ in self._themes]
        if self._original in names:
            option_list.highlighted = names.index(self._original)
        option_list.focus()

    def _prompt(self, name: str, description: str) -> Text:
        theme = self.app.available_themes.get(name)
        text = Text()
        swatch = [theme.primary, theme.secondary or theme.primary, theme.accent or theme.primary] if theme else []
        for colour in swatch:
            text.append("██", style=Style(color=colour))
        text.append(("  " if swatch else "") + name.replace("-", " ").title(), style=Style(bold=True))
        text.append("\n" + description, style=Style(color=palette(self, "text-muted", "#9babae")))
        return text

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id:
            self.app.theme = event.option.id

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-theme":
            self.action_cancel()
            return
        option_list = self.query_one("#theme-list", OptionList)
        highlighted = option_list.highlighted
        self.dismiss(None if highlighted is None else option_list.get_option_at_index(highlighted).id)

    def action_cancel(self) -> None:
        self.app.theme = self._original
        self.dismiss(None)


SHORTCUTS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Conversation", (
        ("Enter", "Send (or steer an active run)"),
        ("Shift+Enter / Ctrl+J", "New line in the composer"),
        ("/", "Commands: model, reasoning, effort, web"),
        ("Ctrl+N", "New conversation, or new note"),
        ("Esc", "Stop the run that is streaming"),
    )),
    ("Set up this message", (
        ("Ctrl+M", "Choose model, reasoning budget and context"),
        ("Ctrl+G", "Set the reasoning level"),
        ("/provider", "Add or configure a model provider"),
        ("/effort quick", "Smallest sufficient evidence"),
        ("/web off", "Keep this message local"),
    )),
    ("Move around", (
        ("Ctrl+1 / 2 / 3", "Chat, papers, notes"),
        ("Ctrl+F", "Focus mode: hide or show the rails"),
        ("Ctrl+K", "Search the current list"),
        ("Ctrl+B", "Show or hide the library rail"),
        ("Ctrl+O", "Show or hide the observatory"),
        ("Ctrl+R", "Refresh from your local backend"),
    )),
    ("Notes", (
        ("Ctrl+S", "Save the open note"),
        ("Ctrl+E", "Edit or preview the open note"),
    )),
    ("The cockpit", (
        ("Ctrl+T", "Change theme"),
        ("Ctrl+P", "Command palette"),
        ("F1", "This help"),
        ("Ctrl+Q", "Quit"),
    )),
)


class ShortcutHelp(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "dismiss(None)", "Close"),
        Binding("f1", "dismiss(None)", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label("Everything is a keystroke away.", classes="dialog-title")
            with VerticalScroll(id="shortcut-scroll"):
                yield Static(self._shortcut_text(), id="shortcuts")
            with Horizontal(classes="dialog-actions"):
                yield Button("Close", id="close-help", variant="primary")
            yield Static("Esc  close", classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one("#close-help", Button).focus()

    def on_button_pressed(self) -> None:
        self.dismiss(None)

    def _shortcut_text(self) -> Text:
        accent = Style(color=palette(self, "primary", "#e6ad83"), bold=True)
        key = Style(color=palette(self, "accent", "#f0c9a6"))
        muted = Style(color=palette(self, "text-muted", "#9babae"))
        text = Text()
        for index, (heading, rows) in enumerate(SHORTCUTS):
            if index:
                text.append("\n")
            text.append(heading.upper() + "\n", style=accent)
            for shortcut, description in rows:
                text.append("  " + shortcut.ljust(22), style=key)
                text.append(description + "\n", style=muted)
        text.append("\nCOMMANDS\n", style=accent)
        for command in COMMANDS:
            text.append("  " + command.usage.ljust(22), style=key)
            text.append(command.summary + "\n", style=muted)
        return text
