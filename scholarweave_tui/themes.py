"""Colour themes for the ScholarWeave terminal cockpit.

Every theme supplies the two ScholarWeave-specific variables used by ``app.tcss``:

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

MIDNIGHT_WEAVE = Theme(
    name="midnight-weave",
    primary="#e6ad83",
    secondary="#93cbb7",
    accent="#f0c9a6",
    success="#93cbb7",
    warning="#e8c07d",
    error="#ed9b98",
    foreground="#eee8dc",
    background="#0b1319",
    surface="#111e26",
    panel="#192a33",
    dark=True,
    variables={
        "sw-line": "#2c424a",
        "sw-glow": "#192a33",
        "footer-key-foreground": "#e6ad83",
        "block-cursor-foreground": "#0b1319",
        "input-selection-background": "#e6ad83 35%",
        "scrollbar": "#22343d",
        "scrollbar-hover": "#33505b",
        "scrollbar-active": "#e6ad83",
    },
)

PARCHMENT = Theme(
    name="parchment",
    primary="#a3572a",
    secondary="#3f6f5e",
    accent="#8a5a2b",
    success="#3f6f5e",
    warning="#9c6f1f",
    error="#a33a34",
    foreground="#2f2a24",
    background="#f3ecdd",
    surface="#faf5e9",
    panel="#e9dfc9",
    dark=False,
    variables={
        "sw-line": "#d6c8ab",
        "sw-glow": "#efe6d2",
        "footer-key-foreground": "#a3572a",
        "input-selection-background": "#a3572a 25%",
        "scrollbar": "#ded1b6",
        "scrollbar-hover": "#cbbb99",
        "scrollbar-active": "#a3572a",
    },
)

AURORA = Theme(
    name="aurora",
    primary="#88c0d0",
    secondary="#81a1c1",
    accent="#b48ead",
    success="#a3be8c",
    warning="#ebcb8b",
    error="#bf616a",
    foreground="#e5e9f0",
    background="#22272f",
    surface="#2e3440",
    panel="#3b4252",
    dark=True,
    variables={
        "sw-line": "#434c5e",
        "sw-glow": "#3b4252",
        "footer-key-foreground": "#88c0d0",
        "input-selection-background": "#88c0d0 30%",
        "scrollbar": "#3b4252",
        "scrollbar-hover": "#4c566a",
        "scrollbar-active": "#88c0d0",
    },
)

VERDANT = Theme(
    name="verdant",
    primary="#7fc79a",
    secondary="#a8c686",
    accent="#d6b26a",
    success="#7fc79a",
    warning="#d6b26a",
    error="#d98878",
    foreground="#e6efe4",
    background="#0a1310",
    surface="#111d17",
    panel="#18291f",
    dark=True,
    variables={
        "sw-line": "#27402f",
        "sw-glow": "#18291f",
        "footer-key-foreground": "#7fc79a",
        "input-selection-background": "#7fc79a 30%",
        "scrollbar": "#1e3227",
        "scrollbar-hover": "#2d4a38",
        "scrollbar-active": "#7fc79a",
    },
)

EMBER = Theme(
    name="ember",
    primary="#e0824f",
    secondary="#f0b35b",
    accent="#f0b35b",
    success="#9bbf7f",
    warning="#f0b35b",
    error="#e06c75",
    foreground="#f2e7dd",
    background="#15100d",
    surface="#1f1916",
    panel="#2a221d",
    dark=True,
    variables={
        "sw-line": "#3a2e27",
        "sw-glow": "#2a221d",
        "footer-key-foreground": "#f0b35b",
        "input-selection-background": "#e0824f 30%",
        "scrollbar": "#2f2620",
        "scrollbar-hover": "#463831",
        "scrollbar-active": "#e0824f",
    },
)

GLACIER = Theme(
    name="glacier",
    primary="#26688c",
    secondary="#4b8f9e",
    accent="#b25f2e",
    success="#2f7d5f",
    warning="#a8731f",
    error="#a83b3b",
    foreground="#1d2b36",
    background="#eaeff4",
    surface="#f7fafc",
    panel="#dbe4ec",
    dark=False,
    variables={
        "sw-line": "#c3d0dc",
        "sw-glow": "#e2eaf1",
        "footer-key-foreground": "#26688c",
        "input-selection-background": "#26688c 22%",
        "scrollbar": "#cfdae4",
        "scrollbar-hover": "#b3c3d1",
        "scrollbar-active": "#26688c",
    },
)

THEMES: tuple[Theme, ...] = (MIDNIGHT_WEAVE, PARCHMENT, AURORA, VERDANT, EMBER, GLACIER)
DEFAULT_THEME = MIDNIGHT_WEAVE.name

DESCRIPTIONS: dict[str, str] = {
    "midnight-weave": "Deep navy, warm paper and copper. The house style.",
    "parchment": "Daylight reading: warm paper with sienna ink.",
    "aurora": "Cool northern blues for long evening sessions.",
    "verdant": "Low-glare greens that stay calm under lamplight.",
    "ember": "Warm charcoal and amber for late, focused work.",
    "glacier": "Crisp light blues for bright rooms and projectors.",
}
