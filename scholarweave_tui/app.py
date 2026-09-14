from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Literal

from rich.cells import cell_len
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    ContentSwitcher,
    Footer,
    Input,
    Markdown,
    OptionList,
    ProgressBar,
    RichLog,
    Rule,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from backend.agents.blueprint import ModelReferenceSpec
from backend.conversations.schemas import ConversationResponse, ResponseEffort
from backend.providers.reasoning import REASONING_EFFORTS, ReasoningEffort
from backend.providers.schemas import ProviderCreate, ProviderResponse, ProviderUpdate
from backend.research.schemas import DocumentResponse, DocumentSummaryResponse
from backend.runs.schemas import RunResponse
from backend.workspace.schemas import (
    WorkspaceFileContentResponse,
    WorkspaceFileResponse,
)
from scholarweave_tui import commands
from scholarweave_tui.client import ApiError, ScholarWeaveClient
from scholarweave_tui.preferences import Preferences
from scholarweave_tui.stream import LiveRun
from scholarweave_tui.themes import (
    DEFAULT_THEME,
    DESCRIPTIONS,
    THEMES,
    VARIABLE_DEFAULTS,
)
from scholarweave_tui.widgets import (
    ChatMessage,
    ChoicePicker,
    Composer,
    ConfirmDiscard,
    Masthead,
    NoteName,
    ProviderForm,
    ShortcutHelp,
    Spinner,
    ThemePicker,
    TranscriptNote,
    Welcome,
)

View = Literal["chat", "paper", "notes"]
ACTIVE_STATUSES = {"pending", "running"}
EFFORT_SUMMARY = {
    "auto": "follows your intent",
    "quick": "smallest sufficient evidence",
    "thorough": "focused delegation",
}
REASONING_LABELS = {
    "none": "Off", "minimal": "Minimal", "low": "Low", "medium": "Medium",
    "high": "High", "xhigh": "Extra high", "max": "Maximum",
}
COMPOSER_MIN_HEIGHT = 3
COMPOSER_MAX_HEIGHT = 8
COMPOSER_SHORT_MAX_HEIGHT = 5


class ScholarWeaveApp(App[None]):
    TITLE = "ScholarWeave"
    SUB_TITLE = "The research cockpit"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("ctrl+1", "view('chat')", "Chat", show=False, priority=True),
        Binding("ctrl+2", "view('paper')", "Papers", show=False, priority=True),
        Binding("ctrl+3", "view('notes')", "Notes", show=False, priority=True),
        Binding("ctrl+k", "search", "Find", priority=True),
        Binding("ctrl+n", "new", "New", priority=True),
        Binding("ctrl+enter", "send", "Send", show=False, priority=True),
        Binding("ctrl+s", "save_note", "Save", show=False, priority=True),
        Binding("ctrl+r", "reload", "Refresh", show=False, priority=True),
        Binding("ctrl+b", "sidebar", "Library", show=False, priority=True),
        Binding("ctrl+o", "observe", "Observe", show=False, priority=True),
        Binding("ctrl+e", "edit_note", "Edit", show=False),
        Binding("ctrl+f", "focus_mode", "Focus", priority=True),
        Binding("ctrl+m", "model", "Model", priority=True),
        Binding("ctrl+g", "reasoning", "Thinking", priority=True),
        Binding("ctrl+t", "themes", "Theme", show=False, priority=True),
        Binding("f1", "help", "Help", priority=True),
        Binding("escape", "stop", "Stop run", show=False),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, client: ScholarWeaveClient | None = None, preferences: Preferences | None = None) -> None:
        super().__init__()
        self.client = client or ScholarWeaveClient()
        self.preferences = preferences or Preferences()
        self.view: View = "chat"
        self.focus_mode = self.preferences.focus_mode
        self.conversations: list[ConversationResponse] = []
        self.papers: list[DocumentSummaryResponse] = []
        self.notes: list[WorkspaceFileResponse] = []
        self.providers: list[ProviderResponse] = []
        self.model_reference = ModelReferenceSpec()
        self.reasoning_effort: ReasoningEffort | None = None
        self.effort: ResponseEffort = "auto"
        self.web_enabled = self.preferences.web_enabled
        self.current_id: str | None = None
        self.current_title = "A new thread"
        self.paper: DocumentResponse | None = None
        self.note: WorkspaceFileContentResponse | None = None
        self.run_record: RunResponse | None = None
        self.live = LiveRun()
        self._entries: list[str] = []
        self._commands: list[tuple[commands.Command, str]] = []
        self._drafts: dict[str, str] = {}
        self._mutating = False
        self._loading = False
        self._chat_generation = 0
        self._painted_text: str | None = None
        self._painted_activity: tuple[str, ...] = ()
        self._sidebar_override: bool | None = None
        self._observe_override: bool | None = None
        self._note_baseline = ""
        self._steering_seen: set[str] = set()
        self._session_usage_before_live = (0, 0)
        self._live_usage_in_history = True

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Masthead()
            yield Button("1 Chat", id="nav-chat", classes="tab active")
            yield Button("2 Papers", id="nav-paper", classes="tab")
            yield Button("3 Notes", id="nav-notes", classes="tab")
            yield Spinner("Working", id="busy")
            yield Static("CONNECTING", id="connection", markup=False)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("YOUR THREADS", id="catalog-title", classes="eyebrow")
                yield Input(placeholder="Search threads...", id="search")
                yield Button("+  New conversation", id="new", variant="primary")
                yield OptionList(id="catalog")
                yield Static("No conversations yet.", id="catalog-empty", markup=False)
                yield Static("One researcher. One local workspace.", id="sidebar-hint", markup=False)
            with Vertical(id="stage"):
                yield Static("A new thread", id="section-title", markup=False)
                with ContentSwitcher(initial="chat-pane", id="pages"):
                    with Vertical(id="chat-pane"):
                        with VerticalScroll(id="transcript"):
                            yield Welcome()
                        with Center(id="runline-shell"), Horizontal(id="runline"):
                            yield Spinner("Following the thread", id="run-spinner")
                            yield Static("00:00", id="run-elapsed", markup=False)
                            yield Button("Stop", id="stop", variant="error")
                        with Center(id="composer-shell"), Vertical(id="composer-box"):
                            yield Composer(id="composer")
                            yield OptionList(id="slash")
                            with Horizontal(id="composer-controls"):
                                yield Static("", id="statusline", markup=False)
                                yield Button("Send", id="send", variant="primary")
                            yield Static(
                                "Enter  send        Shift+Enter  new line        /  commands",
                                id="composer-hint", markup=False,
                            )
                    with Vertical(id="paper-pane"):
                        with VerticalScroll(id="paper-scroll"):
                            yield Markdown(
                                "# Your evidence, close at hand\n\n"
                                "Choose a paper from your library to read its extracted text and citations.\n\n"
                                "Import PDFs and manage collections in the web app. "
                                "Press **Ctrl+R** to pick up changes here.",
                                id="paper-content", open_links=False,
                            )
                        with Horizontal(id="paper-actions"):
                            yield Static("Reading stays local. Nothing leaves this machine.", id="paper-hint", markup=False)
                            yield Button("Discuss this paper", id="discuss", disabled=True, variant="primary")
                    with Vertical(id="notes-pane"):
                        yield Static("Choose a note, or make room for a new idea.", id="note-path", markup=False)
                        with ContentSwitcher(initial="note-scroll", id="note-content"):
                            with VerticalScroll(id="note-scroll"):
                                yield Markdown(
                                    "# Keep the useful parts\n\n"
                                    "Your knowledge notes and paper notes live here. "
                                    "Search uses your existing local full-text index.\n\n"
                                    "**Ctrl+N** creates a note. **Ctrl+S** saves your edits.",
                                    id="note-preview", open_links=False,
                                )
                            yield TextArea(id="note-editor", show_line_numbers=True)
                        with Horizontal(id="note-actions"):
                            yield Static("", id="note-state", markup=False)
                            yield Button("Edit", id="edit-note", disabled=True)
                            yield Button("Revert", id="revert-note", disabled=True)
                            yield Button("Save", id="save-note", variant="primary", disabled=True)
            with Vertical(id="observatory"):
                yield Static("OBSERVATORY", classes="eyebrow")
                yield Static("READY WHEN YOU ARE", id="run-status", markup=False)
                yield ProgressBar(total=None, show_eta=False, show_percentage=False, id="run-progress")
                yield Static("No active run", id="run-agent", markup=False)
                yield Static("Uses your configured chat model", id="run-model", markup=False)
                yield Rule(line_style="dashed")
                yield Static("No token usage yet", id="usage", markup=False)
                yield Rule(line_style="dashed")
                yield Markdown(
                    "### Work plan\nA plan appears here when your research needs one.",
                    id="plan", open_links=False,
                )
                yield Rule(line_style="dashed")
                yield RichLog(id="activity", wrap=True, highlight=False, markup=False, max_lines=120)
                yield Static("Live activity, not hidden magic.", id="stream-status", markup=False)
        yield Footer()

    def get_theme_variable_defaults(self) -> dict[str, str]:
        return dict(VARIABLE_DEFAULTS)

    def on_mount(self) -> None:
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = self.preferences.theme if self.preferences.theme in self.available_themes else DEFAULT_THEME
        self.theme_changed_signal.subscribe(self, self._theme_changed)
        screen = self.screen_stack[0]
        screen.query_one("#composer-box", Vertical).border_title = "ASK ANYTHING"
        screen.query_one("#busy", Spinner).display = False
        screen.query_one("#run-progress", ProgressBar).display = False
        screen.query_one("#runline", Horizontal).display = False
        screen.query_one("#slash", OptionList).display = False
        self._tooltips()
        self._layout()
        self._populate_catalog()
        self._controls()
        self._resize_composer()
        self.set_interval(0.15, self._paint_live)
        self.set_interval(1, self._paint_clock)
        self.refresh_catalog()
        screen.query_one("#composer", Composer).focus()

    def _tooltips(self) -> None:
        screen = self.screen_stack[0]
        for identifier, tooltip in {
            "#nav-chat": "Conversations  ·  Ctrl+1",
            "#nav-paper": "Paper library  ·  Ctrl+2",
            "#nav-notes": "Knowledge notes  ·  Ctrl+3",
            "#new": "Start something new  ·  Ctrl+N",
            "#send": "Send your message  ·  Enter",
            "#stop": "Stop this run  ·  Esc",
            "#statusline": "What your next message will use  ·  type / to change it",
            "#save-note": "Save to your workspace  ·  Ctrl+S",
            "#edit-note": "Switch between preview and editor",
            "#revert-note": "Throw away unsaved changes",
            "#discuss": "Open a conversation about this paper",
        }.items():
            screen.query_one(identifier).tooltip = tooltip

    def _theme_changed(self, _theme: object) -> None:
        if self.is_mounted:
            self._populate_catalog()

    def _palette(self, name: str, fallback: str) -> str:
        value = (getattr(self, "theme_variables", None) or {}).get(name)
        return value if isinstance(value, str) and value.startswith("#") else fallback

    def action_themes(self) -> None:
        self.change_theme()

    @work(group="theme", exclusive=True)
    async def change_theme(self) -> None:
        options = [(theme.name, DESCRIPTIONS.get(theme.name, "")) for theme in THEMES]
        chosen = await self.push_screen_wait(ThemePicker(options, self.theme))
        if chosen:
            self.theme = chosen
            self.preferences.save_theme(chosen)
            self.notify(f"Theme set to {chosen.replace('-', ' ')}.", title="ScholarWeave")

    def action_help(self) -> None:
        self.push_screen(ShortcutHelp())

    def action_stop(self) -> None:
        if self.run_record is not None and self.run_record.status in ACTIVE_STATUSES:
            self.stop_run()

    async def on_unmount(self) -> None:
        await self.client.close()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {
            "view", "search", "new", "send", "save_note", "reload", "sidebar", "observe", "themes", "help", "stop",
            "edit_note", "focus_mode", "model", "provider", "reasoning", "quit",
        }:
            return not self.screen.is_modal
        return True

    def on_resize(self, event: events.Resize) -> None:
        if self.is_mounted:
            self._layout()
            self._resize_composer()

    def _layout(self) -> None:
        width, height = self.size
        self.set_class(width < 125, "compact")
        self.set_class(width < 90, "narrow")
        self.set_class(height < 35, "short")
        self.set_class(self.focus_mode, "focused")
        self.screen_stack[0].query_one(Masthead).compact = width < 90 or self.focus_mode
        roomy = not self.focus_mode
        sidebar = self._sidebar_override if self._sidebar_override is not None else roomy and width >= 90
        observe = self._observe_override if self._observe_override is not None else False
        self.screen_stack[0].query_one("#sidebar").display = sidebar
        self.screen_stack[0].query_one("#observatory").display = observe
        self.screen_stack[0].query_one("#stage").display = not (width < 70 and (sidebar or observe))

    def action_focus_mode(self) -> None:
        self.focus_mode = not self.focus_mode
        self._sidebar_override = None
        self._observe_override = None
        self.preferences.save_focus_mode(self.focus_mode)
        self._layout()
        self.notify(
            "Focus mode on. Ctrl+F brings the rails back."
            if self.focus_mode else "Rails restored. Ctrl+F returns to focus mode."
        )

    def action_sidebar(self) -> None:
        self._sidebar_override = not self.screen_stack[0].query_one("#sidebar").display
        if self._sidebar_override and self.size.width < 125:
            self._observe_override = False
        self._layout()

    def action_observe(self) -> None:
        self._observe_override = not self.screen_stack[0].query_one("#observatory").display
        if self._observe_override and self.size.width < 125:
            self._sidebar_override = False
        self._layout()

    def action_search(self) -> None:
        self._sidebar_override = True
        if self.size.width < 125:
            self._observe_override = False
        self._layout()
        self.screen_stack[0].query_one("#search", Input).focus()

    def _error(self, error: ApiError) -> None:
        self.notify(str(error), title="ScholarWeave", severity="error", timeout=10)
        self.screen_stack[0].query_one("#connection", Static).update("REQUEST FAILED  ·  Ctrl+R")
        self.screen_stack[0].query_one("#connection").add_class("offline")

    def _connected(self) -> None:
        self.screen_stack[0].query_one("#connection", Static).update("● LOCAL  ·  CONNECTED")
        self.screen_stack[0].query_one("#connection").remove_class("offline")

    def _busy(self, label: str | None) -> None:
        spinner = self.screen_stack[0].query_one("#busy", Spinner)
        spinner.display = label is not None
        if label is not None:
            spinner.label = label

    @work(group="catalog", exclusive=True)
    async def refresh_catalog(self, *, reopen: bool = False) -> None:
        catalog = self.screen_stack[0].query_one("#catalog", OptionList)
        catalog.loading = True
        try:
            conversations, papers, notes = await asyncio.gather(
                self.client.conversations(), self.client.papers(), self.client.notes(),
            )
            self.conversations, self.papers, self.notes = conversations, papers, notes
            self._connected()
            self._populate_catalog()
            await self._refresh_models()
            if self.view == "notes" and self.screen_stack[0].query_one("#search", Input).value.strip():
                self.search_notes()
            if reopen:
                key = (
                    self.current_id if self.view == "chat" else
                    self.paper.id if self.view == "paper" and self.paper else
                    self.note.path if self.view == "notes" and self.note and not self.note_dirty else None
                )
                if key:
                    self.open_entry(key)
        except ApiError as exc:
            self._error(exc)
        finally:
            catalog.loading = False

    async def _refresh_models(self) -> None:
        """Learn the configured models. A provider outage must not block research."""

        try:
            self.providers, settings = await asyncio.gather(
                self.client.providers(), self.client.settings(),
            )
        except ApiError as exc:
            self.notify(f"Model list unavailable: {exc}", severity="warning")
            return
        preferred = settings.last_chat_model_reference
        if not (preferred.provider_profile_id and preferred.model):
            preferred = settings.default_model_references.get("chat") or ModelReferenceSpec()
        self.model_reference = preferred
        self.reasoning_effort = self._remembered_reasoning()
        self._controls()

    def action_reload(self) -> None:
        if not self._mutating and not self._loading:
            self.refresh_catalog(reopen=True)

    def _populate_catalog(self, note_results: list[WorkspaceFileResponse] | None = None) -> None:
        query = self.screen_stack[0].query_one("#search", Input).value.casefold().strip()
        rows: list[tuple[str, str, str]] = []
        if self.view == "chat":
            rows = [
                (item.id, item.title, item.last_message_preview or "Start a conversation")
                for item in self.conversations
                if not query or query in f"{item.title} {item.last_message_preview}".casefold()
            ]
        elif self.view == "paper":
            rows = [
                (item.id, item.title, f"{item.page_count or '?'} pages / {item.status}")
                for item in self.papers
                if not query or query in f"{item.title} {item.source_filename}".casefold()
            ]
        else:
            rows = [
                (item.path, item.note_name or item.paper_name or item.name, " / ".join(item.tags) or item.kind.replace("_", " "))
                for item in (note_results if note_results is not None else self.notes)
                if note_results is not None or not query
                or query in f"{item.name} {item.note_name} {item.paper_name} {' '.join(item.tags)}".casefold()
            ]
        options = []
        self._entries = []
        title_style = self._palette("foreground", "#eee8dc")
        subtitle_style = self._palette("text-muted", "#9babae")
        for key, title, subtitle in rows:
            prompt = Text(title, style=f"bold {title_style}")
            prompt.append("\n" + subtitle.replace("\n", " ")[:95], style=subtitle_style)
            options.append(Option(prompt, id=str(len(self._entries))))
            self._entries.append(key)
        catalog = self.screen_stack[0].query_one("#catalog", OptionList)
        catalog.clear_options().add_options(options)
        catalog.display = bool(rows)
        empty = self.screen_stack[0].query_one("#catalog-empty", Static)
        empty.display = not rows
        empty.update("No matches. Try another search." if query else {
            "chat": "No conversations yet.\nStart with a question.",
            "paper": "No papers yet.\nImport PDFs in the web app.",
            "notes": "No notes yet.\nCtrl+N starts a new one.",
        }[self.view])
        self.screen_stack[0].query_one("#catalog-title", Static).update(
            f"{ {'chat': 'YOUR THREADS', 'paper': 'PAPER LIBRARY', 'notes': 'KNOWLEDGE NOTES'}[self.view]}  ·  {len(rows):02}"
        )

    @on(Input.Changed, "#search")
    def search_changed(self) -> None:
        self._populate_catalog()
        if self.view == "notes" and self.screen_stack[0].query_one("#search", Input).value.strip():
            self.search_notes()
        else:
            self.workers.cancel_group(self, "search")

    @work(group="search", exclusive=True)
    async def search_notes(self) -> None:
        query = self.screen_stack[0].query_one("#search", Input).value.strip()
        await asyncio.sleep(0.25)
        try:
            matches = await self.client.search_notes(query)
            if self.view == "notes" and self.screen_stack[0].query_one("#search", Input).value.strip() == query:
                self._populate_catalog(matches)
        except ApiError as exc:
            self._error(exc)

    @on(OptionList.OptionSelected, "#catalog")
    def entry_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.open_entry(self._entries[int(event.option.id)])

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "nav-chat":
                self.action_view("chat")
            case "nav-paper":
                self.action_view("paper")
            case "nav-notes":
                self.action_view("notes")
            case "new":
                self.action_new()
            case "send":
                self.action_send()
            case "stop":
                self.stop_run()
            case "edit-note":
                self.edit_note()
            case "save-note":
                self.action_save_note()
            case "revert-note":
                self.revert_note()
            case "discuss":
                self.discuss_paper()

    @on(Composer.Submitted)
    def composer_submitted(self) -> None:
        self.action_send()

    @on(TextArea.Changed, "#composer")
    def composer_changed(self) -> None:
        self._resize_composer()
        self._sync_menu()

    def _resize_composer(self) -> None:
        if not self.is_mounted:
            return
        composer = self.screen_stack[0].query_one("#composer", Composer)
        wrap_width = max(20, composer.content_size.width - 1)
        visible_lines = sum(
            max(1, (cell_len(line) + wrap_width - 1) // wrap_width)
            for line in composer.text.split("\n")
        )
        maximum = COMPOSER_SHORT_MAX_HEIGHT if self.size.height < 35 else COMPOSER_MAX_HEIGHT
        composer.styles.height = min(maximum, max(COMPOSER_MIN_HEIGHT, visible_lines))

    def _sync_menu(self) -> None:
        """Show the command menu while the composer holds a slash command."""

        composer = self.screen_stack[0].query_one("#composer", Composer)
        menu = self.screen_stack[0].query_one("#slash", OptionList)
        text = composer.text
        self._commands = commands.matches(text) if commands.is_command(text) else []
        menu.clear_options()
        if self._commands:
            menu.add_options([
                Option(self._command_prompt(command, argument), id=str(index))
                for index, (command, argument) in enumerate(self._commands)
            ])
            menu.highlighted = 0
        menu.display = bool(self._commands)
        composer.menu_open = bool(self._commands)

    def _command_prompt(self, command: commands.Command, argument: str) -> Text:
        text = Text(f"/{command.name}", style=f"bold {self._palette('primary', '#e6ad83')}")
        if argument:
            text.append(f" {argument}", style=self._palette("accent", "#f0c9a6"))
        elif command.argument_hint:
            text.append(f" {command.argument_hint}", style=self._palette("text-muted", "#9babae"))
        text.append("   " + command.summary, style=self._palette("text-muted", "#9babae"))
        return text

    @on(Composer.MenuKey)
    def menu_key(self, event: Composer.MenuKey) -> None:
        menu = self.screen_stack[0].query_one("#slash", OptionList)
        if not self._commands:
            return
        if event.key == "escape":
            self._close_menu()
        elif event.key == "up":
            menu.action_cursor_up()
        elif event.key == "down":
            menu.action_cursor_down()
        else:
            self._choose_command(menu.highlighted or 0, complete=event.key == "tab")

    @on(OptionList.OptionSelected, "#slash")
    def menu_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self._choose_command(int(event.option.id))

    def _choose_command(self, index: int, *, complete: bool = False) -> None:
        if not 0 <= index < len(self._commands):
            return
        command, argument = self._commands[index]
        composer = self.screen_stack[0].query_one("#composer", Composer)
        if complete or (command.arguments and not argument):
            composer.load_text(f"/{command.name} " if command.arguments else f"/{command.name}")
            composer.move_cursor(composer.document.end)
            self._sync_menu()
            return
        self._close_menu()
        self.run_command(command.name, argument)

    def _close_menu(self) -> None:
        self._commands = []
        menu = self.screen_stack[0].query_one("#slash", OptionList)
        menu.clear_options()
        menu.display = False
        self.screen_stack[0].query_one("#composer", Composer).menu_open = False

    def run_command(self, name: str, argument: str = "") -> None:
        """Apply a slash command. Unknown names stay in the composer as text."""

        composer = self.screen_stack[0].query_one("#composer", Composer)
        clear = True
        match name:
            case "model":
                self.choose_model()
            case "provider":
                self.configure_provider()
            case "reasoning":
                self.choose_reasoning(argument)
            case "effort":
                clear = self._set_effort(argument)
            case "web":
                clear = self._set_web(argument)
            case "focus":
                self.action_focus_mode()
            case "new":
                self.action_new()
            case "papers":
                self.action_view("paper")
            case "notes":
                self.action_view("notes")
            case "chat":
                self.action_view("chat")
            case "stop":
                self.action_stop()
            case "refresh":
                self.action_reload()
            case "theme":
                self.action_themes()
            case "status":
                self.notify(self._status_detail(), title="This message", timeout=10)
            case "help":
                self.action_help()
            case "quit":
                self.action_quit()
            case _:
                self.notify(f"No command called /{name}. Type / to see them all.", severity="warning")
                clear = False
        if clear:
            composer.clear()
        self._controls()

    def _set_effort(self, argument: str) -> bool:
        value = argument.casefold()
        if value not in {"auto", "quick", "thorough"}:
            self.notify("Effort is auto, quick or thorough.", severity="warning")
            return False
        self.effort = value  # type: ignore[assignment]
        self.notify(f"Effort: {value} — {EFFORT_SUMMARY[value]}.")
        return True

    def _set_web(self, argument: str) -> bool:
        value = argument.casefold()
        if value not in {"on", "off"}:
            self.notify("Web is on or off.", severity="warning")
            return False
        self.web_enabled = value == "on"
        self.preferences.save_web_enabled(self.web_enabled)
        self.notify("Web sources allowed." if self.web_enabled else "Staying local for now.")
        return True

    def _chat_models(self) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        for provider in self.providers:
            if provider.state == "archived":
                continue
            for model in provider.models:
                if not model.enabled:
                    continue
                hints = sorted(model.capabilities) or ["chat"]
                levels = ", ".join(model.reasoning_efforts or []) or "no reasoning levels"
                rows.append((
                    f"{provider.id}\t{model.name}",
                    f"{provider.name} / {model.name}",
                    f"{' · '.join(hints)}  ·  {levels}",
                ))
        return rows

    def _model_label(self, reference: ModelReferenceSpec | None = None) -> str:
        reference = reference or self.model_reference
        if not reference.provider_profile_id or not reference.model:
            return "workspace default model"
        return reference.model

    def _model_efforts(self) -> list[ReasoningEffort]:
        for provider in self.providers:
            if provider.id != self.model_reference.provider_profile_id:
                continue
            for model in provider.models:
                if model.name == self.model_reference.model:
                    return list(model.reasoning_efforts or [])
        return []

    def action_model(self) -> None:
        self.choose_model()

    def action_provider(self) -> None:
        self.configure_provider()

    @work(group="provider", exclusive=True)
    async def configure_provider(self) -> None:
        rows = [
            ("__add__", "Add provider", "Connect Ollama, OpenAI, Azure, or another compatible server"),
            *[
                (
                    provider.id,
                    provider.name,
                    f"{provider.kind.replace('_', ' ')}  ·  {provider.base_url}  ·  "
                    f"{len(provider.models)} models",
                )
                for provider in self.providers
                if provider.state == "active"
            ],
        ]
        chosen = await self.push_screen_wait(ChoicePicker(
            "Model providers.",
            "Add a connection or update an existing profile.",
            rows,
        ))
        if chosen is None:
            return
        current = next((provider for provider in self.providers if provider.id == chosen), None)
        draft = await self.push_screen_wait(ProviderForm(current))
        if draft is None:
            return
        self._mutating = True
        self._busy("Saving provider")
        try:
            if current is None:
                saved = await self.client.create_provider(ProviderCreate(
                    name=draft.name,
                    kind=draft.kind,
                    base_url=draft.base_url,
                    api_key=draft.api_key,
                ))
            else:
                update_values = {
                    "name": draft.name,
                    "kind": draft.kind,
                    "base_url": draft.base_url,
                }
                if draft.api_key is not None:
                    update_values["api_key"] = draft.api_key
                saved = await self.client.update_provider(
                    current.id, ProviderUpdate(**update_values),
                )
            discovered = await self.client.discover_provider_models(saved.id)
            await self._refresh_models()
            if discovered.discovery_error:
                self.notify(
                    f"{saved.name} was saved, but model discovery failed: "
                    f"{discovered.discovery_error}",
                    severity="warning",
                    timeout=12,
                )
            else:
                self.notify(
                    f"{saved.name} saved · {len(discovered.models)} models discovered."
                )
        except ApiError as exc:
            self._error(exc)
        finally:
            self._mutating = False
            self._busy(None)
            self._controls()

    @work(group="picker", exclusive=True)
    async def choose_model(self) -> None:
        rows = [("", "Workspace default", "Whatever Settings has configured for chat"), *self._chat_models()]
        current = (
            f"{self.model_reference.provider_profile_id}\t{self.model_reference.model}"
            if self.model_reference.model else ""
        )
        chosen = await self.push_screen_wait(ChoicePicker(
            "Choose your thinking partner.",
            "New conversations use this model. Your choice is remembered.",
            rows, current, empty="No enabled models. Use /provider to add one.",
        ))
        if chosen is None:
            return
        provider_id, _, model = chosen.partition("\t")
        await self._apply_model(ModelReferenceSpec(
            provider_profile_id=provider_id or None, model=model or None,
        ))

    async def _apply_model(self, reference: ModelReferenceSpec) -> None:
        self.model_reference = reference
        self.reasoning_effort = self._remembered_reasoning()
        try:
            await self.client.save_chat_model(reference)
        except ApiError as exc:
            self._error(exc)
        self._controls()
        self.notify(f"Model: {self._model_label()}.")
        if self.current_id is not None:
            await self._new_chat()
            self.notify("Started a new thread for this model.")

    def _remembered_reasoning(self) -> ReasoningEffort | None:
        remembered = self.preferences.reasoning(
            self.model_reference.provider_profile_id, self.model_reference.model,
        )
        return remembered if remembered in self._model_efforts() else None  # type: ignore[return-value]

    def action_reasoning(self) -> None:
        self.choose_reasoning()

    @work(group="picker", exclusive=True)
    async def choose_reasoning(self, argument: str = "") -> None:
        efforts = self._model_efforts()
        if argument:
            value = argument.casefold()
            if value in {"default", "off"} and value not in efforts:
                self._apply_reasoning(None)
                return
            if value not in efforts:
                self.notify(
                    f"{self._model_label()} accepts: {', '.join(efforts) or 'no reasoning levels'}.",
                    severity="warning",
                )
                return
            self._apply_reasoning(value)  # type: ignore[arg-type]
            return
        rows: list[tuple[str, str, str]] = []
        if efforts:
            rows = [("", "Provider default", "Let the model decide how long to think")]
            rows += [
                (effort, REASONING_LABELS.get(effort, effort), f"Send reasoning_effort={effort}")
                for effort in REASONING_EFFORTS if effort in efforts
            ]
        chosen = await self.push_screen_wait(ChoicePicker(
            "How hard should it think?",
            f"Levels declared for {self._model_label()}.",
            rows, self.reasoning_effort or "",
            empty=f"{self._model_label()} declares no reasoning levels.",
        ))
        if chosen is not None:
            self._apply_reasoning(chosen or None)  # type: ignore[arg-type]

    def _apply_reasoning(self, effort: ReasoningEffort | None) -> None:
        self.reasoning_effort = effort
        self.preferences.save_reasoning(
            self.model_reference.provider_profile_id, self.model_reference.model, effort,
        )
        self._controls()
        self.notify(
            f"Thinking: {REASONING_LABELS.get(effort, effort)}." if effort else "Thinking: provider default."
        )

    def _status_detail(self) -> str:
        reasoning = REASONING_LABELS.get(self.reasoning_effort or "", "provider default")
        return (
            f"Model: {self._model_label()}\n"
            f"Thinking: {reasoning}\n"
            f"Effort: {self.effort} — {EFFORT_SUMMARY[self.effort]}\n"
            f"Web sources: {'allowed' if self.web_enabled else 'off'}"
        )

    def action_view(self, view: View) -> None:
        if self._mutating or self._loading:
            self.notify("Finish the current request before switching.", severity="warning")
            return
        self.view = view
        if self.size.width < 70:
            self._sidebar_override = False
            self._observe_override = False
            self._layout()
        self.screen_stack[0].query_one("#pages", ContentSwitcher).current = f"{view}-pane"
        for name in ("chat", "paper", "notes"):
            self.screen_stack[0].query_one(f"#nav-{name}").set_class(name == view, "active")
        self.screen_stack[0].query_one("#search", Input).value = ""
        self.screen_stack[0].query_one("#search", Input).placeholder = {
            "chat": "Search threads...", "paper": "Search papers...", "notes": "Search notes...",
        }[view]
        new = self.screen_stack[0].query_one("#new", Button)
        new.label = "+  New note" if view == "notes" else "+  New conversation"
        new.display = view != "paper"
        self._update_title()
        self._populate_catalog()
        if view == "chat":
            self.screen_stack[0].query_one("#composer", Composer).focus()

    def _update_title(self) -> None:
        title = self.current_title if self.view == "chat" else (
            self.paper.title if self.view == "paper" and self.paper else
            (self.note.note_name or self.note.name) if self.view == "notes" and self.note else
            "The paper library" if self.view == "paper" else "Your knowledge, connected"
        )
        self.screen_stack[0].query_one("#section-title", Static).update(title)

    @property
    def note_dirty(self) -> bool:
        return self.note is not None and self.screen_stack[0].query_one("#note-editor", TextArea).text != self._note_baseline

    async def _discard_note(self) -> bool:
        return not self.note_dirty or await self.push_screen_wait(ConfirmDiscard())

    @work(group="selection", exclusive=True)
    async def open_entry(self, key: str) -> None:
        if self._mutating:
            return
        self._loading = True
        self._controls()
        pane = self.screen_stack[0].query_one(
            {"chat": "#transcript", "paper": "#paper-scroll", "notes": "#note-content"}[self.view]
        )
        pane.loading = True
        try:
            if self.view == "chat":
                await self._open_conversation(key)
            elif self.view == "paper":
                self.paper = await self.client.paper(key)
                parts = [f"# {self.paper.title}", f"**{self.paper.page_count or '?'} pages**  ·  {self.paper.status}"]
                if self.paper.chunks:
                    for chunk in self.paper.chunks:
                        parts.extend([f"### {chunk.citation}", chunk.text])
                else:
                    parts.append("No extracted text yet. Run extraction in the web app, then refresh this paper.")
                await self.screen_stack[0].query_one("#paper-content", Markdown).update("\n\n".join(parts))
                self.screen_stack[0].query_one("#paper-scroll", VerticalScroll).scroll_home(animate=False)
                self.screen_stack[0].query_one("#discuss", Button).disabled = False
            elif await self._discard_note():
                note = await self.client.read_note(key)
                self._load_note(note)
            self._connected()
            self._update_title()
            if self.size.width < 90:
                self._sidebar_override = False
                self._layout()
        except ApiError as exc:
            self._error(exc)
        finally:
            pane.loading = False
            self._loading = False
            self._controls()

    async def _open_conversation(self, key: str) -> None:
        conversation, runs = await asyncio.gather(self.client.conversation(key), self.client.runs(key))
        self._drafts[self.current_id or ""] = self.screen_stack[0].query_one("#composer", TextArea).text
        self._chat_generation += 1
        self.workers.cancel_group(self, "stream")
        self.current_id, self.current_title = key, conversation.title
        self.screen_stack[0].query_one("#composer", TextArea).load_text(self._drafts.get(key, ""))
        self.effort = "auto"
        transcript = self.screen_stack[0].query_one("#transcript", VerticalScroll)
        await transcript.remove_children()
        runs = sorted(runs, key=lambda run: run.created_at)
        active = next((run for run in reversed(runs) if run.status in ACTIVE_STATUSES), None)
        session_input = 0
        session_output = 0
        self._steering_seen.clear()
        for run in runs:
            if isinstance(run.input, str) and run.input:
                await transcript.mount(ChatMessage("user", run.input))
            for event in run.events:
                if event.event_type == "steering.queued":
                    await self._show_steering(event.payload.get("message_id"), event.payload.get("content"))
            state = LiveRun.restore(run)
            turn_input, turn_output = self._turn_tokens(state)
            if run is active:
                self._session_usage_before_live = (session_input, session_output)
                session_totals = (session_input + turn_input, session_output + turn_output)
            else:
                session_input += turn_input
                session_output += turn_output
                session_totals = (session_input, session_output)
            progress = self._progress_text(state)
            usage = self._usage_text(state, session_totals)
            if progress:
                await transcript.mount(TranscriptNote(
                    progress,
                    classes="turn-metadata live-progress" if run is active else "turn-metadata",
                ))
            if run is active:
                await transcript.mount(ChatMessage(
                    "assistant",
                    state.assistant,
                    id="live-response",
                    thinking=not state.assistant,
                    usage=usage,
                ))
            elif state.assistant:
                await transcript.mount(ChatMessage("assistant", state.assistant, usage=usage))
            if run.error:
                await transcript.mount(TranscriptNote(
                    f"Run {run.status}: {run.error}", classes="run-note failed",
                ))
            elif run.status == "cancelled":
                await transcript.mount(TranscriptNote(
                    "Run stopped. Partial output may be incomplete.", classes="run-note",
                ))
        if not runs:
            for item in conversation.items:
                if item.role in {"user", "assistant"} and item.text:
                    await transcript.mount(ChatMessage(item.role, item.text))
        if not transcript.children:
            await transcript.mount(Welcome())
        self.run_record = active or (runs[-1] if runs else None)
        self.live = LiveRun.restore(self.run_record) if self.run_record else LiveRun()
        if active is None:
            self._session_usage_before_live = (session_input, session_output)
        self._live_usage_in_history = active is None
        self._reset_observatory()
        self.screen_stack[0].query_one("#run-model", Static).update(conversation.model_reference.model or "Configured chat model")
        transcript.scroll_end(animate=False)
        if active:
            self.watch_run(active.id, self._chat_generation)

    def _controls(self) -> None:
        screen = self.screen_stack[0]
        active = self.run_record is not None and self.run_record.status in ACTIVE_STATUSES
        busy = self._mutating or self._loading
        screen.query_one("#send", Button).disabled = busy
        screen.query_one("#send", Button).label = "Steer" if active else "Send"
        stop = screen.query_one("#stop", Button)
        stop.display = active
        stop.disabled = busy
        screen.query_one("#runline", Horizontal).display = active
        screen.query_one("#composer", Composer).read_only = busy
        screen.query_one("#statusline", Static).update(self._status_line())
        screen.query_one("#composer-hint", Static).update(
            "Enter  steer this run        Esc  stop        /  commands"
            if active else "Enter  send        Shift+Enter  new line        /  commands"
        )
        screen.query_one("#save-note", Button).disabled = not self.note_dirty or self._mutating
        screen.query_one("#revert-note", Button).disabled = not self.note_dirty or self._mutating
        screen.query_one("#note-state", Static).set_class(self.note_dirty, "dirty")
        self._busy(
            "Saving your work" if self._mutating and self.view == "notes" else
            "Sending" if self._mutating else "Loading" if self._loading else None
        )

    def _status_line(self) -> str:
        parts = [self._model_label()]
        if self.reasoning_effort:
            parts.append(f"thinking {self.reasoning_effort}")
        if self.effort != "auto":
            parts.append(self.effort)
        if not self.web_enabled:
            parts.append("local only")
        return "  ·  ".join(parts)

    async def _new_chat(self, draft: str | None = None) -> None:
        self._drafts[self.current_id or ""] = self.screen_stack[0].query_one("#composer", TextArea).text
        self._chat_generation += 1
        self.workers.cancel_group(self, "stream")
        self.current_id, self.current_title, self.run_record = None, "A new thread", None
        self.live = LiveRun()
        self._session_usage_before_live = (0, 0)
        self._live_usage_in_history = True
        self.screen_stack[0].query_one("#composer", TextArea).load_text(
            self._drafts.get("", "") if draft is None else draft
        )
        self.effort = "auto"
        transcript = self.screen_stack[0].query_one("#transcript", VerticalScroll)
        await transcript.remove_children()
        await transcript.mount(Welcome())
        self._reset_observatory()
        self.screen_stack[0].query_one("#run-model", Static).update("Uses your configured chat model")
        self._controls()
        self.action_view("chat")

    async def _show_steering(self, message_id: object, content: object) -> None:
        if not isinstance(message_id, str) or not isinstance(content, str) or message_id in self._steering_seen:
            return
        self._steering_seen.add(message_id)
        transcript = self.screen_stack[0].query_one("#transcript", VerticalScroll)
        bubble = transcript.query("#live-response")
        has_live = bool(bubble)
        await bubble.remove()
        if has_live:
            await transcript.query(".live-progress").remove()
        await transcript.mount(ChatMessage("user", f"**Steering queued**\n\n{content}"))
        if has_live:
            await transcript.mount(
                TranscriptNote(
                    self._progress_text(self.live),
                    classes="turn-metadata live-progress",
                ),
                ChatMessage(
                    "assistant",
                    self.live.assistant,
                    id="live-response",
                    thinking=not self.live.assistant,
                    usage=self._usage_text(self.live, self._session_totals()),
                ),
            )
            self._painted_text = None

    @work(group="mutation")
    async def action_send(self) -> None:
        if self.view != "chat" or self._mutating or self._loading:
            return
        composer = self.screen_stack[0].query_one("#composer", TextArea)
        content = composer.text.strip()
        if not content:
            self.notify("Start with a question or an idea.", severity="information")
            composer.focus()
            return
        self._mutating = True
        self._controls()
        try:
            if self.run_record and self.run_record.status in ACTIVE_STATUSES:
                steering = await self.client.steer_run(self.run_record.id, content)
                await self._show_steering(steering.id, steering.content)
                self.notify("Steering queued for the active run.")
            else:
                if self.current_id is None:
                    conversation = await self.client.create_conversation(self.model_reference)
                    self.current_id = conversation.id
                    self.current_title = conversation.title
                response = await self.client.send_message(
                    self.current_id, content, effort=self.effort,
                    web_enabled=self.web_enabled, reasoning_effort=self.reasoning_effort,
                )
                self.current_title = response.conversation.title
                previous_answer = self.live.assistant
                previous_progress = self._progress_text(self.live)
                if self.run_record is not None and not self._live_usage_in_history:
                    previous_input, previous_output = self._turn_tokens(self.live)
                    before_input, before_output = self._session_usage_before_live
                    self._session_usage_before_live = (
                        before_input + previous_input,
                        before_output + previous_output,
                    )
                    self._live_usage_in_history = True
                previous_usage = self._usage_text(self.live, self._session_usage_before_live)
                self.run_record = response.run
                self.live = LiveRun.restore(response.run)
                self._live_usage_in_history = False
                self._steering_seen.clear()
                self._chat_generation += 1
                self.workers.cancel_group(self, "stream")
                transcript = self.screen_stack[0].query_one("#transcript", VerticalScroll)
                await transcript.query(Welcome).remove()
                for metadata in transcript.query(".live-progress").results(TranscriptNote):
                    metadata.update(previous_progress)
                    metadata.remove_class("live-progress")
                for previous in transcript.query("#live-response"):
                    # The old bubble remains in history, but is no longer the live target.
                    await previous.remove()
                    if previous_answer:
                        await transcript.mount(ChatMessage(
                            "assistant",
                            previous_answer,
                            usage=previous_usage,
                        ))
                await transcript.mount(
                    ChatMessage("user", content),
                    TranscriptNote(
                        self._progress_text(self.live),
                        classes="turn-metadata live-progress",
                    ),
                    ChatMessage(
                        "assistant",
                        self.live.assistant,
                        id="live-response",
                        thinking=not self.live.assistant,
                        usage=self._usage_text(self.live, self._session_totals()),
                    ),
                )
                self._reset_observatory()
                transcript.scroll_end(animate=False)
                self.watch_run(response.run.id, self._chat_generation)
                self.effort = "auto"
                self.refresh_catalog()
            composer.clear()
            self._drafts[self.current_id or ""] = ""
            self._drafts[""] = ""
            self._connected()
            self._update_title()
        except ApiError as exc:
            self._error(exc)
        finally:
            self._mutating = False
            self._controls()
            composer.focus()

    @work(group="stream", exclusive=True)
    async def watch_run(self, run_id: str, generation: int) -> None:
        failures = 0
        while generation == self._chat_generation:
            try:
                self.screen_stack[0].query_one("#stream-status", Static).update("LIVE / watching this thread")
                async for event in self.client.events(run_id, after=self.live.cursor):
                    if generation != self._chat_generation:
                        return
                    if self.live.apply(event):
                        failures = 0
                        if event.event_type == "steering.queued":
                            await self._show_steering(
                                event.payload.get("message_id"), event.payload.get("content"),
                            )
                    if event.event_type in {"run.completed", "run.failed", "run.cancelled"}:
                        break
                record = await self.client.run(run_id)
                if generation != self._chat_generation:
                    return
                self.run_record = record
                for event in record.events:
                    self.live.apply(event)
                if record.status not in ACTIVE_STATUSES:
                    restored = LiveRun.restore(record)
                    if record.status in {"failed", "cancelled"} and not restored.assistant:
                        restored.assistant = self.live.assistant
                    self.live = restored
                    await self._paint_live()
                    if record.status in {"failed", "cancelled"}:
                        await self.screen_stack[0].query_one("#transcript", VerticalScroll).mount(
                            TranscriptNote(
                                f"Run {record.status}. {record.error or 'Partial output may be incomplete.'}",
                                classes="run-note failed",
                            )
                        )
                    self.screen_stack[0].query_one("#stream-status", Static).update(
                        record.error or f"Run {record.status}. Your work stays local."
                    )
                    if record.error:
                        self.notify(record.error, title="Run failed", severity="error", timeout=15)
                    self._controls()
                    self.refresh_catalog()
                    return
                # The server closes lagging subscribers; resume from our last delivered sequence.
                failures += 1
            except ApiError as exc:
                failures += 1
                self.screen_stack[0].query_one("#stream-status", Static).update(f"Reconnecting ({failures}/5): {exc}")
            if failures >= 5:
                self.screen_stack[0].query_one("#stream-status", Static).update("Live connection lost. Ctrl+R reconnects; the run may still be active.")
                self.notify("Live connection lost. Refresh to resume.", severity="error")
                return
            await asyncio.sleep(min(2 ** (failures - 1), 8))

    def _reset_observatory(self) -> None:
        self._painted_text = None
        self._painted_activity = ()
        self.screen_stack[0].query_one("#activity", RichLog).clear()
        self.screen_stack[0].query_one("#stream-status", Static).update("Live activity, not hidden magic.")
        self._paint_clock()

    async def _paint_live(self) -> None:
        if not self.is_running:
            return
        bubbles = self.screen_stack[0].query("#live-response")
        if bubbles and self.live.assistant != self._painted_text:
            transcript = self.screen_stack[0].query_one("#transcript", VerticalScroll)
            pinned = transcript.is_vertical_scroll_end
            settled = self.run_record is not None and self.run_record.status not in ACTIVE_STATUSES
            await bubbles.first(ChatMessage).set_content(
                self.live.assistant or ("_No response text was produced._" if settled else ""),
                thinking=not self.live.assistant and not settled,
                label=self._phase(),
            )
            if not self.is_running:
                return
            self._painted_text = self.live.assistant
            if pinned:
                transcript.scroll_end(animate=False)
        activity = tuple(self.live.activity)
        if activity != self._painted_activity:
            log = self.screen_stack[0].query_one("#activity", RichLog)
            log.clear()
            for line in activity:
                log.write(self._activity_line(line))
            self._painted_activity = activity
            for bubble in self.screen_stack[0].query("#live-response").results(ChatMessage):
                bubble.set_activity_label(self._phase())
        usage_text = self._usage_text(self.live, self._session_totals())
        progress_text = self._progress_text(self.live)
        for metadata in self.screen_stack[0].query(".live-progress").results(TranscriptNote):
            metadata.update(progress_text)
            metadata.display = bool(progress_text)
        if bubbles:
            bubbles.first(ChatMessage).set_usage(usage_text)
        self.screen_stack[0].query_one("#usage", Static).update(usage_text or "No token usage yet")
        goal = self.live.goal_state or {}
        items = goal.get("items", goal.get("work_plan", []))
        plan = "### Work plan\n"
        if isinstance(items, list) and items:
            for item in items:
                if not isinstance(item, dict):
                    continue
                status = item.get("status", "pending")
                marker = {"completed": "✓", "in_progress": "▸", "blocked": "!"}.get(status, "·")
                plan += f"\n- {marker} {item.get('title') or item.get('description') or item.get('id')} ({status})"
        else:
            plan += "A plan appears here when your research needs one."
        markdown = self.screen_stack[0].query_one("#plan", Markdown)
        if getattr(self, "_painted_plan", None) != plan:
            self._painted_plan = plan
            await markdown.update(plan)

    def _phase(self) -> str:
        active_label = self.live.active_progress_label()
        if active_label:
            return active_label
        for entry in reversed(self.live.activity):
            kind, _, detail = entry.partition(": ")
            name = detail.split(" / ")[0].replace("_", " ").strip()
            if kind == "tool.started" and name:
                return f"Using {name}"
            if kind == "agent.started" and name:
                return f"{name} is working"
            if kind.startswith("context."):
                return "Gathering context"
            if kind.startswith("goal."):
                return "Shaping the plan"
            if kind == "steering.queued":
                return "Taking your steer"
            if kind == "model.retry":
                return "Retrying the model"
            if kind in {"tool.completed", "run.started", "run.recovered"}:
                break
        return "Following the thread"

    def _progress_text(self, live: LiveRun) -> str:
        lines: list[str] = []
        goal = live.goal_state or {}
        items = goal.get("items", goal.get("work_plan", []))
        if isinstance(items, list) and items:
            valid = [item for item in items if isinstance(item, dict)]
            completed = sum(item.get("status") == "completed" for item in valid)
            lines.append(f"Tasks  {completed}/{len(valid)} complete")
            for item in valid:
                status = item.get("status", "pending")
                marker = {"completed": "✓", "in_progress": "●", "blocked": "!"}.get(status, "○")
                title = item.get("title") or item.get("description") or item.get("id") or "Untitled task"
                lines.append(f"  {marker}  {' '.join(str(title).split())[:72]}")
        steps = live.progress[-7:]
        if steps:
            if lines:
                lines.append("Stages")
            hidden = max(0, len(live.progress) - len(steps))
            if hidden:
                lines.append(f"  ✓  {hidden} earlier stage{'s' if hidden != 1 else ''}")
            now = datetime.now(UTC)
            for step in steps:
                marker = "●" if step.status == "running" else "!" if step.status == "failed" else "✓"
                lines.append(f"  {marker}  {step.label}  ·  {self._duration(step.seconds(now))}")
        elif live.status in ACTIVE_STATUSES:
            started = live.started_at or datetime.now(UTC)
            lines.append(f"  ●  Starting  ·  {self._duration((datetime.now(UTC) - started).total_seconds())}")
        return "ACTIVITY\n" + "\n".join(lines) if lines else ""

    def _usage_text(self, live: LiveRun, session_totals: tuple[int, int]) -> str:
        input_tokens, output_tokens = self._turn_tokens(live)
        performance = live.usage.get("performance")
        cached = (
            performance.get("cached_input_tokens")
            if isinstance(performance, dict) and performance.get("cache_reported_calls", 0) > 0
            else live.usage.get("cached_input_tokens")
        )
        cached_count = self._token_count(cached) if type(cached) is int and cached >= 0 else "—"
        if not live.usage and live.context_tokens is None:
            return ""
        parts = [
            f"Tokens  in {self._token_count(input_tokens)}",
            f"out {self._token_count(output_tokens)}",
            f"cache {cached_count}",
            f"session {self._token_count(sum(session_totals))}",
        ]
        if live.context_window_tokens:
            context = live.context_tokens
            if context is None:
                parts.append(f"context — / {self._token_count(live.context_window_tokens)}")
            else:
                parts.append(
                    f"context {self._token_count(context)} / {self._token_count(live.context_window_tokens)}"
                )
        return "  ·  ".join(parts)

    def _session_totals(self) -> tuple[int, int]:
        if self._live_usage_in_history:
            return self._session_usage_before_live
        input_tokens, output_tokens = self._turn_tokens(self.live)
        before_input, before_output = self._session_usage_before_live
        return before_input + input_tokens, before_output + output_tokens

    @staticmethod
    def _turn_tokens(live: LiveRun) -> tuple[int, int]:
        input_tokens = live.usage.get("input_tokens")
        output_tokens = live.usage.get("output_tokens")
        return (
            input_tokens if type(input_tokens) is int and input_tokens >= 0 else 0,
            output_tokens if type(output_tokens) is int and output_tokens >= 0 else 0,
        )

    @staticmethod
    def _token_count(value: int) -> str:
        if value < 1_000:
            return str(value)
        if value < 1_000_000:
            return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
        return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "m"

    @staticmethod
    def _duration(seconds: float) -> str:
        if seconds < 1:
            return "<1s"
        if seconds < 60:
            return f"{round(seconds)}s"
        minutes = int(seconds // 60)
        return f"{minutes}m {round(seconds % 60)}s"

    def _activity_line(self, entry: str) -> Text:
        kind, _, detail = entry.partition(": ")
        line = Text()
        line.append(kind, style=self._palette("text-accent", "#e6ad83"))
        if detail:
            line.append("  " + detail, style=self._palette("text-muted", "#9babae"))
        return line

    def _paint_clock(self) -> None:
        if not self.is_running:
            return
        screen = self.screen_stack[0]
        record = self.run_record
        status = screen.query_one("#run-status", Static)
        progress = screen.query_one("#run-progress", ProgressBar)
        if record is None:
            status.update("○  READY WHEN YOU ARE")
            status.remove_class("active")
            progress.display = False
            screen.query_one("#run-agent", Static).update("No active run")
            screen.query_one("#run-elapsed", Static).update("00:00")
            return
        active = record.status in ACTIVE_STATUSES
        status.update(("●  " if active else "○  ") + record.status.upper())
        status.set_class(active, "active")
        progress.display = active
        started = record.started_at or record.created_at
        finished = record.finished_at or datetime.now(UTC)
        elapsed = max(0, int((finished - started).total_seconds()))
        clock = f"{elapsed // 60:02}:{elapsed % 60:02}"
        screen.query_one("#run-agent", Static).update(f"{record.agent_name} · {clock}")
        screen.query_one("#run-elapsed", Static).update(clock)
        spinner = screen.query_one("#run-spinner", Spinner)
        spinner.label = self._phase() if active else record.status.title()
        bubbles = screen.query("#live-response")
        progress_text = self._progress_text(self.live)
        for metadata in screen.query(".live-progress").results(TranscriptNote):
            metadata.update(progress_text)
            metadata.display = bool(progress_text)
        if bubbles:
            bubbles.first(ChatMessage).set_usage(self._usage_text(self.live, self._session_totals()))

    @work(group="mutation")
    async def stop_run(self) -> None:
        if not self.run_record or self.run_record.status not in ACTIVE_STATUSES or self._mutating:
            return
        self._mutating = True
        self._controls()
        try:
            self.run_record = await self.client.cancel_run(self.run_record.id)
            self.notify("Stop requested. Waiting for the run to settle.")
        except ApiError as exc:
            self._error(exc)
        finally:
            self._mutating = False
            self._controls()

    @work(group="new")
    async def action_new(self) -> None:
        if self._mutating or self._loading:
            return
        if self.view == "notes":
            if not await self._discard_note():
                return
            name = await self.push_screen_wait(NoteName())
            if name is None:
                return
            self._mutating = True
            try:
                self._load_note(await self.client.create_note(name))
                self.screen_stack[0].query_one("#note-content", ContentSwitcher).current = "note-editor"
                self.screen_stack[0].query_one("#edit-note", Button).label = "Preview"
                self.screen_stack[0].query_one("#note-editor", TextArea).focus()
                self.refresh_catalog()
                self._update_title()
            except ApiError as exc:
                self._error(exc)
            finally:
                self._mutating = False
                self._controls()
            return
        await self._new_chat()

    def _load_note(self, note: WorkspaceFileContentResponse) -> None:
        if not isinstance(note.content, str):
            self.notify("This file is not a text note and cannot be edited here.", severity="error")
            return
        self.note = note
        self._note_baseline = note.content
        self.screen_stack[0].query_one("#note-editor", TextArea).load_text(note.content)
        self.screen_stack[0].query_one("#note-preview", Markdown).update(note.content or "_An empty page. Make it yours._")
        self.screen_stack[0].query_one("#note-content", ContentSwitcher).current = "note-scroll"
        self.screen_stack[0].query_one("#note-path", Static).update(note.path)
        self.screen_stack[0].query_one("#edit-note", Button).disabled = False
        self.screen_stack[0].query_one("#edit-note", Button).label = "Edit"
        self.screen_stack[0].query_one("#note-state", Static).update("✓  Saved locally")

    def edit_note(self) -> None:
        if self.note and not self._mutating:
            switcher = self.screen_stack[0].query_one("#note-content", ContentSwitcher)
            editing = switcher.current == "note-editor"
            if editing:
                self.screen_stack[0].query_one("#note-preview", Markdown).update(
                    self.screen_stack[0].query_one("#note-editor", TextArea).text or "_An empty page. Make it yours._"
                )
            switcher.current = "note-scroll" if editing else "note-editor"
            self.screen_stack[0].query_one("#edit-note", Button).label = "Edit" if editing else "Preview"
            if not editing:
                self.screen_stack[0].query_one("#note-editor", TextArea).focus()

    def action_edit_note(self) -> None:
        if self.view == "notes":
            self.edit_note()

    @on(TextArea.Changed, "#note-editor")
    def note_changed(self) -> None:
        self.screen_stack[0].query_one("#note-state", Static).update(
            "●  Unsaved changes" if self.note_dirty else "✓  Saved locally" if self.note else ""
        )
        self._controls()

    @work(group="mutation")
    async def action_save_note(self) -> None:
        if self.view != "notes" or not self.note_dirty or self._mutating or self._loading or not self.note:
            return
        self._mutating = True
        editor = self.screen_stack[0].query_one("#note-editor", TextArea)
        editor.read_only = True
        self._controls()
        try:
            current = await self.client.read_note(self.note.path)
            if current.content != self._note_baseline:
                self.notify(
                    "This note changed outside the TUI. Your draft is retained. "
                    "Copy it before reopening the note to reconcile changes.",
                    title="Note changed on disk", severity="error", timeout=15,
                )
                return
            saved = await self.client.save_note(self.note.path, editor.text)
            self._load_note(saved)
            self.notify("Saved to your workspace and search index.")
            self.refresh_catalog()
        except ApiError as exc:
            self._error(exc)
        finally:
            self._mutating = False
            editor.read_only = False
            self._controls()

    @work(group="note-discard", exclusive=True)
    async def revert_note(self) -> None:
        if not self._mutating and self.note and await self._discard_note():
            self._load_note(self.note)
            self._controls()

    @work(group="discuss", exclusive=True)
    async def discuss_paper(self) -> None:
        if not self.paper or self._mutating or self._loading:
            return
        paper = self.paper
        await self._new_chat(
            f'Help me understand "{paper.title}" (local document ID: {paper.id}). '
            "Use the existing paper as evidence."
        )

    @work(group="quit", exclusive=True)
    async def action_quit(self) -> None:
        if self._mutating:
            self.notify("Wait for the current write to finish before quitting.", severity="warning")
            return
        if await self._discard_note():
            self.exit()
