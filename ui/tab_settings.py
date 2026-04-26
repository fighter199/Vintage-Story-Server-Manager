"""
ui/tab_settings.py — extracted tab builder for _build_settings_tab.

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


def build_settings_tab(parent, app):
    sf = ScrollableFrame(parent, bg=Theme.BG_PANEL)
    sf.pack(fill=tk.BOTH, expand=True)
    pad = sf.body
    # Server paths — entry, Browse, and Open in file manager
    for label, var, browse_fn, open_fn in [
        ("Mods Folder",        app.mods_folder_var,  app.browse_mods_folder,   app.open_mods_folder),
        ("World Folder",       app.world_folder_var, app.browse_world_folder,  app.open_world_folder),
        ("Backup Destination", app.backup_dir_var,   app.browse_backup_folder, app.open_backup_folder),
    ]:
        row = tk.Frame(pad, bg=Theme.BG_PANEL)
        row.pack(fill=tk.X, padx=10, pady=(8, 0))
        tk.Label(row, text=label + ":", fg=Theme.AMBER_DIM,
                 bg=Theme.BG_PANEL, font=app.F_SMALL).pack(anchor=tk.W)
        entry_row = tk.Frame(row, bg=Theme.BG_PANEL)
        entry_row.pack(fill=tk.X)
        TermEntry(entry_row, textvariable=var,
                  font_spec=app.F_NORMAL).pack(
            side=tk.LEFT, fill=tk.X, expand=True, ipady=2)
        TermButton(entry_row, "Browse", browse_fn,
                   variant="amber", font_spec=app.F_SMALL,
                   padx=8, pady=2).pack(side=tk.LEFT, padx=(6, 0))
        TermButton(entry_row, "📂 Open", open_fn,
                   variant="amber", font_spec=app.F_SMALL,
                   padx=8, pady=2).pack(side=tk.LEFT, padx=(4, 0))

    # Max backups
    row = tk.Frame(pad, bg=Theme.BG_PANEL)
    row.pack(fill=tk.X, padx=10, pady=(8, 0))
    tk.Label(row, text="Max Backups:", fg=Theme.AMBER_DIM,
             bg=Theme.BG_PANEL, font=app.F_SMALL).pack(side=tk.LEFT)
    TermEntry(row, textvariable=app.max_backups_var, width=6,
              font_spec=app.F_NORMAL).pack(side=tk.LEFT, padx=6, ipady=2)

    # Crash-loop config (improvement #15)
    crash_row = tk.Frame(pad, bg=Theme.BG_PANEL)
    crash_row.pack(fill=tk.X, padx=10, pady=(10, 0))
    tk.Label(crash_row, text="Crash limit:", fg=Theme.AMBER_DIM,
             bg=Theme.BG_PANEL, font=app.F_SMALL).pack(side=tk.LEFT)
    app._crash_limit_var = tk.StringVar(
        value=str(app._settings.get("crash_limit", 3)))
    TermEntry(crash_row, textvariable=app._crash_limit_var, width=4,
              font_spec=app.F_SMALL).pack(side=tk.LEFT, padx=4)
    tk.Label(crash_row, text="crashes in",
             fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
             font=app.F_SMALL).pack(side=tk.LEFT, padx=(8, 0))
    app._crash_window_var = tk.StringVar(
        value=str(app._settings.get("crash_window_secs", 600)))
    TermEntry(crash_row, textvariable=app._crash_window_var, width=6,
              font_spec=app.F_SMALL).pack(side=tk.LEFT, padx=4)
    tk.Label(crash_row, text="secs → auto-restart disabled",
             fg=Theme.MUTED, bg=Theme.BG_PANEL,
             font=app.F_SMALL).pack(side=tk.LEFT)

    # Player-count poll interval
    poll_row = tk.Frame(pad, bg=Theme.BG_PANEL)
    poll_row.pack(fill=tk.X, padx=10, pady=(10, 0))
    tk.Label(poll_row, text="Player count poll interval:",
             fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
             font=app.F_SMALL).pack(side=tk.LEFT)
    app._player_poll_var = tk.StringVar(
        value=str(app._settings.get("player_count_poll_secs", 30)))
    TermEntry(poll_row, textvariable=app._player_poll_var, width=6,
              font_spec=app.F_SMALL).pack(side=tk.LEFT, padx=6)
    tk.Label(poll_row, text="seconds  (0 = disabled)",
             fg=Theme.MUTED, bg=Theme.BG_PANEL,
             font=app.F_SMALL).pack(side=tk.LEFT)
    tk.Label(
        pad,
        text=("How often VSSM sends /list clients to refresh the player "
              "list while the server is running. Lower values give more "
              "up-to-date counts at the cost of slightly more chatter in "
              "the server log."),
        fg=Theme.MUTED, bg=Theme.BG_PANEL,
        font=app.F_SMALL, justify=tk.LEFT, wraplength=620,
    ).pack(anchor=tk.W, padx=10, pady=(2, 0))

    # Checkboxes
    for text, var in [
        ("Auto-restart on crash",  app.autorestart_var),
        ("Backup before start",    app.backup_before_start_var),
        ("Backup before stop",     app.backup_before_stop_var),
        ("Auto-save enabled",      app.autosave_enabled_var),
        ("Send /autosavenow with backup", app.autosave_cmd_var),
    ]:
        TermCheckbutton(pad, text, var, font_spec=app.F_NORMAL
                        ).pack(anchor=tk.W, padx=10, pady=(6, 0))

    # Auto-save interval
    row2 = tk.Frame(pad, bg=Theme.BG_PANEL)
    row2.pack(fill=tk.X, padx=10, pady=(6, 0))
    tk.Label(row2, text="Auto-save every (min):", fg=Theme.AMBER_DIM,
             bg=Theme.BG_PANEL, font=app.F_SMALL).pack(side=tk.LEFT)
    TermEntry(row2, textvariable=app.autosave_interval_var, width=5,
              font_spec=app.F_NORMAL).pack(side=tk.LEFT, padx=6)

    # Cron schedule (improvement #16: live validation)
    tk.Label(pad, text="Recurring restart schedule:",
             fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
             font=app.F_SMALL).pack(anchor=tk.W, padx=10, pady=(10, 0))
    cron_row = tk.Frame(pad, bg=Theme.BG_PANEL)
    cron_row.pack(fill=tk.X, padx=10)
    TermEntry(cron_row, textvariable=app.cron_expr_var,
              font_spec=app.F_NORMAL).pack(side=tk.LEFT, fill=tk.X,
                                            expand=True, ipady=2)
    app._cron_status_var = tk.StringVar(value="")
    tk.Label(cron_row, textvariable=app._cron_status_var,
             fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
             font=app.F_SMALL).pack(side=tk.LEFT, padx=6)
    app.cron_expr_var.trace_add('write', lambda *_: app._validate_cron_live())

    tk.Label(
        pad,
        text=(
            "Times when the server should automatically restart. Use 24-hour "
            "HH:MM, optionally prefixed by a weekday (mon, tue, wed, thu, fri, "
            "sat, sun). Separate multiple entries with commas or semicolons.\n"
            "Examples:   06:00          (every day at 6 AM)\n"
            "            06:00, 18:00   (every day at 6 AM and 6 PM)\n"
            "            mon 04:00; fri 22:30   (Mondays 4 AM, Fridays 10:30 PM)\n"
            "Leave blank to disable scheduled restarts."
        ),
        fg=Theme.MUTED, bg=Theme.BG_PANEL,
        font=app.F_SMALL, justify=tk.LEFT, wraplength=620,
    ).pack(anchor=tk.W, padx=10, pady=(2, 0))

    # Shutdown timeout
    row3 = tk.Frame(pad, bg=Theme.BG_PANEL)
    row3.pack(fill=tk.X, padx=10, pady=(6, 0))
    tk.Label(row3, text="Shutdown timeout (secs):", fg=Theme.AMBER_DIM,
             bg=Theme.BG_PANEL, font=app.F_SMALL).pack(side=tk.LEFT)
    TermEntry(row3, textvariable=app.shutdown_timeout_var, width=5,
              font_spec=app.F_SMALL).pack(side=tk.LEFT, padx=6)

    # Theme preset (improvement #13: dark mode added)
    tk.Label(pad, text="CRT Theme:", fg=Theme.AMBER_DIM,
             bg=Theme.BG_PANEL, font=app.F_SMALL
             ).pack(anchor=tk.W, padx=10, pady=(10, 0))
    theme_row = tk.Frame(pad, bg=Theme.BG_PANEL)
    theme_row.pack(fill=tk.X, padx=10)
    for t in ("amber", "green", "cyan", "dark", "custom"):
        tk.Radiobutton(theme_row, text=t, variable=app.theme_preset_var,
                       value=t, command=app._on_theme_change,
                       fg=Theme.AMBER, bg=Theme.BG_PANEL,
                       activeforeground=Theme.AMBER_GLOW,
                       activebackground=Theme.BG_PANEL,
                       selectcolor=Theme.BG_INPUT,
                       font=app.F_SMALL).pack(side=tk.LEFT, padx=4)

    # Log housekeeping
    log_row = tk.Frame(pad, bg=Theme.BG_PANEL)
    log_row.pack(fill=tk.X, padx=10, pady=(12, 0))
    tk.Label(log_row, text="Log files:",
             fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
             font=app.F_SMALL).pack(side=tk.LEFT)
    TermButton(log_row, "📂 Open logs folder",
               app.open_logs_folder,
               variant="amber", font_spec=app.F_SMALL,
               padx=8, pady=2).pack(side=tk.LEFT, padx=(8, 0))
    TermButton(log_row, "🗑 Clear old logs",
               app.clear_old_logs,
               variant="stop", font_spec=app.F_SMALL,
               padx=8, pady=2).pack(side=tk.LEFT, padx=(4, 0))
    tk.Label(pad,
             text=("Removes everything in the logs/ folder except the two "
                   "currently-active log files. Useful after running the app "
                   "for a long time and accumulating rotated backups."),
             fg=Theme.MUTED, bg=Theme.BG_PANEL,
             font=app.F_SMALL, justify=tk.LEFT, wraplength=620,
             ).pack(anchor=tk.W, padx=10, pady=(1, 0))

    # Save button
    TermButton(pad, "💾 Save Settings", app._save_profile_settings,
               variant="start", font_spec=app.F_BTN, padx=14, pady=6
               ).pack(pady=(14, 8))
