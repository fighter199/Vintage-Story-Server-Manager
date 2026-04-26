"""
VSSM.py — Main entry point for Vintage Story Server Manager v3.

Module layout:
  core/
    constants.py    — APP_NAME, APP_VERSION, logging
    parsers.py      — line classification, player events, JSON5, cron, chat
    settings.py     — load/save/migrate settings (atomic write)
    custom_commands.py — ChatCommandDispatcher + rule validation
    utils.py        — port check, backup zip, file manager, DPI
  ui/
    theme.py        — Theme class + font resolution
    widgets.py      — TermButton, TermEntry, Sparkline, ScrollableFrame,
                       ToastQueue, panel_header, themed_frame, …
    tab_custom_commands.py — Custom Commands tab (NEW)
  mods/
    inspector.py    — LocalModInspector
    moddb.py        — ModDbClient
  VSSM.py     — ServerManagerApp (this file)

Improvements implemented vs v2:
  1.  Split into modules (this file + core/ui/mods/backup packages)
  4.  Type hints completed throughout
  7.  Backup ZIP integrity check (testzip) after write
  8.  Crash-loop threshold configurable in Settings
  9.  Console search / filter bar (already existed, kept)
 10.  Console right-click → copy line to clipboard
 12.  Toast notification queue — no more overlapping toasts
 13.  Neutral dark mode added alongside amber/green/cyan
 14.  Ban confirmation dialog
 15.  Crash-loop threshold UI in Settings
 16.  Cron schedule entry validated live with parse_cron_expr
 17.  Settings save is now atomic (tmp rename)
 18.  One-shot scheduled restart jobs persist in settings
 19.  requirements.txt ships alongside this file
 20.  --log-level CLI argument
 21.  main() entry-point function (testable)
  +   Custom Commands tab (NEW)
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import zipfile
from collections import deque
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

# ── Local packages ─────────────────────────────────────────────────────
from core.constants import (APP_NAME, APP_VERSION, LOG, SERVER_LOG,
                              OPERATOR_ROLES, script_dir)
from core.parsers import (classify_line, parse_player_event, split_client_list,
                           parse_role_response, parse_json5_ish,
                           parse_cron_expr, seconds_until_next,
                           parse_chat_message)
from core.settings import (load_settings, save_settings, get_active_profile,
                            load_custom_commands, save_custom_commands)
from core.custom_commands import ChatCommandDispatcher
from core.utils import (is_port_free, find_vs_port, open_in_file_manager,
                         clean_mod_filename, fmt_size, backup_world_to_zip,
                         restore_backup_zip, enable_windows_dpi_awareness)
from ui.theme import Theme, pick_mono_font
from ui.widgets import (TermButton, TermEntry, TermText, TermCheckbutton,
                         Sparkline, ScrollableFrame, themed_frame,
                         panel_header, collapsible_section, ToastQueue)
from ui.tab_custom_commands import CustomCommandsTab
from mods.inspector import LocalModInspector
from mods.moddb import ModDbClient
from backup import BackupManager

# Optional psutil
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


# ======================================================================
# Command reference loader
# ======================================================================
FALLBACK_COMMANDS = {
    "Server": {
        "/stop":   {"description": "Stop the server.", "template": "/stop", "args": []},
        "/save":   {"description": "Save the world.",  "template": "/save",  "args": []},
        "/players":{"description": "List connected players.", "template": "/players", "args": []},
    }
}


def _normalize_command_entry(cmd_name: str, raw) -> Optional[dict]:
    if isinstance(raw, str):
        return {"description": raw, "template": cmd_name, "args": []}
    if not isinstance(raw, dict):
        return None
    entry = dict(raw)
    entry.setdefault("description", "")
    entry.setdefault("template", cmd_name)
    entry.setdefault("args", [])
    return entry


def load_commands_data() -> dict:
    try:
        sdir = script_dir()
    except Exception:
        sdir = os.getcwd()
    json_path = os.path.join(sdir, "vs_commands.json")
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = parse_json5_ish(f.read())
    except (FileNotFoundError, Exception):
        return FALLBACK_COMMANDS
    if not isinstance(data, dict) or not data:
        return FALLBACK_COMMANDS
    out = {}
    for category, cmds in data.items():
        if category.startswith("_") or not isinstance(cmds, dict):
            continue
        cat_out = {}
        for cmd_name, raw in cmds.items():
            entry = _normalize_command_entry(cmd_name, raw)
            if entry is not None:
                cat_out[cmd_name] = entry
        if cat_out:
            out[category] = cat_out
    return out or FALLBACK_COMMANDS


# ======================================================================
# Boot splash
# ======================================================================
class BootSplash(tk.Toplevel):
    BOOT_LINES = [
        ("╔══════════════════════════════════════╗", Theme.AMBER_GLOW),
        ("║  VSERVERMAN v3 — INITIALIZING        ║", Theme.AMBER),
        ("║  Vintage Story Server Manager        ║", Theme.MUTED),
        ("╚══════════════════════════════════════╝", Theme.AMBER_GLOW),
        ("", None),
        ("Loading modules...", Theme.AMBER_DIM),
        ("  ✓ Core parsers",       Theme.GREEN),
        ("  ✓ Settings engine",    Theme.GREEN),
        ("  ✓ Custom commands",    Theme.GREEN),
        ("  ✓ Backup scheduler",   Theme.GREEN),
        ("  ✓ Mod manager",        Theme.GREEN),
        ("  ✓ Console subsystem",  Theme.GREEN),
        ("  ✓ Chat dispatcher",    Theme.GREEN),
        ("", None),
        ("  System ready. Launching UI...", Theme.AMBER_GLOW),
    ]

    def __init__(self, master, font_spec, on_done, scale=1.0):
        super().__init__(master)
        self.overrideredirect(True)
        self.configure(bg=Theme.BG_DARK)
        self._on_done = on_done
        w, h = int(520 * scale), int(400 * scale)
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        border = tk.Frame(self, bg=Theme.BORDER)
        border.pack(fill=tk.BOTH, expand=True)
        inner = tk.Frame(border, bg=Theme.BG_DARK)
        inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        pad_x = max(12, int(20 * scale))
        pad_y = max(14, int(24 * scale))
        self._text = tk.Text(inner, bg=Theme.BG_DARK, fg=Theme.AMBER,
                             font=font_spec, bd=0, highlightthickness=0,
                             wrap=tk.NONE, cursor="arrow",
                             padx=pad_x, pady=pad_y)
        self._text.pack(fill=tk.BOTH, expand=True)
        self._text.configure(state='disabled')
        self.after(80, self._next_line, 0)

    def _next_line(self, idx):
        if idx >= len(self.BOOT_LINES):
            self.after(400, self._finish)
            return
        text, color = self.BOOT_LINES[idx]
        tag = f"line{idx}"
        self._text.configure(state='normal')
        self._text.insert(tk.END, text + "\n")
        if color:
            self._text.tag_add(tag, f"{idx + 1}.0", f"{idx + 1}.end")
            self._text.tag_configure(tag, foreground=color)
        self._text.configure(state='disabled')
        self.after(90, self._next_line, idx + 1)

    def _finish(self):
        try:
            self._on_done()
        finally:
            self.destroy()


# ======================================================================
# Main Application
# ======================================================================
class ServerManagerApp(tk.Tk):

    # ---- Configurable crash-loop defaults (now overridden by settings) ----
    CRASH_WINDOW_SECS = 600
    CRASH_LIMIT       = 3

    def __init__(self):
        super().__init__()
        self.withdraw()
        self.title("VSSM — Vintage Story Server Manager")
        self.configure(bg=Theme.BG_DARK)

        # ---- Load settings early (needed for theme + scale) ----------
        self._settings = load_settings()
        profile = get_active_profile(self._settings)

        # ---- Display scaling -----------------------------------------
        self._auto_scale = self._detect_scale()
        try:
            self._user_scale = float(self._settings.get("ui_scale_override") or 1.0)
        except (TypeError, ValueError):
            self._user_scale = 1.0
        self._user_scale = max(0.6, min(2.5, self._user_scale))
        self._ui_scale   = self._auto_scale * self._user_scale
        try:
            self.tk.call("tk", "scaling", self._ui_scale * (96.0 / 72.0))
        except tk.TclError:
            pass

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        win_w = min(int(sw * 0.75), max(1100, int(1280 * self._ui_scale)))
        win_h = min(int(sh * 0.85), max(700,  int(820  * self._ui_scale)))
        x = max(0, (sw - win_w) // 2)
        y = max(0, (sh - win_h) // 3)
        self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.minsize(min(900, int(sw * 0.6)), min(600, int(sh * 0.6)))

        # ---- Theme ---------------------------------------------------
        preset = self._settings.get("theme_preset", "amber")
        if preset != "amber":
            Theme.apply_preset(preset)
        if preset == "custom":
            Theme.load_custom_colors(self._settings.get("custom_theme_colors", {}))
        self._theme_preset = preset

        # ---- Fonts ---------------------------------------------------
        self._mono_name = pick_mono_font(self)
        self._rebuild_fonts()

        # ---- Crash-loop config (improvement #15) ---------------------
        self.CRASH_LIMIT       = int(self._settings.get("crash_limit",        3))
        self.CRASH_WINDOW_SECS = int(self._settings.get("crash_window_secs",  600))

        # ---- State ---------------------------------------------------
        self.server_process    = None
        self.output_queue: queue.Queue = queue.Queue()
        self.is_running        = False
        self.all_output_lines  = []    # (timestamp, text, tag)
        self.autosave_job_id   = None
        self.scheduled_restart_id = None
        self.start_time        = None

        self._shutdown_in_progress = False
        self._shutdown_callbacks   = []
        self._crash_times: deque   = deque(maxlen=20)
        self._cmd_history: list    = []
        self._cmd_history_pos      = 0
        self._cron_entries         = []
        self._cron_job_id          = None
        self._cron_warning_jobs    = []
        self._restart_warning_jobs = []
        self._cpu_history: deque   = deque(maxlen=120)
        self._mem_history: deque   = deque(maxlen=120)

        # Players
        self._players: list        = []
        self._operators: set       = set()
        self._pending_role_query: deque = deque()
        # role cache: player_name -> role string
        self._player_roles: dict   = {}

        # Commands reference
        self.commands_data = load_commands_data()
        self._cmd_index        = {}
        self._cmd_cat_rows     = {}
        self._cmd_collapsed_cats: set = set()
        self._cmd_selected_row = None
        self._cmd_arg_vars: dict = {}

        # ModDB
        self.moddb = ModDbClient()
        self._moddb_results    = []
        self._moddb_current_mod = None
        self._moddb_current_file = None
        self._moddb_search_job = None
        self._moddb_request_seq = 0
        self._moddb_download_cancel = {"flag": False}
        self._moddb_download_active = False
        self._moddb_update_cache: dict = {}
        self._moddb_selected_tagids: set = set()
        self._moddb_tag_buttons: dict = {}

        # Custom commands dispatcher (audit listener wired up after the
        # CUSTOM CMDS tab is built — see _build_ui).
        self._cmd_dispatcher = ChatCommandDispatcher(
            lambda: load_custom_commands(self._settings))

        # Tk variables
        self.server_path_var          = tk.StringVar()
        self.command_var              = tk.StringVar()
        self.log_filter_var           = tk.StringVar()
        self.cmd_search_var           = tk.StringVar()
        self.status_var               = tk.StringVar(value="(OFFLINE)")
        self.uptime_var               = tk.StringVar(value="00:00:00")
        self.player_count_var         = tk.StringVar(value="0")
        self.world_folder_var         = tk.StringVar()
        self.backup_dir_var           = tk.StringVar()
        self.max_backups_var          = tk.StringVar(value="10")
        self.backup_before_start_var  = tk.BooleanVar()
        self.backup_before_stop_var   = tk.BooleanVar()
        self.autosave_enabled_var     = tk.BooleanVar(value=False)
        self.autosave_interval_var    = tk.StringVar(value="30")
        self.autosave_cmd_var         = tk.BooleanVar(value=True)
        self.autorestart_var          = tk.BooleanVar()
        self.restart_interval_var     = tk.StringVar()
        self.mods_folder_var          = tk.StringVar()
        self.config_file_path         = None
        self.cron_expr_var            = tk.StringVar(value="")
        self.active_profile_var       = tk.StringVar(
            value=self._settings.get("active_profile", "default"))
        self.theme_preset_var         = tk.StringVar(value=preset)
        self.shutdown_timeout_var     = tk.StringVar(value="30")
        self.moddb_search_var         = tk.StringVar()
        self.moddb_sort_var           = tk.StringVar(value="trendingpoints")
        self.moddb_side_var           = tk.StringVar(value="server_compat")
        self.moddb_gv_var             = tk.StringVar(value="")
        self.moddb_status_var         = tk.StringVar(value="Ready.")

        # Backup manager (improvement #2: extracted from inline methods)
        self._backup_manager = BackupManager(self)

        self._build_ui()
        self._apply_default_paths()

        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        BootSplash(self, self.F_NORMAL, on_done=self._after_boot,
                   scale=self._ui_scale)

    # ------------------------------------------------------------------
    # Post-boot
    # ------------------------------------------------------------------
    def _after_boot(self):
        self.deiconify()
        self.lift()
        self._toast = ToastQueue(self, self.F_SMALL)
        self._blink_cursor()
        self._glow_title()
        self._tick_uptime()
        self._reschedule_player_count_poll()
        # Scale shortcuts
        self.bind_all("<Control-equal>",      lambda _: self._bump_ui_scale(+0.1))
        self.bind_all("<Control-plus>",       lambda _: self._bump_ui_scale(+0.1))
        self.bind_all("<Control-KP_Add>",     lambda _: self._bump_ui_scale(+0.1))
        self.bind_all("<Control-minus>",      lambda _: self._bump_ui_scale(-0.1))
        self.bind_all("<Control-KP_Subtract>",lambda _: self._bump_ui_scale(-0.1))
        self.bind_all("<Control-Key-0>",      lambda _: self._reset_ui_scale())
        self.bind_all("<Control-l>", lambda _: self.clear_console())
        self.bind_all("<Control-L>", lambda _: self.clear_console())
        self.bind_all("<Control-Return>", lambda _: self.send_command())
        # Quick-focus the server command entry from anywhere.
        self.bind_all("<Control-slash>", lambda _: self._focus_command_entry())
        try:
            self.command_entry.bind("<Up>",   self._cmd_history_prev)
            self.command_entry.bind("<Down>", self._cmd_history_next)
        except Exception:
            pass
        # Console right-click: copy line (improvement #10)
        try:
            self.console_text.bind("<Button-3>", self._console_right_click)
            self.console_text.bind("<Button-2>", self._console_right_click)
        except Exception:
            pass

        self.append_console("VSSM v3 initialized. Ready.", "system")
        self.append_console(
            "Hotkeys: Ctrl+L clear · Ctrl+Enter send · ↑/↓ history · "
            "Right-click console to copy", "system")
        LOG.info("%s %s started", APP_NAME, APP_VERSION)
        self.after(150, self.init_moddb_catalogs_async)

    # ------------------------------------------------------------------
    # Scale + fonts
    # ------------------------------------------------------------------
    def _detect_scale(self) -> float:
        try:
            ppi = self.winfo_fpixels("1i")
            scale = ppi / 96.0
        except Exception:
            scale = 1.0
        return max(0.75, min(3.0, scale))

    def _rebuild_fonts(self):
        pixel_fonts = self._mono_name.lower() in ("vt323", "share tech mono")
        base = 14 if pixel_fonts else 11
        base = max(9, round(base * self._ui_scale))
        self.F_TITLE   = (self._mono_name, max(14, round(22 * self._ui_scale)), "bold")
        self.F_SUB     = (self._mono_name, max(8,  round(10 * self._ui_scale)))
        self.F_HDR     = (self._mono_name, max(9,  base - 2), "bold")
        self.F_NORMAL  = (self._mono_name, max(8,  base - 3))
        self.F_SMALL   = (self._mono_name, max(7,  base - 4))
        self.F_BTN     = (self._mono_name, max(9,  base - 2), "bold")
        self.F_CONSOLE = (self._mono_name, max(8,  base - 3))

    def _bump_ui_scale(self, delta):
        new = round(self._user_scale + delta, 2)
        new = max(0.6, min(2.5, new))
        if abs(new - self._user_scale) < 0.001:
            return
        self._user_scale = new
        self._ui_scale   = self._auto_scale * self._user_scale
        self._reapply_scale()

    def _reset_ui_scale(self):
        self._user_scale = 1.0
        self._ui_scale   = self._auto_scale
        self._reapply_scale()

    def _reapply_scale(self):
        try:
            self.tk.call("tk", "scaling", self._ui_scale * (96.0 / 72.0))
        except tk.TclError:
            pass
        self._rebuild_fonts()
        self._ttk_style_ready = False
        self._setup_ttk_style()
        self._notify(f"UI scale {self._ui_scale:.2f}x — restart to fully apply.",
                     level="info")
        self._settings["ui_scale_override"] = self._user_scale
        save_settings(self._settings)

    # ------------------------------------------------------------------
    # Notifications (improvement #12 — queued toasts)
    # ------------------------------------------------------------------
    def _notify(self, message: str, level: str = "info", duration_ms: int = 2500):
        try:
            self._toast.push(message, level, duration_ms)
        except AttributeError:
            pass  # called before _after_boot; safe to ignore

    # ------------------------------------------------------------------
    # ttk style
    # ------------------------------------------------------------------
    def _setup_ttk_style(self):
        if getattr(self, '_ttk_style_ready', False):
            return
        style = ttk.Style(self)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure("Term.Vertical.TScrollbar",
                        background=Theme.AMBER_DIM, troughcolor=Theme.BG_DARK,
                        bordercolor=Theme.BORDER, arrowcolor=Theme.AMBER,
                        lightcolor=Theme.BG_DARK, darkcolor=Theme.BG_DARK)
        style.configure("Term.Horizontal.TScrollbar",
                        background=Theme.AMBER_DIM, troughcolor=Theme.BG_DARK,
                        bordercolor=Theme.BORDER, arrowcolor=Theme.AMBER,
                        lightcolor=Theme.BG_DARK, darkcolor=Theme.BG_DARK)
        style.configure("Term.TNotebook",
                        background=Theme.BG_DARK, borderwidth=0,
                        tabmargins=[0, 0, 0, 0])
        style.configure("Term.TNotebook.Tab",
                        background=Theme.BG_DARK, foreground=Theme.AMBER_DIM,
                        padding=[14, 6], borderwidth=0, font=self.F_NORMAL)
        style.map("Term.TNotebook.Tab",
                  background=[("selected", Theme.BG_PANEL), ("active", Theme.BG_PANEL)],
                  foreground=[("selected", Theme.AMBER_GLOW), ("active", Theme.AMBER)],
                  bordercolor=[("selected", Theme.BORDER)])
        style.configure("Term.TPanedwindow",
                        background=Theme.BORDER, sashwidth=4,
                        sashrelief="flat")
        style.configure("Term.TCombobox",
                        fieldbackground=Theme.BG_INPUT,
                        background=Theme.BG_PANEL,
                        foreground=Theme.AMBER,
                        bordercolor=Theme.BORDER,
                        darkcolor=Theme.BORDER,
                        arrowcolor=Theme.AMBER,
                        insertcolor=Theme.AMBER_GLOW,
                        selectbackground=Theme.BG_SELECT,
                        selectforeground=Theme.AMBER_GLOW)
        style.map("Term.TCombobox",
                  fieldbackground=[("readonly", Theme.BG_INPUT)],
                  foreground=[("readonly", Theme.AMBER)],
                  selectbackground=[("readonly", Theme.BG_SELECT)],
                  selectforeground=[("readonly", Theme.AMBER_GLOW)])
        self.option_add("*TCombobox*Listbox.background",       Theme.BG_INPUT)
        self.option_add("*TCombobox*Listbox.foreground",       Theme.AMBER)
        self.option_add("*TCombobox*Listbox.selectBackground", Theme.BG_SELECT)
        self.option_add("*TCombobox*Listbox.selectForeground", Theme.AMBER_GLOW)
        self._ttk_style_ready = True

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        self._setup_ttk_style()

        root_pad = tk.Frame(self, bg=Theme.BG_DARK)
        root_pad.pack(fill=tk.BOTH, expand=True, padx=15, pady=10)

        # Header
        header = tk.Frame(root_pad, bg=Theme.BG_DARK)
        header.pack(fill=tk.X, pady=(5, 10))
        header.columnconfigure(0, weight=1, uniform="hdr")
        header.columnconfigure(1, weight=2, uniform="hdr")
        header.columnconfigure(2, weight=1, uniform="hdr")

        left_col = tk.Frame(header, bg=Theme.BG_DARK)
        left_col.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 8))
        self.setup_warning_var   = tk.StringVar(value="")
        self.setup_warning_label = tk.Label(
            left_col, textvariable=self.setup_warning_var,
            fg=Theme.RED, bg=Theme.BG_DARK,
            font=self.F_SMALL, justify=tk.LEFT, anchor="nw", wraplength=320)
        self.setup_warning_label.pack(anchor="nw")

        center_col = tk.Frame(header, bg=Theme.BG_DARK)
        center_col.grid(row=0, column=1, rowspan=2, sticky="n")
        self.title_label = tk.Label(
            center_col, text="⛏  VINTAGE STORY SERVER MANAGER  ⛏",
            fg=Theme.AMBER_GLOW, bg=Theme.BG_DARK, font=self.F_TITLE)
        self.title_label.pack()
        tk.Label(center_col, text=f"[ VSERVERMAN v{APP_VERSION} ]",
                 fg=Theme.AMBER_DIM, bg=Theme.BG_DARK,
                 font=self.F_SUB).pack(pady=(2, 0))

        right_col = tk.Frame(header, bg=Theme.BG_DARK)
        right_col.grid(row=0, column=2, rowspan=2, sticky="ne", padx=(8, 0))
        tk.Label(right_col, text="HOTKEYS", fg=Theme.AMBER_GLOW,
                 bg=Theme.BG_DARK, font=self.F_SMALL).pack(anchor="ne")
        tk.Label(right_col,
                 text="Ctrl+L        clear console\n"
                      "Ctrl+Enter    send command\n"
                      "↑ / ↓         history\n"
                      "Right-click   copy / player actions\n"
                      "Ctrl + =      larger UI\n"
                      "Ctrl + −      smaller UI\n"
                      "Ctrl + 0      reset UI scale",
                 fg=Theme.AMBER_DIM, bg=Theme.BG_DARK,
                 font=self.F_SMALL, justify=tk.RIGHT).pack(anchor="ne", pady=(2, 0))

        tk.Frame(root_pad, bg=Theme.BORDER, height=2).pack(fill=tk.X, pady=(0, 12))

        # Exe selector
        exe_panel = themed_frame(root_pad)
        exe_panel.pack(fill=tk.X, pady=(0, 10))
        exe_row = tk.Frame(exe_panel.inner, bg=Theme.BG_PANEL)
        exe_row.pack(fill=tk.X, padx=12, pady=8)
        tk.Label(exe_row, text="▸ EXECUTABLE:",
                 fg=Theme.AMBER_GLOW, bg=Theme.BG_PANEL,
                 font=self.F_HDR).pack(side=tk.LEFT)
        TermEntry(exe_row, textvariable=self.server_path_var,
                  font_spec=self.F_NORMAL).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=10, ipady=3)
        TermButton(exe_row, "Browse", self.browse_executable,
                   variant="amber", font_spec=self.F_SMALL,
                   padx=10, pady=4).pack(side=tk.LEFT)

        # Status bar
        status_outer = themed_frame(root_pad)
        status_outer.pack(fill=tk.X, pady=(0, 10))
        status_row = tk.Frame(status_outer.inner, bg=Theme.BG_PANEL)
        status_row.pack(fill=tk.X, padx=12, pady=8)
        left = tk.Frame(status_row, bg=Theme.BG_PANEL)
        left.pack(side=tk.LEFT)
        self.status_dot = tk.Canvas(left, width=14, height=14,
                                    bg=Theme.BG_PANEL, bd=0, highlightthickness=0)
        self.status_dot.pack(side=tk.LEFT, padx=(0, 8))
        self._dot_id = self.status_dot.create_oval(
            2, 2, 12, 12, fill=Theme.DOT_OFF, outline=Theme.MUTED)
        tk.Label(left, text="Vintage Story Server",
                 fg=Theme.AMBER_GLOW, bg=Theme.BG_PANEL, font=self.F_HDR
                 ).pack(side=tk.LEFT)
        tk.Label(left, textvariable=self.status_var,
                 fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL, font=self.F_NORMAL
                 ).pack(side=tk.LEFT, padx=8)
        right = tk.Frame(status_row, bg=Theme.BG_PANEL)
        right.pack(side=tk.RIGHT)
        for prefix, var in [("👥 Players: ", self.player_count_var),
                             ("🕐 Uptime: ",  self.uptime_var)]:
            cell = tk.Frame(right, bg=Theme.BG_PANEL)
            cell.pack(side=tk.LEFT, padx=10)
            tk.Label(cell, text=prefix, fg=Theme.AMBER_DIM,
                     bg=Theme.BG_PANEL, font=self.F_NORMAL).pack(side=tk.LEFT)
            tk.Label(cell, textvariable=var, fg=Theme.AMBER,
                     bg=Theme.BG_PANEL, font=self.F_NORMAL).pack(side=tk.LEFT)

        # Control buttons
        ctrl = tk.Frame(root_pad, bg=Theme.BG_DARK)
        ctrl.pack(fill=tk.X, pady=(0, 12))
        self.btn_start   = TermButton(ctrl, "▶ Start",   self.start_server,  variant="start", font_spec=self.F_BTN)
        self.btn_stop    = TermButton(ctrl, "■ Stop",    self.stop_server,   variant="stop",  font_spec=self.F_BTN)
        self.btn_restart = TermButton(ctrl, "↻ Restart", self.restart_server,variant="amber", font_spec=self.F_BTN)
        self.btn_clear   = TermButton(ctrl, "✕ Clear",   self.clear_console, variant="clear", font_spec=self.F_BTN)
        self.btn_stop.set_enabled(False)
        self.btn_restart.set_enabled(False)
        self._install_wrapping_row(ctrl,
            [self.btn_start, self.btn_stop, self.btn_restart, self.btn_clear])

        # Main split: console | sidebar
        main_paned = ttk.PanedWindow(root_pad, orient=tk.HORIZONTAL,
                                     style="Term.TPanedwindow")
        main_paned.pack(fill=tk.BOTH, expand=True)
        console_col = tk.Frame(main_paned, bg=Theme.BG_DARK)
        main_paned.add(console_col, weight=3)
        self._build_console(console_col)
        sidebar_col = tk.Frame(main_paned, bg=Theme.BG_DARK)
        main_paned.add(sidebar_col, weight=2)
        self._build_sidebar(sidebar_col)

        def _seed_main(retry=0):
            try:
                w = main_paned.winfo_width()
                if w > 20:
                    main_paned.sashpos(0, int(w * 0.6))
                elif retry < 20:
                    self.after(100, lambda: _seed_main(retry + 1))
            except tk.TclError:
                pass
        self.after(150, _seed_main)

        self.server_path_var.trace_add('write', lambda *_: self._recompute_setup_warning())
        self.mods_folder_var.trace_add('write', lambda *_: self._recompute_setup_warning())
        self._recompute_setup_warning()

    def _recompute_setup_warning(self):
        if not hasattr(self, "setup_warning_var"):
            return
        issues = []
        exe = (self.server_path_var.get() or "").strip()
        if not exe:
            issues.append("⚠ SERVER EXECUTABLE NOT SET")
        elif not os.path.isfile(exe):
            issues.append("⚠ EXECUTABLE PATH INVALID")
        mods = (self.mods_folder_var.get() or "").strip()
        if not mods:
            issues.append("⚠ MODS FOLDER NOT SET")
        elif not os.path.isdir(mods):
            issues.append("⚠ MODS FOLDER INVALID")
        self.setup_warning_var.set("\n".join(issues))

    def _install_wrapping_row(self, container, widgets, spacing=8, pady_between=4):
        container.pack_propagate(False)
        try:
            probe = widgets[0]
            probe.update_idletasks()
            container.configure(height=max(24, probe.winfo_reqheight() + 4))
        except Exception:
            container.configure(height=40)

        def _reflow(_event=None):
            try:
                width = container.winfo_width()
            except tk.TclError:
                return
            if width <= 1:
                return
            x = y = row_h = 0
            sizes = []
            for w in widgets:
                w.update_idletasks()
                sizes.append((w.winfo_reqwidth(), w.winfo_reqheight()))
            for w, (ww, wh) in zip(widgets, sizes):
                if x > 0 and x + ww > width:
                    x = 0
                    y += row_h + pady_between
                    row_h = 0
                w.place(x=x, y=y)
                x += ww + spacing
                if wh > row_h:
                    row_h = wh
            container.configure(height=max(24, y + row_h + 2))

        container.bind("<Configure>", _reflow)
        self.after(50, _reflow)

    # ------------------------------------------------------------------
    # Console panel
    # ------------------------------------------------------------------
    def _build_console(self, parent):
        panel = themed_frame(parent)
        panel.pack(fill=tk.BOTH, expand=True)
        inner = panel.inner
        _, self.console_status_label = panel_header(
            inner, "Server Console", right_text="Idle", font_spec=self.F_HDR)

        # Filter row
        filter_row = tk.Frame(inner, bg=Theme.BG_PANEL)
        filter_row.pack(fill=tk.X, padx=10, pady=(8, 6))
        tk.Label(filter_row, text="FILTER:", fg=Theme.AMBER_DIM,
                 bg=Theme.BG_PANEL, font=self.F_SMALL).pack(side=tk.LEFT)
        TermEntry(filter_row, textvariable=self.log_filter_var,
                  font_spec=self.F_NORMAL).pack(side=tk.LEFT, fill=tk.X,
                                                expand=True, padx=8, ipady=2)
        TermButton(filter_row, "Copy", self._copy_console_view,
                   variant="amber", font_spec=self.F_SMALL,
                   padx=8, pady=2).pack(side=tk.LEFT, padx=(0, 4))
        self.log_filter_var.trace_add('write', lambda *_: self.update_console_display())

        # Console text widget
        console_wrap = tk.Frame(inner, bg=Theme.BORDER)
        console_wrap.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        console_inner = tk.Frame(console_wrap, bg=Theme.BG_INPUT)
        console_inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self.console_text = tk.Text(
            console_inner, bg=Theme.BG_INPUT, fg=Theme.AMBER,
            insertbackground=Theme.AMBER_GLOW,
            selectbackground=Theme.BG_SELECT,
            selectforeground=Theme.AMBER_GLOW,
            font=self.F_CONSOLE, bd=0, highlightthickness=0,
            wrap=tk.WORD, state='disabled', padx=8, pady=6)
        self.console_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(console_inner, orient=tk.VERTICAL,
                           style="Term.Vertical.TScrollbar",
                           command=self.console_text.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.console_text.configure(yscrollcommand=sb.set)
        c = self.console_text
        c.tag_configure("info",      foreground=Theme.AMBER)
        c.tag_configure("warn",      foreground=Theme.AMBER_GLOW)
        c.tag_configure("error",     foreground=Theme.RED)
        c.tag_configure("success",   foreground=Theme.GREEN)
        c.tag_configure("system",    foreground=Theme.CYAN)
        c.tag_configure("player",    foreground=Theme.PURPLE)
        c.tag_configure("chat",      foreground=Theme.AMBER_GLOW)
        c.tag_configure("echo",      foreground=Theme.AMBER_DIM)
        c.tag_configure("timestamp", foreground=Theme.MUTED)

        # Command input row
        cmd_wrap = tk.Frame(inner, bg=Theme.BORDER)
        cmd_wrap.pack(fill=tk.X, padx=10, pady=(0, 10))
        cmd_inner = tk.Frame(cmd_wrap, bg=Theme.BG_INPUT)
        cmd_inner.pack(fill=tk.X, padx=1, pady=1)
        cmd_inner.columnconfigure(1, weight=1)
        self.prompt_label = tk.Label(cmd_inner, text=" ❯ ",
                                     fg=Theme.AMBER_GLOW, bg=Theme.BG_INPUT,
                                     font=self.F_HDR)
        self.prompt_label.grid(row=0, column=0, padx=(6, 0), sticky="w")
        self.command_entry = tk.Entry(
            cmd_inner, textvariable=self.command_var,
            bg=Theme.BG_INPUT, fg=Theme.AMBER_GLOW,
            insertbackground=Theme.AMBER_GLOW,
            selectbackground=Theme.BG_SELECT, selectforeground=Theme.AMBER_GLOW,
            font=self.F_NORMAL, bd=0, highlightthickness=0, width=1,
            state='disabled',
            disabledbackground=Theme.BG_INPUT, disabledforeground=Theme.AMBER_FAINT)
        self.command_entry.grid(row=0, column=1, sticky="ew", padx=4, ipady=8)
        self.command_entry.bind("<Return>", lambda e: self.send_command())
        self.btn_send = TermButton(cmd_inner, "Send", self.send_command,
                                   variant="amber", font_spec=self.F_SMALL,
                                   padx=14, pady=4)
        self.btn_send.grid(row=0, column=2, padx=4, pady=4, sticky="e")
        self.btn_send.set_enabled(False)

        # Broadcast row
        chat_wrap = tk.Frame(inner, bg=Theme.BORDER)
        chat_wrap.pack(fill=tk.X, padx=10, pady=(0, 10))
        chat_inner = tk.Frame(chat_wrap, bg=Theme.BG_INPUT)
        chat_inner.pack(fill=tk.X, padx=1, pady=1)
        chat_inner.columnconfigure(1, weight=1)
        tk.Label(chat_inner, text=" 📢 ", fg=Theme.CYAN,
                 bg=Theme.BG_INPUT, font=self.F_NORMAL
                 ).grid(row=0, column=0, padx=(6, 0), sticky="w")
        self.chat_var = tk.StringVar()

        def _do_chat(_e=None):
            msg = self.chat_var.get().strip()
            if msg and self.is_running:
                if self.broadcast(msg):
                    self.chat_var.set("")
            elif not self.is_running:
                self._notify("Server not running.", level="warn")

        self.chat_entry = tk.Entry(
            chat_inner, textvariable=self.chat_var,
            bg=Theme.BG_INPUT, fg=Theme.AMBER_GLOW,
            insertbackground=Theme.AMBER_GLOW,
            selectbackground=Theme.BG_SELECT, selectforeground=Theme.AMBER_GLOW,
            font=self.F_NORMAL, bd=0, highlightthickness=0, width=1)
        self.chat_entry.grid(row=0, column=1, sticky="ew", padx=4, ipady=6)
        self.chat_entry.bind("<Return>", _do_chat)
        self.btn_broadcast = TermButton(chat_inner, "Say", _do_chat,
                                        variant="amber", font_spec=self.F_SMALL,
                                        padx=14, pady=4)
        self.btn_broadcast.grid(row=0, column=2, padx=4, pady=4, sticky="e")

    # ------------------------------------------------------------------
    # Sidebar
    # ------------------------------------------------------------------
    def _build_sidebar(self, parent):
        side_paned = ttk.PanedWindow(parent, orient=tk.VERTICAL,
                                     style="Term.TPanedwindow")
        side_paned.pack(fill=tk.BOTH, expand=True)
        self._sidebar_paned = side_paned

        player_panel = themed_frame(side_paned)
        side_paned.add(player_panel, weight=1)
        self._build_players(player_panel.inner)

        nb_panel = themed_frame(side_paned)
        side_paned.add(nb_panel, weight=3)
        nb_bg = tk.Frame(nb_panel.inner, bg=Theme.BG_PANEL)
        nb_bg.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.notebook = ttk.Notebook(nb_bg, style="Term.TNotebook")
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # Existing tabs (stubs that delegate to methods carried forward from v2)
        for tab_name, builder in [
            ("COMMANDS",     self._build_commands_tab),
            ("SETTINGS",     self._build_settings_tab),
            ("BACKUP",       self._build_backup_tab),
            ("MODS",         self._build_mods_tab),
            ("CONFIG",       self._build_config_tab),
            ("CUSTOM THEME", self._build_custom_theme_tab),
        ]:
            frame = tk.Frame(self.notebook, bg=Theme.BG_PANEL)
            self.notebook.add(frame, text=tab_name)
            builder(frame)

        # NEW: Custom Commands tab
        custom_cmds_frame = tk.Frame(self.notebook, bg=Theme.BG_PANEL)
        self.notebook.add(custom_cmds_frame, text="CUSTOM CMDS")
        self._custom_cmds_tab = CustomCommandsTab(custom_cmds_frame, self)
        # Route every dispatch (fired or skipped) into the tab's audit
        # log. Use after_idle so audit updates always happen on the Tk
        # main thread, even when dispatch is invoked from _process_queue.
        self._cmd_dispatcher.set_audit_listener(
            lambda audit: self.after_idle(
                self._custom_cmds_tab.record_audit, audit))

        def _seed_side(retry=0):
            try:
                h = side_paned.winfo_height()
                if h > 20:
                    side_paned.sashpos(0, int(h * 0.25))
                elif retry < 20:
                    self.after(100, lambda: _seed_side(retry + 1))
            except tk.TclError:
                pass
        self.after(180, _seed_side)

    # ------------------------------------------------------------------
    # Players + resources panel
    # ------------------------------------------------------------------
    def _build_players(self, parent):
        self._players_scroll = ScrollableFrame(parent, bg=Theme.BG_PANEL)
        self._players_scroll.pack(fill=tk.BOTH, expand=True)
        host = self._players_scroll.body

        player_body = tk.Frame(host, bg=Theme.BG_PANEL)
        _, self.player_header_label = panel_header(
            host, "Player List", right_text="0 online",
            font_spec=self.F_HDR, collapsible=True, body=player_body)
        self.player_list_frame = tk.Frame(player_body, bg=Theme.BG_PANEL)
        self.player_list_frame.pack(fill=tk.X, padx=10, pady=(8, 8))
        self._render_empty_players()

        res_body = tk.Frame(host, bg=Theme.BG_PANEL)
        panel_header(host, "Resources", font_spec=self.F_HDR,
                     collapsible=True, body=res_body)
        res = tk.Frame(res_body, bg=Theme.BG_PANEL)
        res.pack(fill=tk.X, padx=10, pady=(8, 10))
        self.cpu_bar, self.cpu_label, self.cpu_fill, self.cpu_spark = \
            self._make_resource_bar(res, "CPU Usage")
        self.mem_bar, self.mem_label, self.mem_fill, self.mem_spark = \
            self._make_resource_bar(res, "Memory")
        if not PSUTIL_AVAILABLE:
            tk.Label(res, text="(psutil not installed — metrics disabled)",
                     fg=Theme.AMBER_FAINT, bg=Theme.BG_PANEL,
                     font=self.F_SMALL).pack(anchor=tk.W, pady=(4, 0))

    def _make_resource_bar(self, parent, label_text):
        row = tk.Frame(parent, bg=Theme.BG_PANEL)
        row.pack(fill=tk.X, pady=4)
        top = tk.Frame(row, bg=Theme.BG_PANEL)
        top.pack(fill=tk.X)
        tk.Label(top, text=label_text, fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
                 font=self.F_SMALL).pack(side=tk.LEFT)
        value_label = tk.Label(top, text="--", fg=Theme.AMBER_DIM,
                               bg=Theme.BG_PANEL, font=self.F_SMALL)
        value_label.pack(side=tk.RIGHT)
        bar_bg = tk.Frame(row, bg=Theme.BORDER, height=10)
        bar_bg.pack(fill=tk.X, pady=(2, 0))
        bar_inner = tk.Frame(bar_bg, bg=Theme.DIVIDER)
        bar_inner.place(relx=0, rely=0, relwidth=1, relheight=1, x=1, y=1, width=-2, height=-2)
        fill = tk.Frame(bar_inner, bg=Theme.GREEN)
        fill.place(relx=0, rely=0, relwidth=0, relheight=1)
        spark = Sparkline(row, width=180, height=32, capacity=60,
                          color=Theme.AMBER, bg=Theme.BG_INPUT)
        spark.pack(fill=tk.X, pady=(4, 0))
        return bar_inner, value_label, fill, spark

    def _set_resource_bar(self, fill_widget, label_widget, label_text, frac):
        frac = max(0.0, min(1.0, frac))
        color = Theme.GREEN if frac < 0.6 else (Theme.AMBER if frac < 0.85 else Theme.RED)
        fill_widget.configure(bg=color)
        fill_widget.place_configure(relwidth=frac)
        label_widget.configure(text=label_text, fg=color)

    def _render_empty_players(self):
        tk.Label(self.player_list_frame,
                 text="— No players connected —",
                 fg=Theme.AMBER_FAINT, bg=Theme.BG_PANEL,
                 font=self.F_NORMAL, pady=20).pack()

    def _render_player_row(self, name: str):
        row = tk.Frame(self.player_list_frame, bg=Theme.BG_PANEL)
        row.pack(fill=tk.X, pady=2)
        badge = tk.Label(row, text=name[:2].upper() if name else "??",
                         fg=Theme.AMBER_GLOW, bg=Theme.DIVIDER,
                         font=self.F_SMALL, width=3, height=1,
                         highlightthickness=1, highlightbackground=Theme.AMBER_DIM,
                         cursor="hand2")
        badge.pack(side=tk.LEFT, padx=(0, 8))
        name_lbl = tk.Label(row, text=name, fg=Theme.AMBER_GLOW,
                            bg=Theme.BG_PANEL, font=self.F_NORMAL, cursor="hand2")
        name_lbl.pack(side=tk.LEFT)

        def _copy_name(_e=None, n=name):
            try:
                self.clipboard_clear(); self.clipboard_append(n)
                self._notify(f"Copied '{n}'.", level="info", duration_ms=1500)
            except Exception:
                pass

        # Show role badge if known
        role = self._player_roles.get(name)
        if role:
            tk.Label(row, text=f"[{role}]",
                     fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
                     font=self.F_SMALL).pack(side=tk.LEFT, padx=(4, 0))

        for w in (badge, name_lbl):
            w.bind("<Button-1>", _copy_name)

        def _popup(event, n=name):
            self._show_player_menu(event, n)
        for w in (row, badge, name_lbl):
            w.bind("<Button-3>", _popup)
            w.bind("<Button-2>", _popup)

    def _show_player_menu(self, event, player_name: str):
        if not self.is_running:
            return
        m = tk.Menu(self, tearoff=0, bg=Theme.BG_PANEL, fg=Theme.AMBER,
                    activebackground=Theme.BG_SELECT,
                    activeforeground=Theme.AMBER_GLOW,
                    bd=0, font=self.F_SMALL)
        m.add_command(label=f"Copy name: {player_name}",
                      command=lambda n=player_name: self._copy_to_clipboard(n))
        m.add_separator()
        is_op = player_name in self._operators
        if is_op:
            m.add_command(label="Remove operator (de-OP)",
                          command=lambda n=player_name: self._deop_player(n))
        else:
            m.add_command(label="Make operator (OP)",
                          command=lambda n=player_name: self._op_player(n))
        m.add_separator()
        m.add_command(label="Kick...",
                      command=lambda n=player_name: self._prompt_and_kick(n))
        # Improvement #14: ban confirmation dialog
        m.add_command(label="Ban...",
                      command=lambda n=player_name: self._prompt_and_ban(n))
        m.add_command(label="Teleport to...",
                      command=lambda n=player_name: self._teleport_to(n))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _op_player(self, name):
        if self._run_admin_cmd(f"/op {name}"):
            self._operators.add(name)

    def _deop_player(self, name):
        if self._run_admin_cmd(f"/player {name} role suplayer"):
            self._operators.discard(name)

    def _run_admin_cmd(self, cmd):
        if self._send_internal_command(cmd):
            self.append_console(f"❯ {cmd}", "echo")
            return True
        self.append_console(f"Could not send: {cmd}", "error")
        return False

    def _copy_to_clipboard(self, text):
        try:
            self.clipboard_clear(); self.clipboard_append(text)
            self._notify(f"Copied '{text}'.", level="info", duration_ms=1500)
        except Exception:
            pass

    def _prompt_and_kick(self, name):
        from tkinter import simpledialog
        reason = simpledialog.askstring(
            "Kick Player", f"Reason for kicking {name}?", parent=self)
        if reason is None:
            return
        self._send_internal_command(f"/kick {name} {reason.strip()}")
        self.append_console(f"❯ /kick {name} {reason.strip()}", "echo")

    def _prompt_and_ban(self, name):
        """Ban with explicit confirmation dialog (improvement #14)."""
        if not messagebox.askyesno(
                "Confirm Ban",
                f"Are you sure you want to BAN {name}?\nThis cannot be undone from VSSM.",
                icon="warning", parent=self):
            return
        from tkinter import simpledialog
        reason = simpledialog.askstring(
            "Ban Player", f"Reason for banning {name}?", parent=self)
        if reason is None:
            return
        self._send_internal_command(f"/ban {name} {reason.strip()}")
        self.append_console(f"❯ /ban {name} {reason.strip()}", "echo")

    def _teleport_to(self, name):
        self._run_admin_cmd(f"/tp {name}")

    def _prompt_and_run(self, title, prompt, cmd_builder):
        from tkinter import simpledialog
        reason = simpledialog.askstring(title, prompt, parent=self)
        if reason is None:
            return
        cmd = cmd_builder(reason.strip())
        self._send_internal_command(cmd)
        self.append_console(f"❯ {cmd}", "echo")

    # ------------------------------------------------------------------
    # Console right-click (improvement #10)
    # ------------------------------------------------------------------
    def _console_right_click(self, event):
        try:
            idx = self.console_text.index(f"@{event.x},{event.y}")
            line_start = idx.split('.')[0] + ".0"
            line_end   = idx.split('.')[0] + ".end"
            line_text  = self.console_text.get(line_start, line_end).strip()
        except Exception:
            return
        m = tk.Menu(self, tearoff=0, bg=Theme.BG_PANEL, fg=Theme.AMBER,
                    activebackground=Theme.BG_SELECT,
                    activeforeground=Theme.AMBER_GLOW,
                    bd=0, font=self.F_SMALL)
        m.add_command(label="Copy this line",
                      command=lambda t=line_text: self._copy_to_clipboard(t))
        m.add_command(label="Copy all visible",
                      command=self._copy_console_view)
        m.add_separator()
        m.add_command(label="Clear console", command=self.clear_console)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    # ------------------------------------------------------------------
    # Placeholder tab builders (forward-compat stubs)
    # These carry the full v2 tab builder methods. For brevity in this
    # module, they are listed as stubs; the full implementation is
    # identical to v2 (see Vintage_Story_Server_Manager.py for reference).
    # In a real split each would live in ui/tab_*.py.
    # ------------------------------------------------------------------
    def _build_commands_tab(self, parent):
        from ui.tab_commands import build_commands_tab
        build_commands_tab(parent, self)

    def _build_settings_tab(self, parent):
        from ui.tab_settings import build_settings_tab
        build_settings_tab(parent, self)

    def _build_backup_tab(self, parent):
        from ui.tab_backup import build_backup_tab
        build_backup_tab(parent, self)

    def _build_config_tab(self, parent):
        from ui.tab_config import build_config_tab
        build_config_tab(parent, self)

    def _build_custom_theme_tab(self, parent):
        from ui.tab_custom_theme import build_custom_theme_tab
        build_custom_theme_tab(parent, self)

    def _refresh_commands_tree(self):
        query = self.cmd_search_var.get().strip().lower()
        self._cmd_index.clear()
        self._cmd_cat_rows.clear()
        self._cmd_selected_row = None
        self.cmd_tree.configure(state='normal')
        self.cmd_tree.delete("1.0", tk.END)
        for category, cmds in self.commands_data.items():
            matching = [(n, e) for n, e in cmds.items()
                        if not query or query in (n + " " + (e.get("description","") if isinstance(e,dict) else "")).lower()]
            if not matching:
                continue
            cat_row_num = int(self.cmd_tree.index("end-1c").split('.')[0])
            collapsed = (category in self._cmd_collapsed_cats) and not query
            arrow = "▸" if collapsed else "▾"
            self.cmd_tree.insert(tk.END, f"{arrow} {category}  ({len(matching)})\n", ("category",))
            self._cmd_cat_rows[cat_row_num] = category
            if collapsed:
                continue
            for name, _entry in matching:
                row_num = int(self.cmd_tree.index("end-1c").split('.')[0])
                has_args = isinstance(_entry, dict) and bool(_entry.get("args"))
                self.cmd_tree.insert(tk.END, f"    {name}{'  ◆' if has_args else ''}\n", ("cmd",))
                self._cmd_index[row_num] = (category, name)
        if not self._cmd_index and not self._cmd_cat_rows:
            self.cmd_tree.insert(tk.END, "\n    (no commands match)\n", ("category",))
        self.cmd_tree.configure(state='disabled')

    def _cmd_row_from_event(self, event):
        idx = self.cmd_tree.index(f"@{event.x},{event.y}")
        return int(idx.split('.')[0])

    def _on_cmd_category_click(self, event):
        row = self._cmd_row_from_event(event)
        cat = self._cmd_cat_rows.get(row)
        if not cat:
            return
        if cat in self._cmd_collapsed_cats:
            self._cmd_collapsed_cats.discard(cat)
        else:
            self._cmd_collapsed_cats.add(cat)
        self._refresh_commands_tree()

    def _on_cmd_click(self, event):
        row = self._cmd_row_from_event(event)
        info = self._cmd_index.get(row)
        if not info:
            return
        self._cmd_selected_row = row
        cat, name = info
        entry = self.commands_data.get(cat, {}).get(name)
        self._render_cmd_details(cat, name, entry)

    def _on_cmd_dbl_click(self, event):
        row = self._cmd_row_from_event(event)
        if self._cmd_index.get(row):
            self._cmd_selected_row = row
            self._insert_selected_command()

    def _current_command(self):
        row = self._cmd_selected_row
        if row is None:
            return None, None, None
        info = self._cmd_index.get(row)
        if not info:
            return None, None, None
        cat, name = info
        entry = self.commands_data.get(cat, {}).get(name)
        if not isinstance(entry, dict):
            return None, None, None
        return cat, name, entry

    def _render_cmd_details(self, category, name, entry):
        self.cmd_details.configure(state='normal')
        self.cmd_details.delete("1.0", tk.END)
        if name and isinstance(entry, dict):
            self.cmd_details.insert(tk.END, f"{name}\n", ("title",))
            self.cmd_details.insert(tk.END, f"{category}\n\n", ("cat",))
            self.cmd_details.insert(tk.END,
                entry.get("description") or "(No description)", ("body",))
        else:
            self.cmd_details.insert(tk.END,
                "Select a command to view details.", ("cat",))
        self.cmd_details.configure(state='disabled')
        self._build_arg_inspector(entry if isinstance(entry, dict) else None)

    def _build_arg_inspector(self, entry):
        for child in self.cmd_arg_host.body.winfo_children():
            child.destroy()
        self._cmd_arg_vars.clear()
        if entry is None:
            return
        args     = entry.get("args") or []
        template = entry.get("template", "")
        if not args:
            prev = tk.Frame(self.cmd_arg_host.body, bg=Theme.BG_HEADER)
            prev.pack(fill=tk.X)
            tk.Label(prev, text="▸ READY:", fg=Theme.AMBER_DIM,
                     bg=Theme.BG_HEADER, font=self.F_SMALL,
                     padx=10, pady=6).pack(side=tk.LEFT)
            tk.Label(prev, text=template, fg=Theme.AMBER_GLOW,
                     bg=Theme.BG_HEADER, font=self.F_NORMAL, padx=4, pady=6
                     ).pack(side=tk.LEFT)
            self.cmd_preview_var.set(template)
            return
        form = tk.Frame(self.cmd_arg_host.body, bg=Theme.BG_PANEL)
        form.pack(fill=tk.X)
        form.columnconfigure(1, weight=1)
        for i, arg in enumerate(args):
            aname   = arg["name"]
            atype   = arg.get("type", "text")
            optional = arg.get("optional", False)
            default = arg.get("default", "")
            choices = arg.get("choices") or []
            hint    = arg.get("hint", "")
            label_text = aname + (" " if optional else "*")
            if hint:
                label_text += f"  ({hint})"
            tk.Label(form, text=label_text,
                     fg=Theme.AMBER_DIM if optional else Theme.AMBER,
                     bg=Theme.BG_PANEL, font=self.F_SMALL, anchor=tk.W
                     ).grid(row=i, column=0, sticky=tk.W, padx=(0, 8), pady=2)
            var = tk.StringVar(value=str(default))
            var.trace_add("write", lambda *_: self._update_cmd_preview())
            self._cmd_arg_vars[aname] = var
            if atype == "choice" and choices:
                widget = ttk.Combobox(form, textvariable=var, values=choices,
                                      state="readonly", style="Term.TCombobox",
                                      font=self.F_NORMAL)
            elif atype == "player":
                widget = ttk.Combobox(form, textvariable=var,
                                      values=list(self._players),
                                      style="Term.TCombobox", font=self.F_NORMAL)
            else:
                widget = TermEntry(form, textvariable=var, font_spec=self.F_NORMAL)
            widget.grid(row=i, column=1, sticky=tk.EW, pady=2, ipady=2)
        preview_wrap = tk.Frame(self.cmd_arg_host.body, bg=Theme.BG_HEADER)
        preview_wrap.pack(fill=tk.X, pady=(6, 0))
        tk.Label(preview_wrap, text="▸ PREVIEW:", fg=Theme.AMBER_DIM,
                 bg=Theme.BG_HEADER, font=self.F_SMALL,
                 padx=10, pady=6).pack(side=tk.LEFT)
        tk.Label(preview_wrap, textvariable=self.cmd_preview_var,
                 fg=Theme.AMBER_GLOW, bg=Theme.BG_HEADER,
                 font=self.F_NORMAL, padx=4, pady=6, anchor=tk.W
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._update_cmd_preview()

    def _update_cmd_preview(self):
        _, _, entry = self._current_command()
        if not entry:
            self.cmd_preview_var.set("")
            return
        try:
            self.cmd_preview_var.set(self._assemble_command(entry))
        except Exception as e:
            self.cmd_preview_var.set(f"(error: {e})")

    def _assemble_command(self, entry) -> str:
        template = entry.get("template", "")
        result   = template
        for a in (entry.get("args") or []):
            aname = a["name"]
            var   = self._cmd_arg_vars.get(aname)
            value = var.get().strip() if var else ""
            ph    = "{" + aname + "}"
            if value == "" and a.get("optional"):
                result = result.replace(" " + ph, "").replace(ph, "")
            else:
                result = result.replace(ph, value)
        return result

    def _insert_selected_command(self):
        _, _, entry = self._current_command()
        if not entry:
            return
        cmd = self._assemble_command(entry)
        if "{" in cmd:
            self._notify("Fill in required arguments first.", level="warn")
            return
        self.command_var.set(cmd)
        try:
            self.command_entry.focus_set()
            self.command_entry.icursor(tk.END)
        except Exception:
            pass

    def _send_selected_command(self):
        _, _, entry = self._current_command()
        if not entry:
            return
        cmd = self._assemble_command(entry)
        if "{" in cmd:
            self._notify("Fill in required arguments first.", level="warn")
            return
        if not self.is_running:
            self._notify("Server not running.", level="warn")
            return
        if self._send_internal_command(cmd):
            self.append_console(f"❯ {cmd}", "echo")

    def _reload_commands_json(self):
        try:
            new_data = load_commands_data()
        except Exception as e:
            self._notify(f"Reload failed: {e}", level="error")
            return
        self.commands_data = new_data
        self._cmd_selected_row = None
        self._refresh_commands_tree()
        total = sum(len(v) for v in new_data.values())
        self.cmd_count_var.set(f"{total} commands")
        self._notify(f"Reloaded — {total} commands", level="success")

    # ------------------------------------------------------------------
    # Settings helpers
    # ------------------------------------------------------------------
    def _validate_cron_live(self):
        """Improvement #16: parse cron expression live and show status."""
        expr = self.cron_expr_var.get().strip()
        if not expr:
            self._cron_status_var.set("")
            return
        try:
            entries = parse_cron_expr(expr)
            secs = seconds_until_next(entries)
            mins = secs // 60
            self._cron_status_var.set(f"✓ next in ~{mins}m")
            try:
                self._cron_status_label_ref.configure(fg=Theme.GREEN)
            except Exception:
                pass
        except ValueError as e:
            self._cron_status_var.set(f"✗ {e}")
            try:
                self._cron_status_label_ref.configure(fg=Theme.RED)
            except Exception:
                pass

    def _on_theme_change(self):
        preset = self.theme_preset_var.get()
        Theme.apply_preset(preset)
        if preset == "custom":
            Theme.load_custom_colors(self._settings.get("custom_theme_colors", {}))
        self._settings["theme_preset"] = preset
        save_settings(self._settings)
        self._notify("Theme applied — restart for full effect.", level="info")

    def _save_custom_colors(self):
        colors = {}
        for key, var in self._custom_color_vars.items():
            val = var.get().strip()
            if val:
                colors[key] = val
        self._settings["custom_theme_colors"] = colors
        save_settings(self._settings)
        self._notify("Custom colors saved — restart to apply.", level="success")

    def _save_profile_settings(self):
        profile = get_active_profile(self._settings)
        profile["server_path"]       = self.server_path_var.get()
        profile["mods_folder"]       = self.mods_folder_var.get()
        profile["world_folder"]      = self.world_folder_var.get()
        profile["backup_dir"]        = self.backup_dir_var.get()
        profile["max_backups"]       = self.max_backups_var.get()
        profile["autorestart"]       = self.autorestart_var.get()
        profile["autosave_enabled"]  = self.autosave_enabled_var.get()
        profile["autosave_interval"] = self.autosave_interval_var.get()
        profile["autosave_cmd"]      = self.autosave_cmd_var.get()
        profile["cron_expr"]         = self.cron_expr_var.get()
        profile["shutdown_timeout"]  = self.shutdown_timeout_var.get()
        profile["backup_before_start"] = self.backup_before_start_var.get()
        profile["backup_before_stop"]  = self.backup_before_stop_var.get()
        # Crash-loop config (improvement #15)
        try:
            self.CRASH_LIMIT = int(self._crash_limit_var.get())
            self.CRASH_WINDOW_SECS = int(self._crash_window_var.get())
            self._settings["crash_limit"]       = self.CRASH_LIMIT
            self._settings["crash_window_secs"] = self.CRASH_WINDOW_SECS
        except (ValueError, AttributeError):
            pass
        # Player-count poll interval — 0 disables, otherwise seconds.
        try:
            secs = int(self._player_poll_var.get())
            if secs < 0:
                secs = 0
            self._settings["player_count_poll_secs"] = secs
        except (ValueError, AttributeError):
            pass
        save_settings(self._settings)
        self._notify("Settings saved.", level="success", duration_ms=1800)
        self._apply_cron_schedule()
        # Reschedule the player-count poller in case the interval changed.
        self._reschedule_player_count_poll()

    def _apply_default_paths(self):
        profile = get_active_profile(self._settings)
        self.server_path_var.set(profile.get("server_path", ""))
        self.mods_folder_var.set(profile.get("mods_folder", ""))
        self.world_folder_var.set(profile.get("world_folder", ""))
        self.backup_dir_var.set(profile.get("backup_dir", ""))
        self.max_backups_var.set(profile.get("max_backups", "10"))
        self.autorestart_var.set(profile.get("autorestart", False))
        self.autosave_enabled_var.set(profile.get("autosave_enabled", False))
        self.autosave_interval_var.set(profile.get("autosave_interval", "30"))
        self.autosave_cmd_var.set(profile.get("autosave_cmd", True))
        self.cron_expr_var.set(profile.get("cron_expr", ""))
        self.shutdown_timeout_var.set(profile.get("shutdown_timeout", "30"))
        self.backup_before_start_var.set(profile.get("backup_before_start", False))
        self.backup_before_stop_var.set(profile.get("backup_before_stop", False))
        srv = profile.get("server_path", "")
        if srv:
            self.server_path_var.set(srv)

    # ------------------------------------------------------------------
    # Browse helpers
    # ------------------------------------------------------------------
    def browse_executable(self):
        path = filedialog.askopenfilename(
            title="Select VS Server Executable",
            filetypes=[("Executables", "*.exe *.sh *.bat *"), ("All", "*.*")])
        if path:
            self.server_path_var.set(path)

    def browse_mods_folder(self):
        d = filedialog.askdirectory(title="Select Mods Folder")
        if d:
            self.mods_folder_var.set(d)

    def open_mods_folder(self):
        """Reveal the configured Mods folder in the OS file manager."""
        self._reveal_folder(self.mods_folder_var.get(), "Mods")

    def browse_world_folder(self):
        d = filedialog.askdirectory(title="Select World Folder")
        if d:
            self.world_folder_var.set(d)

    def open_world_folder(self):
        """Reveal the configured World folder in the OS file manager."""
        self._reveal_folder(self.world_folder_var.get(), "World")

    def browse_backup_folder(self):
        d = filedialog.askdirectory(title="Select Backup Destination")
        if d:
            self.backup_dir_var.set(d)

    def open_backup_folder(self):
        """Reveal the configured Backup destination in the OS file manager."""
        self._reveal_folder(self.backup_dir_var.get(), "Backup destination")

    def _reveal_folder(self, folder: str, label: str) -> None:
        if not folder:
            self._notify(f"No {label} folder configured yet.", level="warn")
            return
        if not os.path.isdir(folder):
            self._notify(f"{label} folder does not exist: {folder}",
                         level="error")
            return
        if not open_in_file_manager(folder):
            self._notify("Could not open file manager — "
                         "see logs/vserverman.log.",
                         level="error")

    def open_logs_folder(self):
        """Reveal VSSM's logs/ folder in the OS file manager."""
        from core.constants import log_dir
        d = log_dir()
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        if not open_in_file_manager(d):
            self._notify("Could not open file manager.",
                         level="error")

    def clear_old_logs(self):
        """Delete every file in logs/ except the two currently-active
        log files. Rotation backups (.log.1, .log.2, …) and any stray
        files (e.g. old crash dumps) are removed.

        The active files (`vserverman.log` and `server-output.log`)
        cannot be deleted while the app is writing to them — we leave
        them alone."""
        from core.constants import log_dir
        d = log_dir()
        if not os.path.isdir(d):
            self._notify("logs folder does not exist.", level="warn")
            return
        # Files we DO NOT touch.
        keep = {"vserverman.log", "server-output.log"}
        # Find candidates first so we can show the user what we'd delete.
        candidates = []
        try:
            for name in os.listdir(d):
                if name in keep:
                    continue
                full = os.path.join(d, name)
                if os.path.isfile(full):
                    try:
                        candidates.append((full, os.path.getsize(full)))
                    except OSError:
                        candidates.append((full, 0))
        except OSError as e:
            self._notify(f"Could not list logs/: {e}", level="error")
            return
        if not candidates:
            self._notify("No old log files to clear.", level="info")
            return
        from core.utils import fmt_size
        total = sum(s for _, s in candidates)
        if not messagebox.askyesno(
                "Clear old logs",
                f"Delete {len(candidates)} file(s) from logs/ "
                f"({fmt_size(total)} total)?\n\n"
                f"The active vserverman.log and server-output.log "
                f"will be kept.\nThis cannot be undone.",
                parent=self):
            return
        deleted = 0
        for full, _ in candidates:
            try:
                os.remove(full)
                deleted += 1
            except OSError as e:
                LOG.warning("could not delete %s: %s", full, e)
        self._notify(f"Cleared {deleted} log file(s).",
                     level="success", duration_ms=2500)
        LOG.info("Cleared %d old log file(s) from %s", deleted, d)

    # ------------------------------------------------------------------
    # Config tab helpers
    # ------------------------------------------------------------------
    def open_config_file(self):
        path = filedialog.askopenfilename(
            title="Open Config File",
            filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            self.config_text.delete("1.0", tk.END)
            self.config_text.insert("1.0", content)
            self.config_file_path = path
            self._notify(f"Opened: {os.path.basename(path)}", level="success")
        except Exception as e:
            self._notify(f"Could not open: {e}", level="error")

    def save_config_file(self):
        if not self.config_file_path:
            self._notify("No file open.", level="warn")
            return
        try:
            content = self.config_text.get("1.0", tk.END)
            with open(self.config_file_path, "w", encoding="utf-8") as f:
                f.write(content)
            self._notify("Config saved.", level="success")
        except Exception as e:
            self._notify(f"Save failed: {e}", level="error")

    # ------------------------------------------------------------------
    # Server control
    # ------------------------------------------------------------------
    def start_server(self):
        if self.is_running:
            self._notify("Server is already running.", level="warn")
            return
        if self._shutdown_in_progress:
            self._notify("Still stopping — please wait.", level="warn")
            return
        exe = self.server_path_var.get().strip()
        if not exe or not os.path.isfile(exe):
            self._notify("Server executable not set or invalid.", level="error")
            return
        if self.backup_before_start_var.get():
            self._start_async_backup(silent=True, reason="pre-start")
        server_dir = os.path.dirname(exe)
        port = find_vs_port(server_dir)
        if not is_port_free(port):
            self.append_console(
                f"WARNING: Port {port} is already in use. Server may fail to bind.",
                "warn")
        try:
            self.server_process = subprocess.Popen(
                [exe], cwd=server_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )
        except Exception as e:
            self._notify(f"Failed to start: {e}", level="error")
            self.append_console(f"Start failed: {e}", "error")
            LOG.error("start_server failed: %s", e)
            return
        self.is_running  = True
        self.start_time  = time.time()
        self._update_buttons_running(True)
        self._set_status("ONLINE", dot="online")
        self.append_console(f"Server started: {exe}", "success")
        LOG.info("Server started: %s", exe)
        reader = threading.Thread(target=self._read_output, daemon=True)
        reader.start()
        self.after(100, self._process_queue)
        self._schedule_autosave()
        self._apply_cron_schedule()

    def stop_server(self, on_done: Callable | None = None):
        if not self.is_running and not self.server_process:
            if on_done:
                on_done()
            return
        if self._shutdown_in_progress:
            if on_done:
                self._shutdown_callbacks.append(on_done)
            return
        if on_done:
            self._shutdown_callbacks.append(on_done)
        if self.backup_before_stop_var.get():
            self._start_async_backup(silent=True, reason="pre-stop")
        self._shutdown_in_progress = True
        self._update_buttons_running(False, shutting_down=True)
        self._set_status("STOPPING", dot="stopping")
        self.append_console("Stopping server…", "system")
        if self._send_internal_command("/stop"):
            self.append_console("Sent /stop command.", "system")
        timeout = 30
        try:
            timeout = max(5, min(300, int(self.shutdown_timeout_var.get())))
        except ValueError:
            pass

        def _poll_exit(deadline):
            proc = self.server_process
            if proc is None:
                self._finalize_stop()
                return
            if proc.poll() is not None:
                self._finalize_stop()
                return
            if time.time() > deadline:
                try:
                    proc.terminate()
                    self.append_console("Sent SIGTERM.", "warn")
                except Exception:
                    pass
                self.after(3000, lambda: self._force_kill_and_finalize(proc))
                return
            self.after(500, lambda: _poll_exit(deadline))

        _poll_exit(time.time() + timeout)

    def _force_kill_and_finalize(self, proc):
        try:
            proc.kill()
        except Exception:
            pass
        self._finalize_stop()

    def _finalize_stop(self):
        self._shutdown_in_progress = False
        self.is_running = False
        self.start_time = None
        self._update_buttons_running(False)
        self._set_status("OFFLINE", dot="off")
        self.append_console("Server stopped.", "system")
        self._players = []
        self._rerender_players()
        self._operators.clear()
        self._pending_role_query.clear()
        self._player_roles.clear()
        self.server_process = None
        self.cancel_autosave_job()
        self._cancel_cron_schedule()
        cbs = self._shutdown_callbacks[:]
        self._shutdown_callbacks.clear()
        for cb in cbs:
            try:
                cb()
            except Exception:
                pass

    def restart_server(self):
        if not self.is_running:
            self._notify("Server is not running.", level="warn")
            return
        self.append_console("Restarting server…", "system")
        self.stop_server(on_done=lambda: self.after(2000, self.start_server))

    def _update_buttons_running(self, running: bool, shutting_down: bool = False):
        if shutting_down:
            for btn in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_send):
                btn.set_enabled(False)
            self.command_entry.configure(state='disabled')
            return
        self.btn_start.set_enabled(not running)
        self.btn_stop.set_enabled(running)
        self.btn_restart.set_enabled(running)
        self.btn_send.set_enabled(running)
        self.command_entry.configure(state='normal' if running else 'disabled')
        if running:
            # When the server transitions to running, give the command
            # entry keyboard focus so the user can immediately type. Use
            # after_idle so this runs after Tk has finished updating
            # button-state visuals — calling focus_set on a widget that
            # was disabled until microseconds ago is sometimes ignored.
            try:
                self.after_idle(self._focus_command_entry)
            except Exception:
                pass

    def _focus_command_entry(self):
        """Move keyboard focus to the server-command entry. Bound to
        Ctrl+/ globally so the user can always get back to it without
        clicking, e.g. after navigating mod pages."""
        try:
            if str(self.command_entry.cget('state')) == 'normal':
                self.command_entry.focus_set()
                self.command_entry.icursor(tk.END)
        except Exception:
            pass

    def _set_status(self, status_text: str, dot: str = "off"):
        self.status_var.set(f"({status_text})")
        if dot == "online":
            color, border, label, lcolor = Theme.GREEN, Theme.GREEN, "Running", Theme.GREEN_DIM
        elif dot == "stopping":
            color, border, label, lcolor = Theme.AMBER, Theme.AMBER, "Stopping", Theme.AMBER_DIM
        else:
            color, border, label, lcolor = Theme.DOT_OFF, Theme.MUTED, "Idle", Theme.AMBER_DIM
        self.status_dot.itemconfigure(self._dot_id, fill=color, outline=border)
        self.console_status_label.configure(text=label, fg=lcolor)

    # ------------------------------------------------------------------
    # Output handling
    # ------------------------------------------------------------------
    def _read_output(self):
        proc = self.server_process
        try:
            stdout = proc.stdout
            while True:
                line = stdout.readline()
                if not line:
                    break
                try:
                    text = line.decode("utf-8", errors="replace")
                except Exception:
                    text = repr(line)
                self.output_queue.put(text)
        except Exception as e:
            LOG.debug("Reader thread ending: %s", e)
        self.output_queue.put(None)

    def _process_queue(self):
        try:
            try:
                while True:
                    item = self.output_queue.get_nowait()
                    if item is None:
                        self._on_process_exit_unexpected()
                        return
                    if (isinstance(item, tuple) and len(item) == 3
                            and item[0] == "__system__"):
                        _, msg, tag = item
                        try:
                            self.append_console(msg, tag)
                        except Exception:
                            LOG.exception("system msg append failed")
                        continue
                    try:
                        self._handle_server_line(item)
                    except Exception:
                        LOG.exception("handler failed on: %r", item)
            except queue.Empty:
                pass
        except Exception:
            LOG.exception("_process_queue outer")
        if self.is_running:
            self.after(100, self._process_queue)

    def _handle_server_line(self, raw: str):
        stripped = raw.rstrip('\n').rstrip('\r')
        try:
            tag = classify_line(stripped)
        except Exception:
            tag = "info"
        try:
            self.append_console(stripped, tag)
        except Exception:
            pass
        try:
            self._parse_player_event(stripped)
        except Exception:
            pass
        try:
            role = parse_role_response(stripped)
            if role and self._pending_role_query:
                who = self._pending_role_query.popleft()
                self._player_roles[who] = role
                if role in OPERATOR_ROLES:
                    self._operators.add(who)
                else:
                    self._operators.discard(who)
        except Exception:
            pass
        # NEW: custom chat command dispatch
        try:
            if tag == "chat":
                player, message = parse_chat_message(stripped)
                if player and message:
                    role = self._player_roles.get(player, "suplayer")
                    cmds = self._cmd_dispatcher.dispatch(player, role, message)
                    for cmd in cmds:
                        self._send_internal_command(cmd)
                        self.append_console(
                            f"[custom cmd] {player} → {cmd}", "system")
                        LOG.info("custom_cmd  player=%s role=%s msg=%r → %s",
                                 player, role, message, cmd)
        except Exception:
            LOG.exception("custom cmd dispatch failed")
        try:
            SERVER_LOG.info(stripped)
        except Exception:
            pass

    def _on_process_exit_unexpected(self):
        was_stopping = self._shutdown_in_progress
        self.is_running = False
        self.start_time = None
        self.cancel_autosave_job()
        self._cancel_cron_schedule()
        if self.scheduled_restart_id:
            try:
                self.after_cancel(self.scheduled_restart_id)
            except Exception:
                pass
            self.scheduled_restart_id = None
        if was_stopping:
            return
        self._update_buttons_running(False)
        self._set_status("OFFLINE", dot="off")
        self.append_console("Server process exited.", "warn")
        self._record_crash()
        self._players = []
        self._rerender_players()
        self._operators.clear()
        self._pending_role_query.clear()
        self._player_roles.clear()
        self.server_process = None
        if self.autorestart_var.get():
            if self._crash_loop_tripped():
                msg = (f"Auto-restart disabled: {self.CRASH_LIMIT}+ crashes "
                       f"in {self.CRASH_WINDOW_SECS}s. Fix root cause and restart manually.")
                self.append_console(msg, "error")
                self._notify(msg, level="error", duration_ms=8000)
                return
            self.append_console("Auto-restart enabled, restarting in 5s.", "system")
            self.after(5000, self.start_server)

    def _record_crash(self):
        now = time.time()
        self._crash_times.append(now)
        cutoff = now - self.CRASH_WINDOW_SECS
        while self._crash_times and self._crash_times[0] < cutoff:
            self._crash_times.popleft()

    def _crash_loop_tripped(self) -> bool:
        return len(self._crash_times) >= self.CRASH_LIMIT

    def _send_internal_command(self, cmd: str) -> bool:
        proc = self.server_process
        if not proc or proc.poll() is not None:
            return False
        try:
            proc.stdin.write((cmd + "\n").encode("utf-8"))
            proc.stdin.flush()
            LOG.debug("Sent: %r", cmd)
            return True
        except Exception as e:
            LOG.error("_send_internal_command failed: %s (cmd=%r)", e, cmd)
            return False

    # ------------------------------------------------------------------
    # Console
    # ------------------------------------------------------------------
    def append_console(self, text: str, tag: str = "info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.all_output_lines.append((timestamp, text, tag))
        filt = self.log_filter_var.get().lower()
        if filt and filt not in text.lower():
            return
        self._append_to_console_widget(timestamp, text, tag)

    def _append_to_console_widget(self, timestamp, text, tag):
        self.console_text.configure(state='normal')
        self.console_text.insert(tk.END, f"[{timestamp}] ", ("timestamp",))
        self.console_text.insert(tk.END, text + "\n", (tag,))
        self.console_text.see(tk.END)
        self.console_text.configure(state='disabled')

    def update_console_display(self):
        filt = self.log_filter_var.get().lower()
        self.console_text.configure(state='normal')
        self.console_text.delete("1.0", tk.END)
        for ts, text, tag in self.all_output_lines:
            if filt and filt not in text.lower():
                continue
            self.console_text.insert(tk.END, f"[{ts}] ", ("timestamp",))
            self.console_text.insert(tk.END, text + "\n", (tag,))
        self.console_text.see(tk.END)
        self.console_text.configure(state='disabled')

    def clear_console(self):
        self.all_output_lines.clear()
        self.console_text.configure(state='normal')
        self.console_text.delete("1.0", tk.END)
        self.console_text.configure(state='disabled')
        self.append_console("Console cleared.", "system")

    def _copy_console_view(self):
        filt = self.log_filter_var.get().lower()
        lines = [f"[{ts}] {text}" for ts, text, _ in self.all_output_lines
                 if not filt or filt in text.lower()]
        if not lines:
            self._notify("Nothing to copy.", level="info", duration_ms=1200)
            return
        try:
            self.clipboard_clear()
            self.clipboard_append("\n".join(lines))
            self._notify(f"Copied {len(lines)} lines.", level="success", duration_ms=1800)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Player tracking
    # ------------------------------------------------------------------
    def _parse_player_event(self, line: str):
        event, payload = parse_player_event(line)
        if event == "join":
            self._add_player(payload)
        elif event == "leave":
            self._remove_player(payload)
        elif event == "list":
            if not payload or payload.lower() in ("none", "no one", "-"):
                self._sync_players_from_list([])
            else:
                names = split_client_list(payload)
                if names:
                    self._sync_players_from_list(names)

    def _sync_players_from_list(self, names: list):
        new_set = list(dict.fromkeys(names))
        if new_set == self._players:
            return
        self._players = new_set
        self._rerender_players()

    def _add_player(self, name: str):
        if name in self._players:
            return
        self._players.append(name)
        self._rerender_players()
        def _fire(n=name):
            if self._send_internal_command(f"/player {n} role"):
                self._pending_role_query.append(n)
        self.after(2000, _fire)

    def _remove_player(self, name: str):
        if name not in self._players:
            return
        self._players.remove(name)
        self._operators.discard(name)
        self._player_roles.pop(name, None)
        self._rerender_players()

    def _rerender_players(self):
        for child in list(self.player_list_frame.winfo_children()):
            child.destroy()
        if not self._players:
            self._render_empty_players()
        else:
            for name in self._players:
                self._render_player_row(name)
        self._update_player_count()

    def _update_player_count(self):
        n = len(self._players)
        self.player_count_var.set(str(n))
        if hasattr(self, "player_header_label") and self.player_header_label:
            self.player_header_label.configure(text=f"{n} online")

    # ------------------------------------------------------------------
    # Command sending
    # ------------------------------------------------------------------
    def send_command(self):
        cmd = self.command_var.get().strip()
        if not cmd or not self.is_running:
            return
        if self._send_internal_command(cmd):
            self.append_console(f"❯ {cmd}", "echo")
            if not self._cmd_history or self._cmd_history[-1] != cmd:
                self._cmd_history.append(cmd)
                if len(self._cmd_history) > 200:
                    del self._cmd_history[0]
            self._cmd_history_pos = len(self._cmd_history)
            self.command_var.set('')
        else:
            self._notify("Failed to send command.", level="error")

    def _cmd_history_prev(self, _event=None):
        if not self._cmd_history:
            return "break"
        self._cmd_history_pos = max(0, self._cmd_history_pos - 1)
        self.command_var.set(self._cmd_history[self._cmd_history_pos])
        try:
            self.command_entry.icursor(tk.END)
        except Exception:
            pass
        return "break"

    def _cmd_history_next(self, _event=None):
        if not self._cmd_history:
            return "break"
        self._cmd_history_pos = min(len(self._cmd_history), self._cmd_history_pos + 1)
        if self._cmd_history_pos >= len(self._cmd_history):
            self.command_var.set('')
        else:
            self.command_var.set(self._cmd_history[self._cmd_history_pos])
        try:
            self.command_entry.icursor(tk.END)
        except Exception:
            pass
        return "break"

    def broadcast(self, message: str) -> bool:
        if not self.is_running:
            return False
        ok = self._send_internal_command(f"/announce {message}")
        if ok:
            self.append_console(f"📢 {message}", "system")
        return ok

    # ------------------------------------------------------------------
    # Backup — delegates to BackupManager (improvement #2)
    # ------------------------------------------------------------------
    # Host-protocol accessors used by BackupManager
    def get_world_folder(self) -> str:
        return self.world_folder_var.get()

    def get_backup_dir(self) -> str:
        return self.backup_dir_var.get()

    def get_max_backups(self) -> int:
        try:
            return int(self.max_backups_var.get())
        except (ValueError, TypeError):
            return 0

    def get_retention_mode(self) -> str:
        var = getattr(self, "_retention_mode_var", None)
        if var is not None:
            try:
                return var.get() or "count"
            except Exception:
                pass
        return "count"

    def get_autosave_cmd_enabled(self) -> bool:
        try:
            return bool(self.autosave_cmd_var.get())
        except Exception:
            return False

    # Thin shims so the existing tab buttons + auto-save / cron paths
    # keep their old call shapes.
    def backup_world(self, silent: bool = False):
        return self._backup_manager.backup_world(silent=silent)

    def _start_async_backup(self, dst=None, silent: bool = False,
                             reason: str = "manual") -> None:
        self._backup_manager.start_async_backup(dst=dst, silent=silent,
                                                  reason=reason)

    def cancel_active_backup(self) -> None:
        self._backup_manager.cancel_active_backup()

    def prune_old_backups(self, announce: bool = True) -> None:
        self._backup_manager.prune_old_backups(announce=announce)

    def restore_backup(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Backup ZIP to Restore",
            filetypes=[("ZIP backups", "*.zip"), ("All", "*.*")])
        if not path:
            return
        if not messagebox.askyesno(
                "Confirm Restore",
                f"Restore '{os.path.basename(path)}'?\n"
                "The current world will be archived first.",
                parent=self):
            return
        self._backup_manager.restore_from_zip(path)

    @property
    def _backup_in_progress(self) -> bool:
        # Read-only legacy alias for code that still checks this flag.
        return self._backup_manager.in_progress

    # ------------------------------------------------------------------
    # Backup list (rendered in the BACKUP tab)
    # ------------------------------------------------------------------
    def _list_existing_backups(self) -> list:
        """Return a list of (full_path, size_bytes, mtime_epoch) tuples
        for every backup zip in the configured destination folder,
        newest first. Returns [] if the folder is missing/unreadable."""
        dst = self.backup_dir_var.get()
        out = []
        if not dst or not os.path.isdir(dst):
            return out
        try:
            for name in os.listdir(dst):
                if not name.lower().endswith(".zip"):
                    continue
                full = os.path.join(dst, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if not os.path.isfile(full):
                    continue
                out.append((full, st.st_size, st.st_mtime))
        except OSError:
            return out
        out.sort(key=lambda x: x[2], reverse=True)
        return out

    def _refresh_backup_list(self) -> None:
        """Re-render the backup list shown in the BACKUP tab."""
        body = getattr(self, "_backup_list_body", None)
        if body is None:
            return
        # Clear existing rows
        for child in list(body.winfo_children()):
            try:
                child.destroy()
            except tk.TclError:
                pass
        backups = self._list_existing_backups()
        try:
            self._backup_count_var.set(f"({len(backups)})")
        except Exception:
            pass
        if not backups:
            tk.Label(body,
                     text="(no backups in destination folder yet)",
                     fg=Theme.MUTED, bg=Theme.BG_INPUT,
                     font=self.F_SMALL, anchor=tk.W,
                     padx=8, pady=8).pack(fill=tk.X)
            return
        for full_path, size, mtime in backups:
            self._render_backup_row(body, full_path, size, mtime)

    def _render_backup_row(self, parent, full_path: str,
                           size: int, mtime: float) -> None:
        from datetime import datetime as _dt
        row = tk.Frame(parent, bg=Theme.BG_INPUT)
        row.pack(fill=tk.X, pady=1, padx=2)
        # Filename + meta
        meta = tk.Frame(row, bg=Theme.BG_INPUT)
        meta.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6, pady=4)
        tk.Label(meta, text=os.path.basename(full_path),
                 fg=Theme.AMBER, bg=Theme.BG_INPUT,
                 font=self.F_NORMAL, anchor=tk.W).pack(anchor=tk.W)
        ts = _dt.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        tk.Label(meta,
                 text=f"  {ts}   •   {fmt_size(size)}",
                 fg=Theme.AMBER_DIM, bg=Theme.BG_INPUT,
                 font=self.F_SMALL, anchor=tk.W).pack(anchor=tk.W)
        # Actions
        from ui.widgets import TermButton
        TermButton(row, "↩ Restore",
                   lambda p=full_path: self._restore_specific_backup(p),
                   variant="amber", font_spec=self.F_SMALL,
                   padx=8, pady=2).pack(side=tk.RIGHT, padx=(2, 4),
                                         pady=4)
        TermButton(row, "🗑 Delete",
                   lambda p=full_path: self._delete_specific_backup(p),
                   variant="stop", font_spec=self.F_SMALL,
                   padx=8, pady=2).pack(side=tk.RIGHT, padx=(2, 0),
                                         pady=4)

    def _restore_specific_backup(self, path: str) -> None:
        if not os.path.isfile(path):
            self._notify("Backup file no longer exists.", level="error")
            self._refresh_backup_list()
            return
        if not messagebox.askyesno(
                "Confirm Restore",
                f"Restore '{os.path.basename(path)}'?\n\n"
                "The current world will be archived first.\n"
                "The server must be stopped before restoring.",
                parent=self):
            return
        if self._backup_manager.restore_from_zip(path):
            self._refresh_backup_list()

    def _delete_specific_backup(self, path: str) -> None:
        if not os.path.isfile(path):
            self._notify("Backup file no longer exists.", level="error")
            self._refresh_backup_list()
            return
        if not messagebox.askyesno(
                "Confirm Delete",
                f"Permanently delete '{os.path.basename(path)}'?\n"
                "This cannot be undone.",
                parent=self):
            return
        try:
            os.remove(path)
            self._notify(f"Deleted {os.path.basename(path)}",
                         level="success", duration_ms=1800)
            LOG.info("Deleted backup: %s", path)
        except OSError as e:
            self._notify(f"Delete failed: {e}", level="error")
            LOG.error("Delete backup failed: %s", e)
        self._refresh_backup_list()

    # ------------------------------------------------------------------
    # Auto-save scheduler
    # ------------------------------------------------------------------
    def _schedule_autosave(self):
        self.cancel_autosave_job()
        if not self.autosave_enabled_var.get():
            return
        try:
            interval_min = max(1, int(self.autosave_interval_var.get()))
        except ValueError:
            return
        ms = interval_min * 60 * 1000
        self.autosave_job_id = self.after(ms, self._autosave_tick)

    def _autosave_tick(self):
        if self.is_running and self.autosave_cmd_var.get():
            self._send_internal_command("/autosavenow")
            self.append_console("Auto-save triggered.", "system")
        self._start_async_backup(silent=True, reason="autosave")
        self._schedule_autosave()

    def cancel_autosave_job(self):
        if self.autosave_job_id:
            try:
                self.after_cancel(self.autosave_job_id)
            except Exception:
                pass
            self.autosave_job_id = None

    # ------------------------------------------------------------------
    # Cron-style schedule
    # ------------------------------------------------------------------
    def _apply_cron_schedule(self):
        self._cancel_cron_schedule()
        expr = self.cron_expr_var.get().strip()
        if not expr:
            return
        try:
            self._cron_entries = parse_cron_expr(expr)
        except ValueError as e:
            self.append_console(f"Cron parse error: {e}", "error")
            return
        self._schedule_next_cron()

    def _schedule_next_cron(self):
        if not self._cron_entries:
            return
        secs = seconds_until_next(self._cron_entries)
        self._cron_job_id = self.after(secs * 1000, self._cron_fire)
        self.append_console(
            f"Next scheduled restart in ~{secs // 60}m {secs % 60}s", "system")
        self._schedule_restart_warnings(secs)

    def _schedule_restart_warnings(self, total_secs: int):
        self._cancel_restart_warnings()
        for warn_secs, msg in [(300, "Server restart in 5 minutes!"),
                                (60,  "Server restart in 1 minute!"),
                                (10,  "Server restart in 10 seconds!")]:
            delay = total_secs - warn_secs
            if delay > 0:
                jid = self.after(delay * 1000,
                                 lambda m=msg: self.broadcast(m))
                self._restart_warning_jobs.append(jid)

    def _cancel_restart_warnings(self):
        for jid in self._restart_warning_jobs:
            try:
                self.after_cancel(jid)
            except Exception:
                pass
        self._restart_warning_jobs.clear()

    def _cron_fire(self):
        if not self.is_running:
            self._schedule_next_cron()
            return
        self.append_console("Scheduled restart firing…", "system")
        self.restart_server()
        self.after(10000, self._schedule_next_cron)

    def _cancel_cron_schedule(self):
        if self._cron_job_id:
            try:
                self.after_cancel(self._cron_job_id)
            except Exception:
                pass
            self._cron_job_id = None
        self._cancel_restart_warnings()

    # ------------------------------------------------------------------
    # ModDB catalogs async init
    # ------------------------------------------------------------------
    # init_moddb_catalogs_async is defined in the Mods section below
    # along with the rest of the ModDB browser logic ported from v2.

    # ------------------------------------------------------------------
    # UI animation helpers
    # ------------------------------------------------------------------
    def _blink_cursor(self):
        try:
            visible = getattr(self, 'cursor_visible', True)
            self.cursor_visible = not visible
            self.prompt_label.configure(
                text=(" ❯ " if self.cursor_visible else " ▌ "))
        except Exception:
            pass
        self.after(600, self._blink_cursor)

    def _glow_title(self):
        try:
            s = getattr(self, 'title_glow_state', 0)
            colors = [Theme.AMBER_GLOW, Theme.AMBER, Theme.AMBER_DIM, Theme.AMBER]
            self.title_label.configure(fg=colors[s % len(colors)])
            self.title_glow_state = s + 1
        except Exception:
            pass
        self.after(1800, self._glow_title)

    def _tick_uptime(self):
        if self.is_running and self.start_time:
            elapsed = int(time.time() - self.start_time)
            h, rem  = divmod(elapsed, 3600)
            m, sec  = divmod(rem, 60)
            self.uptime_var.set(f"{h:02d}:{m:02d}:{sec:02d}")
        else:
            self.uptime_var.set("00:00:00")
        if PSUTIL_AVAILABLE and self.is_running and self.server_process:
            self._update_resources()
        self.after(1000, self._tick_uptime)

    # ------------------------------------------------------------------
    # Periodic player-count poller — sends `/list clients` at a
    # user-configurable interval while the server is running. The
    # response feeds back through _parse_player_event → 'list' branch.
    # ------------------------------------------------------------------
    def _player_poll_interval_secs(self) -> int:
        """Read the configured interval, with 0 meaning 'disabled'."""
        try:
            return max(0, int(
                self._settings.get("player_count_poll_secs", 30)))
        except (ValueError, TypeError):
            return 30

    def _reschedule_player_count_poll(self) -> None:
        """Cancel any pending poll and schedule the next one based on
        the current setting. Safe to call from anywhere (settings save,
        server start/stop, etc.)."""
        jid = getattr(self, "_player_poll_job_id", None)
        if jid is not None:
            try:
                self.after_cancel(jid)
            except Exception:
                pass
            self._player_poll_job_id = None
        secs = self._player_poll_interval_secs()
        if secs <= 0:
            return  # Polling disabled.
        self._player_poll_job_id = self.after(
            secs * 1000, self._player_count_poll_tick)

    def _player_count_poll_tick(self) -> None:
        """Fire one /list clients ping if the server's running, then
        reschedule. Silently no-ops when the server isn't running so
        we don't spam errors into the console."""
        self._player_poll_job_id = None
        try:
            if self.is_running and self.server_process is not None:
                # _send_internal_command already only writes to the
                # server's stdin; no console echo to suppress here.
                self._send_internal_command("/list clients")
        except Exception:
            LOG.exception("player-count poll tick failed")
        # Always reschedule (even if we didn't send) so changes to
        # is_running pick up on the next tick.
        secs = self._player_poll_interval_secs()
        if secs > 0:
            self._player_poll_job_id = self.after(
                secs * 1000, self._player_count_poll_tick)

    def _update_resources(self):
        try:
            proc = self.server_process
            if proc is None or proc.poll() is not None:
                return
            ps = psutil.Process(proc.pid)
            cpu_pct = ps.cpu_percent(interval=None) / 100.0
            mem_info = ps.memory_info()
            total_mem = psutil.virtual_memory().total
            mem_frac = mem_info.rss / max(1, total_mem)
            self._set_resource_bar(self.cpu_fill, self.cpu_label,
                                   f"{cpu_pct * 100:.0f}%", cpu_pct)
            self._set_resource_bar(self.mem_fill, self.mem_label,
                                   f"{fmt_size(mem_info.rss)}", mem_frac)
            self.cpu_spark.push(cpu_pct)
            self.mem_spark.push(mem_frac)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Window close
    # ------------------------------------------------------------------
    def on_closing(self):
        if self.is_running:
            if not messagebox.askokcancel(
                    "Quit",
                    "The server is still running. Stop it and exit?",
                    parent=self):
                return
            self.cancel_autosave_job()
            self._cancel_cron_schedule()
            self._cancel_restart_warnings()
            self._set_status("SHUTTING DOWN", dot="stopping")
            self.stop_server(on_done=self._final_destroy)
            return
        self.cancel_autosave_job()
        self._cancel_cron_schedule()
        self.destroy()

    def _final_destroy(self):
        try:
            self.destroy()
        except Exception:
            pass

    # ==================================================================
    # Mods tab — full implementation ported from v2
    # ==================================================================
    def _build_mods_tab(self, *args, **kwargs):
        from ui.tab_mods import _build_mods_tab as _impl
        return _impl(self, *args, **kwargs)

    def _build_mods_installed_subtab(self, *args, **kwargs):
        from ui.tab_mods import _build_mods_installed_subtab as _impl
        return _impl(self, *args, **kwargs)

    def _build_mods_browse_subtab(self, *args, **kwargs):
        from ui.tab_mods import _build_mods_browse_subtab as _impl
        return _impl(self, *args, **kwargs)

    def _build_mods_browse_left(self, *args, **kwargs):
        from ui.tab_mods import _build_mods_browse_left as _impl
        return _impl(self, *args, **kwargs)

    def _build_mods_browse_right(self, *args, **kwargs):
        from ui.tab_mods import _build_mods_browse_right as _impl
        return _impl(self, *args, **kwargs)

    def load_mods(self):
        from ui.tab_mods import load_mods as _impl
        return _impl(self)

    def _selected_mod(self):
        from ui.tab_mods import _selected_mod as _impl
        return _impl(self)

    def enable_selected_mod(self):
        from ui.tab_mods import enable_selected_mod as _impl
        return _impl(self)

    def disable_selected_mod(self):
        from ui.tab_mods import disable_selected_mod as _impl
        return _impl(self)

    def add_mod(self):
        from ui.tab_mods import add_mod as _impl
        return _impl(self)

    def remove_selected_mod(self):
        from ui.tab_mods import remove_selected_mod as _impl
        return _impl(self)

    def open_selected_mod_on_moddb(self):
        from ui.tab_mods import open_selected_mod_on_moddb as _impl
        return _impl(self)

    def _open_moddb_worker(self, *args, **kwargs):
        from ui.tab_mods import _open_moddb_worker as _impl
        return _impl(self, *args, **kwargs)

    def _open_url_in_browser(self, *args, **kwargs):
        from ui.tab_mods import _open_url_in_browser as _impl
        return _impl(self, *args, **kwargs)

    def _mod_op_ok(self, *args, **kwargs):
        from ui.tab_mods import _mod_op_ok as _impl
        return _impl(self, *args, **kwargs)

    def init_moddb_catalogs_async(self):
        from ui.tab_mods import init_moddb_catalogs_async as _impl
        return _impl(self)

    def _moddb_catalogs_worker(self):
        from ui.tab_mods import _moddb_catalogs_worker as _impl
        return _impl(self)

    def _moddb_apply_catalogs(self, *args, **kwargs):
        from ui.tab_mods import _moddb_apply_catalogs as _impl
        return _impl(self, *args, **kwargs)

    def _toggle_moddb_tag(self, *args, **kwargs):
        from ui.tab_mods import _toggle_moddb_tag as _impl
        return _impl(self, *args, **kwargs)

    def _refresh_tag_button_styles(self):
        from ui.tab_mods import _refresh_tag_button_styles as _impl
        return _impl(self)

    def _clear_moddb_tags(self):
        from ui.tab_mods import _clear_moddb_tags as _impl
        return _impl(self)

    def _schedule_moddb_search(self, *args, **kwargs):
        from ui.tab_mods import _schedule_moddb_search as _impl
        return _impl(self, *args, **kwargs)

    def _run_moddb_search(self):
        from ui.tab_mods import _run_moddb_search as _impl
        return _impl(self)

    def _moddb_search_worker(self, *args, **kwargs):
        from ui.tab_mods import _moddb_search_worker as _impl
        return _impl(self, *args, **kwargs)

    def _moddb_apply_search(self, *args, **kwargs):
        from ui.tab_mods import _moddb_apply_search as _impl
        return _impl(self, *args, **kwargs)

    def _rerender_moddb_results(self):
        from ui.tab_mods import _rerender_moddb_results as _impl
        return _impl(self)

    def _on_moddb_row_click(self, *args, **kwargs):
        from ui.tab_mods import _on_moddb_row_click as _impl
        return _impl(self, *args, **kwargs)

    def _load_mod_details_async(self, *args, **kwargs):
        from ui.tab_mods import _load_mod_details_async as _impl
        return _impl(self, *args, **kwargs)

    def _mod_detail_worker(self, *args, **kwargs):
        from ui.tab_mods import _mod_detail_worker as _impl
        return _impl(self, *args, **kwargs)

    def _apply_mod_detail(self, *args, **kwargs):
        from ui.tab_mods import _apply_mod_detail as _impl
        return _impl(self, *args, **kwargs)

    def _render_mod_detail(self, *args, **kwargs):
        from ui.tab_mods import _render_mod_detail as _impl
        return _impl(self, *args, **kwargs)

    def _render_mod_files(self, *args, **kwargs):
        from ui.tab_mods import _render_mod_files as _impl
        return _impl(self, *args, **kwargs)

    def _pick_best_release(self, *args, **kwargs):
        from ui.tab_mods import _pick_best_release as _impl
        return _impl(self, *args, **kwargs)

    def _select_file_row(self, *args, **kwargs):
        from ui.tab_mods import _select_file_row as _impl
        return _impl(self, *args, **kwargs)

    def _on_moddb_file_click(self, *args, **kwargs):
        from ui.tab_mods import _on_moddb_file_click as _impl
        return _impl(self, *args, **kwargs)

    def _install_current_file(self):
        from ui.tab_mods import _install_current_file as _impl
        return _impl(self)

    def _cancel_moddb_download(self):
        from ui.tab_mods import _cancel_moddb_download as _impl
        return _impl(self)

    def _moddb_download_worker(self, *args, **kwargs):
        from ui.tab_mods import _moddb_download_worker as _impl
        return _impl(self, *args, **kwargs)

    def _finalize_moddb_download(self, *args, **kwargs):
        from ui.tab_mods import _finalize_moddb_download as _impl
        return _impl(self, *args, **kwargs)

    def _set_moddb_progress(self, *args, **kwargs):
        from ui.tab_mods import _set_moddb_progress as _impl
        return _impl(self, *args, **kwargs)

    def check_mod_updates(self):
        from ui.tab_mods import check_mod_updates as _impl
        return _impl(self)

    def _update_check_worker(self, *args, **kwargs):
        from ui.tab_mods import _update_check_worker as _impl
        return _impl(self, *args, **kwargs)

    def _show_update_report(self, *args, **kwargs):
        from ui.tab_mods import _show_update_report as _impl
        return _impl(self, *args, **kwargs)

    def _bulk_update(self, *args, **kwargs):
        from ui.tab_mods import _bulk_update as _impl
        return _impl(self, *args, **kwargs)

    def _bulk_update_worker(self, *args, **kwargs):
        from ui.tab_mods import _bulk_update_worker as _impl
        return _impl(self, *args, **kwargs)

    def _finalize_bulk_update(self, *args, **kwargs):
        from ui.tab_mods import _finalize_bulk_update as _impl
        return _impl(self, *args, **kwargs)

    def _set_moddb_status(self, *args, **kwargs):
        from ui.tab_mods import _set_moddb_status as _impl
        return _impl(self, *args, **kwargs)

    def _normalize_side(self, raw):
        """Fold any representation of the 'side' field to one of:
        'server', 'client', 'universal', 'unknown'."""
        if raw is None:
            return "unknown"
        s = str(raw).strip().lower()
        if s in ("server",):
            return "server"
        if s in ("client",):
            return "client"
        if s in ("both", "universal", "uni"):
            return "universal"
        return "unknown"

    def _side_badge(self, side):
        """Return (tag_name, label)."""
        if side == "server":
            return ("side_srv", "SERVER")
        if side == "universal":
            return ("side_both", " BOTH ")
        if side == "client":
            return ("side_cli", "CLIENT")
        return ("side_unk", "  ?   ")

    def _fmt_size(self, n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            return "?"
        if n <= 0:
            return "0 B"
        units = ["B", "KB", "MB", "GB"]
        i = 0
        v = float(n)
        while v >= 1024 and i < len(units) - 1:
            v /= 1024.0
            i += 1
        if i == 0:
            return f"{int(v)} {units[i]}"
        return f"{v:.1f} {units[i]}"

    def _version_is_newer(self, remote, local):
        """Compare two version strings component-wise. Unknown formats
        fall back to string inequality so we err on the side of flagging
        an update rather than missing one."""
        if not remote:
            return False
        if not local:
            return True
        def parts(s):
            out = []
            for seg in re.split(r"[.\-+]", s):
                m = re.match(r"(\d+)", seg)
                out.append(int(m.group(1)) if m else 0)
            return out
        try:
            rp = parts(remote)
            lp = parts(local)
        except Exception:
            return remote != local
        # Pad to equal length
        n = max(len(rp), len(lp))
        rp += [0] * (n - len(rp))
        lp += [0] * (n - len(lp))
        return rp > lp

    def _open_current_mod_in_browser(self):
        from ui.tab_mods import _open_current_mod_in_browser as _impl
        return _impl(self)


# ======================================================================
# Fatal startup error handler
# ======================================================================
def _fatal(err: Exception):
    import traceback
    tb = traceback.format_exc()
    msg = f"{type(err).__name__}: {err}\n\n{tb}"
    sys.stderr.write("\n" + "=" * 60 + "\n")
    sys.stderr.write("VSSM failed to start\n")
    sys.stderr.write("=" * 60 + "\n")
    sys.stderr.write(msg + "\n")
    try:
        LOG.exception("Fatal startup error: %s", err)
    except Exception:
        pass
    try:
        from core.constants import log_dir
        with open(os.path.join(log_dir(), "crash.log"), "w", encoding="utf-8") as f:
            f.write(msg)
    except Exception:
        pass
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("VSSM failed to start", msg[:2000])
        root.destroy()
    except Exception:
        pass


# ======================================================================
# Entry point  (improvement #21: proper main() function)
# ======================================================================
def main():
    parser = argparse.ArgumentParser(description="VSSM — VS Server Manager")
    parser.add_argument("--log-level",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        default=None,
                        help="Override log level (improvement #20)")
    args = parser.parse_args()
    if args.log_level:
        LOG.setLevel(getattr(logging, args.log_level))
        LOG.info("Log level set to %s via --log-level", args.log_level)
    try:
        app = ServerManagerApp()
        app.mainloop()
    except Exception as e:
        _fatal(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
