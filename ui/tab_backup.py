"""
ui/tab_backup.py — Backup tab builder.

Lists existing backups in the configured destination folder with
per-entry Restore / Delete buttons, alongside the global Backup Now /
Cancel / Prune controls.
"""
from __future__ import annotations

import os
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from core.utils import fmt_size
from .theme import Theme
from .widgets import (TermButton, TermEntry, TermText, TermCheckbutton,
                      Sparkline, ScrollableFrame, themed_frame,
                      panel_header, collapsible_section)


def build_backup_tab(parent, app):
    pad = tk.Frame(parent, bg=Theme.BG_PANEL)
    pad.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
    panel_header(pad, "World Backup", font_spec=app.F_HDR)
    btn_row = tk.Frame(pad, bg=Theme.BG_PANEL)
    btn_row.pack(fill=tk.X, pady=8)
    TermButton(btn_row, "💾 Backup Now", app.backup_world,
               variant="start", font_spec=app.F_BTN, padx=12, pady=6
               ).pack(side=tk.LEFT)
    TermButton(btn_row, "■ Cancel", app.cancel_active_backup,
               variant="stop", font_spec=app.F_SMALL, padx=10, pady=6
               ).pack(side=tk.LEFT, padx=(8, 0))
    TermButton(btn_row, "↩ Restore (browse…)", app.restore_backup,
               variant="amber", font_spec=app.F_SMALL, padx=10, pady=6
               ).pack(side=tk.LEFT, padx=(8, 0))
    TermButton(btn_row, "✂ Prune", app.prune_old_backups,
               variant="amber", font_spec=app.F_SMALL, padx=10, pady=6
               ).pack(side=tk.LEFT, padx=(8, 0))
    TermButton(btn_row, "↻ Refresh", lambda: app._refresh_backup_list(),
               variant="amber", font_spec=app.F_SMALL, padx=10, pady=6
               ).pack(side=tk.LEFT, padx=(8, 0))

    # Backup date-based retention
    ret_row = tk.Frame(pad, bg=Theme.BG_PANEL)
    ret_row.pack(fill=tk.X, pady=(4, 0))
    tk.Label(ret_row, text="Retention mode:", fg=Theme.AMBER_DIM,
             bg=Theme.BG_PANEL, font=app.F_SMALL).pack(side=tk.LEFT)
    app._retention_mode_var = tk.StringVar(value="count")
    for val, lbl in [("count", "Keep last N"), ("days", "Keep last N days")]:
        tk.Radiobutton(ret_row, text=lbl, variable=app._retention_mode_var,
                       value=val, fg=Theme.AMBER, bg=Theme.BG_PANEL,
                       activeforeground=Theme.AMBER_GLOW,
                       activebackground=Theme.BG_PANEL,
                       selectcolor=Theme.BG_INPUT,
                       font=app.F_SMALL).pack(side=tk.LEFT, padx=6)

    status = tk.Label(pad, text="No backup in progress.",
                      fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
                      font=app.F_SMALL, anchor=tk.W)
    status.pack(fill=tk.X, pady=(8, 0))
    app._backup_status_label = status

    # Existing-backups list
    list_header = tk.Frame(pad, bg=Theme.BG_PANEL)
    list_header.pack(fill=tk.X, pady=(12, 2))
    tk.Label(list_header, text="Existing backups:",
             fg=Theme.AMBER_GLOW, bg=Theme.BG_PANEL,
             font=app.F_HDR).pack(side=tk.LEFT)
    app._backup_count_var = tk.StringVar(value="(0)")
    tk.Label(list_header, textvariable=app._backup_count_var,
             fg=Theme.MUTED, bg=Theme.BG_PANEL,
             font=app.F_SMALL).pack(side=tk.LEFT, padx=(6, 0))

    list_wrap = tk.Frame(pad, bg=Theme.BORDER)
    list_wrap.pack(fill=tk.BOTH, expand=True, pady=(0, 4))
    sf = ScrollableFrame(list_wrap, bg=Theme.BG_INPUT)
    sf.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
    app._backup_list_body = sf.body

    # Render once on tab open; the user can hit Refresh to re-scan.
    app._refresh_backup_list()


# _build_mods_tab is defined in the "Mods" section near the
# bottom of this class — full implementation ported from v2.
