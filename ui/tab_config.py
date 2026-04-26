"""
ui/tab_config.py — extracted tab builder for _build_config_tab.

This module contains a single build_*_tab function called by
ServerManagerApp during _build_ui. The function takes (parent, app)
where `app` is the ServerManagerApp instance (formerly `self`).
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

from .theme import Theme
from .widgets import (TermButton, TermEntry, TermText, TermCheckbutton,
                      Sparkline, ScrollableFrame, themed_frame,
                      panel_header, collapsible_section)


def build_config_tab(parent, app):
    pad = tk.Frame(parent, bg=Theme.BG_PANEL)
    pad.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
    btns = tk.Frame(pad, bg=Theme.BG_PANEL)
    btns.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
    TermButton(btns, "📂 Open Config", app.open_config_file,
               variant="amber", font_spec=app.F_BTN, padx=12, pady=6
               ).pack(side=tk.LEFT)
    TermButton(btns, "💾 Save", app.save_config_file,
               variant="start", font_spec=app.F_BTN, padx=12, pady=6
               ).pack(side=tk.LEFT, padx=(8, 0))
    panel_header(pad, "serverconfig.json", font_spec=app.F_HDR)
    cfg_wrap = tk.Frame(pad, bg=Theme.BORDER)
    cfg_wrap.pack(fill=tk.BOTH, expand=True)
    cfg_inner = tk.Frame(cfg_wrap, bg=Theme.BG_INPUT)
    cfg_inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
    app.config_text = TermText(cfg_inner, font=app.F_CONSOLE,
                                wrap=tk.NONE, padx=8, pady=6)
    app.config_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    csb = ttk.Scrollbar(cfg_inner, orient=tk.VERTICAL,
                         style="Term.Vertical.TScrollbar",
                         command=app.config_text.yview)
    csb.pack(side=tk.RIGHT, fill=tk.Y)
    app.config_text.configure(yscrollcommand=csb.set)
