"""
ui/tab_commands.py — extracted tab builder for _build_commands_tab.

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


def build_commands_tab(parent, app):
    pad = tk.Frame(parent, bg=Theme.BG_PANEL)
    pad.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
    panel_header(pad, "Command Reference", font_spec=app.F_HDR)
    # Search row
    search_row = tk.Frame(pad, bg=Theme.BG_PANEL)
    search_row.pack(fill=tk.X, pady=(6, 4))
    tk.Label(search_row, text="SEARCH:", fg=Theme.AMBER_DIM,
             bg=Theme.BG_PANEL, font=app.F_SMALL).pack(side=tk.LEFT)
    TermEntry(search_row, textvariable=app.cmd_search_var,
              font_spec=app.F_NORMAL).pack(side=tk.LEFT, fill=tk.X,
                                            expand=True, padx=6, ipady=2)
    app.cmd_search_var.trace_add('write', lambda *_: app._refresh_commands_tree())
    app.cmd_count_var = tk.StringVar(value="")
    tk.Label(search_row, textvariable=app.cmd_count_var,
             fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
             font=app.F_SMALL).pack(side=tk.LEFT, padx=6)
    TermButton(search_row, "↻ Reload", app._reload_commands_json,
               variant="amber", font_spec=app.F_SMALL,
               padx=8, pady=2).pack(side=tk.LEFT)
    # IMPORTANT: We pack the bottom-anchored items FIRST (with
    # side=tk.BOTTOM) so they always reserve their space before the
    # expanding tree above them. Without this, narrow windows would
    # clip the Insert/Send buttons and arg inspector off the bottom.
    #
    # Order matters even within `side=BOTTOM`: the LAST one packed sits
    # closest to the top of the bottom group, so we pack from the very
    # bottom upward (buttons → arg inspector → details).

    # Insert / send buttons (very bottom)
    btn_row = tk.Frame(pad, bg=Theme.BG_PANEL)
    btn_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
    TermButton(btn_row, "Insert →", app._insert_selected_command,
               variant="amber", font_spec=app.F_SMALL, padx=8, pady=3
               ).pack(side=tk.LEFT)
    TermButton(btn_row, "Send ▶", app._send_selected_command,
               variant="start", font_spec=app.F_SMALL, padx=8, pady=3
               ).pack(side=tk.LEFT, padx=(6, 0))

    # Arg inspector host (just above buttons)
    app.cmd_arg_host = ScrollableFrame(pad, bg=Theme.BG_PANEL,
                                        max_height=160)
    app.cmd_arg_host.pack(side=tk.BOTTOM, fill=tk.X)

    # Details (above arg inspector)
    app.cmd_preview_var = tk.StringVar()
    app.cmd_details = tk.Text(
        pad, bg=Theme.BG_INPUT, fg=Theme.AMBER_DIM,
        font=app.F_CONSOLE, bd=0, highlightthickness=0,
        wrap=tk.WORD, state='disabled', padx=8, pady=4, height=4)
    app.cmd_details.pack(side=tk.BOTTOM, fill=tk.X, pady=(0, 2))
    app.cmd_details.tag_configure("title",
                                   foreground=Theme.AMBER_GLOW,
                                   font=app.F_HDR)
    app.cmd_details.tag_configure("cat",   foreground=Theme.AMBER_DIM)
    app.cmd_details.tag_configure("body",  foreground=Theme.AMBER)

    # Command tree — fills all remaining space between the search row
    # at the top and the buttons/inspector/details at the bottom.
    tree_wrap = tk.Frame(pad, bg=Theme.BORDER)
    tree_wrap.pack(fill=tk.BOTH, expand=True, pady=(0, 4))
    tree_inner = tk.Frame(tree_wrap, bg=Theme.BG_INPUT)
    tree_inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
    app.cmd_tree = tk.Text(
        tree_inner, bg=Theme.BG_INPUT, fg=Theme.AMBER,
        font=app.F_NORMAL, bd=0, highlightthickness=0,
        wrap=tk.NONE, state='disabled', cursor="arrow", padx=6, pady=4)
    app.cmd_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    tsb = ttk.Scrollbar(tree_inner, orient=tk.VERTICAL,
                         style="Term.Vertical.TScrollbar",
                         command=app.cmd_tree.yview)
    tsb.pack(side=tk.RIGHT, fill=tk.Y)
    app.cmd_tree.configure(yscrollcommand=tsb.set)
    app.cmd_tree.tag_configure("category",
                                foreground=Theme.AMBER_GLOW,
                                font=app.F_HDR)
    app.cmd_tree.tag_configure("cmd", foreground=Theme.AMBER)
    app.cmd_tree.tag_configure("selected",
                                background=Theme.BG_SELECT,
                                foreground=Theme.AMBER_GLOW)
    app.cmd_tree.bind("<Button-1>", app._on_cmd_click)
    app.cmd_tree.tag_bind("category", "<Button-1>", app._on_cmd_category_click)
    app.cmd_tree.bind("<Double-Button-1>", app._on_cmd_dbl_click)

    total = sum(len(v) for v in app.commands_data.values())
    app.cmd_count_var.set(f"{total} commands")
    app._refresh_commands_tree()
