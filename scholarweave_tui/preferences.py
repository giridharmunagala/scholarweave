"""Local, single-user preferences for the terminal cockpit.

The cockpit remembers only how it is dressed and how the composer is set up
(theme, focus mode, per-message defaults, and per-model reasoning/context
choices). Everything that changes research behaviour stays in backend settings.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from scholarweave_tui.themes import DEFAULT_THEME

FILENAME = "tui-preferences.json"
PREFERENCES_VERSION = 2


def default_path() -> Path:
    data_dir = os.environ.get("SCHOLARWEAVE_DATA_DIR")
    root = Path(data_dir) if data_dir else Path(__file__).resolve().parents[1] / "local_data"
    return root / FILENAME


def model_key(provider_profile_id: str | None, model: str | None) -> str | None:
    return f"{provider_profile_id}/{model}" if provider_profile_id and model else None


class Preferences:
    """Best-effort JSON preference file. Presentation only, never required."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()
        self._values = self._read()
        if self._values.get("version") != PREFERENCES_VERSION:
            self._values["version"] = PREFERENCES_VERSION
            self._values["focus_mode"] = False
            self._save()

    def _read(self) -> dict[str, object]:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._values, indent=2), encoding="utf-8")
        except OSError:
            # A read-only data directory must never interrupt a research session.
            pass

    def _set(self, key: str, value: object) -> None:
        if self._values.get(key) == value:
            return
        self._values[key] = value
        self._save()

    @property
    def theme(self) -> str:
        value = self._values.get("theme")
        return value if isinstance(value, str) and value else DEFAULT_THEME

    def save_theme(self, name: str) -> None:
        self._set("theme", name)

    @property
    def focus_mode(self) -> bool:
        value = self._values.get("focus_mode")
        return value if isinstance(value, bool) else False

    def save_focus_mode(self, focused: bool) -> None:
        self._set("focus_mode", focused)

    @property
    def web_enabled(self) -> bool:
        value = self._values.get("web_enabled")
        return value if isinstance(value, bool) else True

    def save_web_enabled(self, enabled: bool) -> None:
        self._set("web_enabled", enabled)

    def reasoning(self, provider_profile_id: str | None, model: str | None) -> str | None:
        key = model_key(provider_profile_id, model)
        stored = self._values.get("reasoning")
        if key is None or not isinstance(stored, dict):
            return None
        value = stored.get(key)
        return value if isinstance(value, str) and value else None

    def save_reasoning(
        self, provider_profile_id: str | None, model: str | None, effort: str | None,
    ) -> None:
        key = model_key(provider_profile_id, model)
        if key is None:
            return
        stored = self._values.get("reasoning")
        levels = dict(stored) if isinstance(stored, dict) else {}
        if effort:
            levels[key] = effort
        else:
            levels.pop(key, None)
        self._set("reasoning", levels)

    def context_window(
        self, provider_profile_id: str | None, model: str | None,
    ) -> int | None:
        key = model_key(provider_profile_id, model)
        stored = self._values.get("context_windows")
        if key is None or not isinstance(stored, dict):
            return None
        value = stored.get(key)
        return value if isinstance(value, int) and 4_096 <= value <= 2_000_000 else None

    def save_context_window(
        self, provider_profile_id: str | None, model: str | None, tokens: int,
    ) -> None:
        key = model_key(provider_profile_id, model)
        if key is None:
            return
        stored = self._values.get("context_windows")
        windows = dict(stored) if isinstance(stored, dict) else {}
        windows[key] = tokens
        self._set("context_windows", windows)
