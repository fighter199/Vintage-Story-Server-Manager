"""
ui/theme.py — Color constants, CRT palettes, and font resolution.
"""
from __future__ import annotations



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
    # Text colours (AMBER*, MUTED, GREEN, RED, CYAN, PURPLE) are kept at
    # a WCAG contrast of at least 4.5:1 against every panel background
    # in every built-in preset — tests/test_theme.py enforces it.
    AMBER         = "#ffb000"
    AMBER_DIM     = "#b57d00"
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
    MUTED         = "#878787"
    DOT_OFF       = "#333333"

    PRESETS = {
        "amber": {},
        "green": {
            "AMBER": "#33ff33", "AMBER_DIM": "#1e9d1e",
            "AMBER_GLOW": "#88ff88", "AMBER_FAINT": "#113311",
            "BG_HEADER": "#001800", "BG_BTN_AMBER": "#001800",
            "BG_BTN_AMBER_HOVER": "#002b00", "BG_SELECT": "#003300",
            "BORDER": "#0a3a0a",
        },
        "cyan": {
            "AMBER": "#44ffff", "AMBER_DIM": "#1d9999",
            "AMBER_GLOW": "#aaffff", "AMBER_FAINT": "#113333",
            "BG_HEADER": "#001818", "BG_BTN_AMBER": "#001818",
            "BG_BTN_AMBER_HOVER": "#002b2b", "BG_SELECT": "#003333",
            "BORDER": "#0a3a3a",
        },
        # Neutral dark mode (improvement #13)
        "dark": {
            "AMBER": "#e0e0e0", "AMBER_DIM": "#aaaaaa",
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
        cls.AMBER_DIM    = "#b57d00"
        cls.AMBER_GLOW   = "#ffcc44"
        cls.AMBER_FAINT  = "#443300"
        cls.BG_HEADER    = "#1a1200"
        cls.BG_BTN_AMBER = "#1a1200"
        cls.BG_BTN_AMBER_HOVER = "#2a1d00"
        cls.BG_SELECT    = "#2a1d00"
        cls.BORDER       = "#332200"


# -----------------------------------------------------------------------
# Live preset changes
# -----------------------------------------------------------------------
def palette() -> dict:
    """The colours a preset can change: {key: "#rrggbb"}, lower case."""
    return {k: getattr(Theme, k).lower() for k in Theme.CUSTOMIZABLE_KEYS}


_FG_OPTIONS = frozenset({"foreground", "fg", "activeforeground",
                         "selectforeground", "insertbackground",
                         "disabledforeground", "fill"})
_TEXT_KEYS = ("AMBER", "AMBER_DIM", "AMBER_GLOW")
_BUTTON_KEYS = ("BG_BTN_AMBER", "BG_BTN_AMBER_HOVER")
_BORDER_KEYS = ("BORDER", "AMBER_DIM", "AMBER")
_BG_KEYS = ("BG_HEADER", "BG_SELECT", "BG_BTN_AMBER", "BG_BTN_AMBER_HOVER",
            "AMBER_FAINT", "BORDER")


class ColorRemap:
    """Maps colours of an old palette to a new one, for recolouring
    widgets that were built with the old one.

    Presets reuse colours — amber's BG_SELECT and BG_BTN_AMBER_HOVER are
    both #2a1d00, but green's differ — so where one old colour stood for
    several keys, the option it's set on and whether the widget is a
    button decide which key it was."""

    def __init__(self, old: dict, new: dict):
        self._by_value: dict = {}          # old colour -> {key: new colour}
        for key, value in old.items():
            target = str(new.get(key, value)).lower()
            self._by_value.setdefault(str(value).lower(), {})[key] = target
        self._changes = any(k != t for k, keys in self._by_value.items()
                            for t in keys.values())

    def __bool__(self) -> bool:
        return self._changes

    def __call__(self, color, option: str = "", button: bool = False):
        """The new colour for `color`, or `color` itself if it isn't one
        of the old palette's."""
        if not isinstance(color, str):
            return color
        keys = self._by_value.get(color.lower())
        if not keys:
            return color
        targets = set(keys.values())
        if len(targets) == 1:
            target = targets.pop()
        else:
            target = keys[self._pick(keys, option.lstrip("-"), button)]
        return color if target == color.lower() else target

    @staticmethod
    def _pick(keys: dict, option: str, button: bool) -> str:
        order: tuple = ()
        if button:
            order += _BUTTON_KEYS
        if option in _FG_OPTIONS:
            order += _TEXT_KEYS
        elif option.startswith("highlight") or option == "outline":
            order += _BORDER_KEYS
        order += _BG_KEYS + _TEXT_KEYS
        return next((k for k in order if k in keys), next(iter(keys)))


# -----------------------------------------------------------------------
# Font resolution
# -----------------------------------------------------------------------
FONT_CANDIDATES = [
    "VT323", "Share Tech Mono", "Consolas", "Courier New",
    "DejaVu Sans Mono", "Monaco", "Liberation Mono",
]


def pick_mono_font(root) -> str:
    import tkinter.font as tkfont
    available = {f.lower() for f in tkfont.families(root)}
    for name in FONT_CANDIDATES:
        if name.lower() in available:
            return name
    return "Courier"


# -----------------------------------------------------------------------
# Font sizes
# -----------------------------------------------------------------------
# Point sizes at 100% text size. Tk converts points to pixels using the
# display's DPI, so these are NOT multiplied by a DPI factor as well —
# doing both made text grow with the square of the scale.
FONT_LADDER = {
    "F_TITLE":   (22, True),
    "F_SUB":     (10, False),
    "F_HDR":     (10, True),
    "F_NORMAL":  (10, False),
    "F_SMALL":   (9,  False),
    "F_BTN":     (10, True),
    "F_CONSOLE": (10, False),
}
TEXT_SCALE_MIN = 0.7
TEXT_SCALE_MAX = 2.0


def font_sizes(text_scale: float = 1.0, pixel_font: bool = False,
               aqua: bool = False) -> dict:
    """{font attribute: (size in points, bold)} for a text-size factor.

    Pixel fonts (VT323, Share Tech Mono) draw small for their point
    size, and macOS maps 1 pt to 1 logical pixel (Windows/Linux: 1.33),
    so both get a boost to look the same size as elsewhere."""
    text_scale = max(TEXT_SCALE_MIN, min(TEXT_SCALE_MAX, text_scale))
    extra = (3 if pixel_font else 0) + (3 if aqua else 0)
    return {name: (max(7, round((pt + extra) * text_scale)), bold)
            for name, (pt, bold) in FONT_LADDER.items()}


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 2 contrast ratio between two #rrggbb colours."""
    def lum(color: str) -> float:
        c = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
             for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)
