"""Colour themes for the ScholarWeave terminal cockpit.

The three themes mirror the web UI (Light, Sepia and Dark) so both front ends
share one restrained palette. Every theme supplies the two ScholarWeave-specific
variables used by ``app.tcss``:

``$sw-line``
    Hairline colour for dividers and quiet borders.
``$sw-glow``
    Soft wash used behind cards, bubbles and raised rows.

``ScholarWeaveApp.get_theme_variable_defaults`` provides neutral fallbacks so the
built-in Textual themes stay usable from the command palette.
"""

from __future__ import annotations

from textual.theme import Theme

VARIABLE_DEFAULTS: dict[str, str] = {
    "sw-line": "#808080 40%",
    "sw-glow": "#808080 12%",
}

LIGHT = Theme(
    name="light",
    primary="#2f5c9e",
    secondary="#4f6680",
    accent="#2c6f7c",
    success="#2e7550",
    warning="#94660c",
    error="#b03a3a",
    foreground="#1f2933",
    background="#f5f6f8",
    surface="#ffffff",
    panel="#e6e9ee",
    dark=False,
    variables={
        "sw-line": "#c8cdd5",
        "sw-glow": "#f2f4f7",
        "footer-key-foreground": "#2f5c9e",
        "input-selection-background": "#2f5c9e 22%",
        "scrollbar": "#dde1e7",
        "scrollbar-hover": "#c8cdd5",
        "scrollbar-active": "#2f5c9e",
    },
)

SEPIA = Theme(
    name="sepia",
    primary="#8a4f2e",
    secondary="#5d6a50",
    accent="#3f6b66",
    success="#4c7438",
    warning="#8f6512",
    error="#a33d31",
    foreground="#2d2924",
    background="#f3eee4",
    surface="#fbf8f2",
    panel="#e7dfd0",
    dark=False,
    variables={
        "sw-line": "#cbbfa9",
        "sw-glow": "#f2ece1",
        "footer-key-foreground": "#8a4f2e",
        "input-selection-background": "#8a4f2e 22%",
        "scrollbar": "#e2d9c9",
        "scrollbar-hover": "#cbbfa9",
        "scrollbar-active": "#8a4f2e",
    },
)

DARK = Theme(
    name="dark",
    primary="#7ea3dc",
    secondary="#8e9cb0",
    accent="#70afbb",
    success="#6dbb8a",
    warning="#d2a65c",
    error="#e07b72",
    foreground="#e2e4e7",
    background="#16181c",
    surface="#1f2227",
    panel="#30353c",
    dark=True,
    variables={
        "sw-line": "#434951",
        "sw-glow": "#262a30",
        "footer-key-foreground": "#7ea3dc",
        "block-cursor-foreground": "#16181c",
        "input-selection-background": "#7ea3dc 30%",
        "scrollbar": "#2e3238",
        "scrollbar-hover": "#434951",
        "scrollbar-active": "#7ea3dc",
    },
)

THEMES: tuple[Theme, ...] = (LIGHT, SEPIA, DARK)
DEFAULT_THEME = LIGHT.name

DESCRIPTIONS: dict[str, str] = {
    "light": "Neutral white with a muted blue accent. The default.",
    "sepia": "Warm, low-glare paper for long reading.",
    "dark": "Neutral graphite for low light.",
}

# Retired themes map to the closest current one so a saved choice survives.
LEGACY_THEMES: dict[str, str] = {
    "midnight-weave": "dark",
    "aurora": "dark",
    "verdant": "dark",
    "ember": "dark",
    "parchment": "sepia",
    "glacier": "light",
}
