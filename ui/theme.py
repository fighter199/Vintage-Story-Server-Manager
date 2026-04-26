"""
ui/theme.py — Color constants, CRT palettes, and font resolution.
"""
from __future__ import annotations

import tkinter.font as tkfont


def _is_valid_hex_color(value) -> bool:
    if not isinstance(value, str):
        return False
    if len(value) != 7 or not value.startswith("#"):
        return False
    try:
        int(value[1:], 16)
        return True
    except ValueError:
        return False


class Theme:
    AMBER         = "#ffb000"
    AMBER_DIM     = "#996a00"
    AMBER_GLOW    = "#ffcc44"
    AMBER_FAINT   = "#443300"
    GREEN         = "#33ff33"
    GREEN_DIM     = "#1a8a1a"
    RED           = "#ff4444"
    RED_DIM       = "#662222"
    CYAN          = "#44ffff"
    PURPLE        = "#cc88ff"
    BG_DARK       = "#0a0a0a"
    BG_PANEL      = "#111111"
    BG_INPUT      = "#0d0d0d"
    BG_HEADER     = "#1a1200"
    BG_BTN_START  = "#0a1a0a"
    BG_BTN_START_HOVER = "#0d2a0d"
    BG_BTN_STOP   = "#1a0a0a"
    BG_BTN_STOP_HOVER  = "#2a0d0d"
    BG_BTN_AMBER  = "#1a1200"
    BG_BTN_AMBER_HOVER = "#2a1d00"
    BG_SELECT     = "#2a1d00"
    BORDER        = "#332200"
    DIVIDER       = "#1a1a1a"
    MUTED         = "#555555"
    DOT_OFF       = "#333333"

    PRESETS = {
        "amber": {},
        "green": {
            "AMBER": "#33ff33", "AMBER_DIM": "#1a8a1a",
            "AMBER_GLOW": "#88ff88", "AMBER_FAINT": "#113311",
            "BG_HEADER": "#001800", "BG_BTN_AMBER": "#001800",
            "BG_BTN_AMBER_HOVER": "#002b00", "BG_SELECT": "#003300",
            "BORDER": "#0a3a0a",
        },
        "cyan": {
            "AMBER": "#44ffff", "AMBER_DIM": "#1a8a8a",
            "AMBER_GLOW": "#aaffff", "AMBER_FAINT": "#113333",
            "BG_HEADER": "#001818", "BG_BTN_AMBER": "#001818",
            "BG_BTN_AMBER_HOVER": "#002b2b", "BG_SELECT": "#003333",
            "BORDER": "#0a3a3a",
        },
        # Neutral dark mode (improvement #13)
        "dark": {
            "AMBER": "#e0e0e0", "AMBER_DIM": "#888888",
            "AMBER_GLOW": "#ffffff", "AMBER_FAINT": "#2a2a2a",
            "BG_HEADER": "#1e1e1e", "BG_BTN_AMBER": "#1e1e1e",
            "BG_BTN_AMBER_HOVER": "#2e2e2e", "BG_SELECT": "#3a3a3a",
            "BORDER": "#333333",
        },
        "custom": {},
    }

    CUSTOMIZABLE_KEYS = (
        "AMBER", "AMBER_DIM", "AMBER_GLOW", "AMBER_FAINT",
        "BG_HEADER", "BG_BTN_AMBER", "BG_BTN_AMBER_HOVER",
        "BG_SELECT", "BORDER",
    )

    @classmethod
    def apply_preset(cls, name: str):
        # Reset to amber defaults first so presets are idempotent
        cls._reset_to_amber()
        preset = cls.PRESETS.get(name, {})
        for k, v in preset.items():
            setattr(cls, k, v)

    @classmethod
    def load_custom_colors(cls, overrides: dict):
        if not isinstance(overrides, dict):
            return
        for k, v in overrides.items():
            if k not in cls.CUSTOMIZABLE_KEYS:
                continue
            if not isinstance(v, str):
                continue
            v = v.strip()
            if not _is_valid_hex_color(v):
                continue
            setattr(cls, k, v)

    @classmethod
    def _reset_to_amber(cls):
        cls.AMBER        = "#ffb000"
        cls.AMBER_DIM    = "#996a00"
        cls.AMBER_GLOW   = "#ffcc44"
        cls.AMBER_FAINT  = "#443300"
        cls.BG_HEADER    = "#1a1200"
        cls.BG_BTN_AMBER = "#1a1200"
        cls.BG_BTN_AMBER_HOVER = "#2a1d00"
        cls.BG_SELECT    = "#2a1d00"
        cls.BORDER       = "#332200"


# -----------------------------------------------------------------------
# Font resolution
# -----------------------------------------------------------------------
FONT_CANDIDATES = [
    "VT323", "Share Tech Mono", "Consolas", "Courier New",
    "DejaVu Sans Mono", "Monaco", "Liberation Mono",
]


def pick_mono_font(root) -> str:
    available = {f.lower() for f in tkfont.families(root)}
    for name in FONT_CANDIDATES:
        if name.lower() in available:
            return name
    return "Courier"
