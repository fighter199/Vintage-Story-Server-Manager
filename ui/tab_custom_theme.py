"""
ui/tab_custom_theme.py — extracted tab builder for _build_custom_theme_tab.

This module contains a single build_*_tab function called by
ServerManagerApp during _build_ui. The function takes (parent, app)
where `app` is the ServerManagerApp instance (formerly `self`).
"""
from __future__ import annotations

import os
import re
import tkinter as tk
from tkinter import colorchooser, ttk

from .theme import Theme
from .widgets import (TermButton, TermEntry, TermText, TermCheckbutton,
                      Sparkline, ScrollableFrame, themed_frame,
                      panel_header, collapsible_section)


_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def _normalize_hex(value: str) -> str | None:
    """Return a `#rrggbb` string if `value` is a valid 6-digit hex, else
    None. Accepts inputs with or without the leading '#'."""
    if not value:
        return None
    v = value.strip()
    if not _HEX_RE.match(v):
        return None
    if not v.startswith("#"):
        v = "#" + v
    return v.lower()


def build_custom_theme_tab(parent, app):
    pad = tk.Frame(parent, bg=Theme.BG_PANEL)
    pad.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
    panel_header(pad, "Custom CRT Colors", font_spec=app.F_HDR)
    tk.Label(pad,
             text="Select 'custom' theme in Settings then edit colors below.\n"
                  "Click the swatch to open the color picker, or type a hex "
                  "code. Changes take effect after Save + restart.",
             fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
             font=app.F_SMALL, pady=8, justify=tk.LEFT,
             ).pack(anchor=tk.W)
    app._custom_color_vars: dict = {}
    sf = ScrollableFrame(pad, bg=Theme.BG_PANEL)
    sf.pack(fill=tk.BOTH, expand=True)
    body = sf.body

    for key in Theme.CUSTOMIZABLE_KEYS:
        row = tk.Frame(body, bg=Theme.BG_PANEL)
        row.pack(fill=tk.X, pady=2)
        tk.Label(row, text=key, fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
                 font=app.F_SMALL, width=22, anchor=tk.W).pack(side=tk.LEFT)
        saved = app._settings.get("custom_theme_colors", {}) or {}
        default_val = saved.get(key) or getattr(Theme, key)
        var = tk.StringVar(value=default_val)
        app._custom_color_vars[key] = var

        # Swatch — a small clickable Label whose bg is the current colour.
        # Using Label rather than a real Tk Frame avoids the "no size
        # because empty" problem; Label respects width/height arguments.
        initial_swatch = _normalize_hex(default_val) or "#000000"
        swatch = tk.Label(
            row, text="  ", bg=initial_swatch,
            relief=tk.SOLID, bd=1, cursor="hand2",
            highlightbackground=Theme.BORDER, highlightthickness=1,
            width=3,
        )
        swatch.pack(side=tk.LEFT, padx=(0, 6), ipady=2)

        TermEntry(row, textvariable=var, width=10,
                  font_spec=app.F_SMALL).pack(side=tk.LEFT, ipady=2)

        # Live-update the swatch when the entry text changes. Capture
        # `swatch` and `var` in default args so each row has its own.
        def _on_change(*_args, _swatch=swatch, _var=var):
            colour = _normalize_hex(_var.get())
            if colour is not None:
                try:
                    _swatch.configure(bg=colour)
                except tk.TclError:
                    pass
        var.trace_add("write", _on_change)

        # Click swatch (or pick button) to open color chooser.
        def _open_picker(_event=None, _swatch=swatch, _var=var, _key=key):
            current = _normalize_hex(_var.get()) or "#000000"
            try:
                rgb, hex_str = colorchooser.askcolor(
                    color=current,
                    title=f"Pick color — {_key}",
                    parent=parent,
                )
            except tk.TclError:
                return
            if not hex_str:
                return  # user cancelled
            # askcolor returns either #rgb (3-digit) or #rrggbb depending
            # on the platform; normalise to lowercase 6-digit.
            normalised = _normalize_hex(hex_str)
            if normalised is None:
                # 3-digit form -> expand to 6
                if hex_str.startswith("#") and len(hex_str) == 4:
                    r, g, b = hex_str[1], hex_str[2], hex_str[3]
                    normalised = f"#{r}{r}{g}{g}{b}{b}".lower()
                else:
                    return
            _var.set(normalised)

        swatch.bind("<Button-1>", _open_picker)
        TermButton(row, "Pick…", _open_picker,
                   variant="amber", font_spec=app.F_SMALL,
                   padx=8, pady=2).pack(side=tk.LEFT, padx=(6, 0))

    TermButton(body, "💾 Save Custom Colors", app._save_custom_colors,
               variant="start", font_spec=app.F_BTN, padx=12, pady=6
               ).pack(pady=(12, 0))
