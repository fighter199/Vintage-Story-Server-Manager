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
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from collections import deque
from tkinter import filedialog, messagebox, ttk
from typing import Optional

# ── Local packages ─────────────────────────────────────────────────────
from core.constants import (APP_NAME, APP_VERSION, LOG, script_dir)
from core.parsers import (parse_cron_expr, seconds_until_next)
from core.profiles import PROFILE_FIELDS, unsaved_fields
from core.settings import (load_settings, save_settings, get_active_profile,
                            load_custom_commands, chat_log_path, load_player_totals,
                            normalize_window_layout, fit_geometry)
from core.custom_commands import ChatCommandDispatcher
from core.command_files import load_commands, ensure_user_file, USER_FILE
from core.utils import (open_in_file_manager,
                        open_in_editor,
                         fmt_size, enable_windows_dpi_awareness)
from ui.theme import (Theme, ColorRemap, palette, pick_mono_font, font_sizes,
                      TEXT_SCALE_MIN, TEXT_SCALE_MAX)
from ui.widgets import (TermButton, TermEntry, TabStrip,
                        retheme_tree, reflow_all,
                        themed_frame,
                         ToastQueue)
from ui.tab_custom_commands import CustomCommandsTab
import ui.tab_mods as _tab_mods
from ui.tab_chat_log import ChatLogTab
from core.chat_log import (ChatLogStore)
from core.player_timers import PlayerTimers
from ui.tab_autorun import AutorunTab
from ui.world_map import WorldMapTab
from core.autorun import AutorunScheduler
from mods.moddb import ModDbClient
from backup import BackupManager
from host.header import HeaderMixin
from host.console import ConsoleMixin
from host.players import PlayersMixin
from host.server import ServerMixin
from host.backups import BackupsMixin
from host.scheduling import SchedulingMixin

# Optional psutil
try:
    import psutil  # noqa: F401 — only PSUTIL_AVAILABLE is used here
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


# Set by main() when the user passes --log-level; when present it wins
# over the persisted "log_level" settings key.
_CLI_LOG_LEVEL: Optional[str] = None


# ======================================================================
# Command reference loader
# ======================================================================
FALLBACK_COMMANDS = {
    "Server": {
        "/stop":   {"description": "Stop the server.", "template": "/stop", "args": []},
        "/autosavenow": {"description": "Save the world now.",
                         "template": "/autosavenow", "args": []},
        "/list clients": {"description": "List connected players.",
                          "template": "/list clients", "args": []},
    }
}


def load_commands_data() -> dict:
    """Merged built-in + user command reference (see core/command_files).
    Problems reading either file are kept in _COMMAND_FILE_PROBLEMS so
    the UI can report them instead of failing silently."""
    global _COMMAND_FILE_PROBLEMS
    try:
        sdir = script_dir()
    except Exception:
        sdir = os.getcwd()
    data, _COMMAND_FILE_PROBLEMS = load_commands(sdir)
    return data or FALLBACK_COMMANDS


_COMMAND_FILE_PROBLEMS: list = []


# ======================================================================
# Boot splash
# ======================================================================
class BootSplash(tk.Toplevel):

    @staticmethod
    def _boot_lines():
        """Built at construction time (not import time) so the splash
        picks up the user's theme preset — apply_preset() mutates the
        Theme class attributes after this module has been imported."""
        return [
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
        self.BOOT_LINES = self._boot_lines()
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
class ServerManagerApp(HeaderMixin, ConsoleMixin, PlayersMixin, ServerMixin, BackupsMixin, SchedulingMixin, tk.Tk):

    # ---- Configurable crash-loop defaults (now overridden by settings) ----
    CRASH_WINDOW_SECS = 600
    CRASH_LIMIT       = 3

    # Cap on console scrollback (lines). Both the in-memory line list and
    # the Text widget are trimmed to this, so a server left running for
    # days can't grow VSSM's memory without bound.
    MAX_CONSOLE_LINES = 5000

    def __init__(self):
        super().__init__()
        self.withdraw()
        self.title("VSSM — Vintage Story Server Manager")
        self.configure(bg=Theme.BG_DARK)

        # ---- Load settings early (needed for theme + scale) ----------
        self._settings = load_settings()
        get_active_profile(self._settings)   # creates the profile if missing

        # Apply the persisted log level (--log-level CLI flag wins).
        if not _CLI_LOG_LEVEL:
            lvl = str(self._settings.get("log_level") or "").upper()
            if lvl in ("DEBUG", "INFO", "WARNING", "ERROR"):
                LOG.setLevel(getattr(logging, lvl))

        # ---- Display scaling -----------------------------------------
        # Tk already converts font points to pixels using the display's
        # DPI, so fonts are sized in points × the user's text-size factor
        # only. `_ui_scale` (DPI × text size) sizes the window itself.
        self._auto_scale = self._detect_scale()
        try:
            self._user_scale = float(self._settings.get("ui_scale_override") or 1.0)
        except (TypeError, ValueError):
            self._user_scale = 1.0
        self._user_scale = max(TEXT_SCALE_MIN, min(TEXT_SCALE_MAX, self._user_scale))
        self._ui_scale   = self._auto_scale * self._user_scale
        self.text_scale_var = tk.StringVar(value=f"{self._user_scale:.0%}")

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        win_w = min(int(sw * 0.75), max(1100, int(1280 * self._ui_scale)))
        win_h = min(int(sh * 0.85), max(700,  int(820  * self._ui_scale)))
        x = max(0, (sw - win_w) // 2)
        y = max(0, (sh - win_h) // 3)
        self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.minsize(min(900, int(sw * 0.6)), min(600, int(sh * 0.6)))
        # Window layout from the last session (size/position, splits,
        # selected tab) — see _save_window_layout.
        self._layout = normalize_window_layout(self._settings.get("window_layout"))
        saved = fit_geometry(self._layout.get("geometry", ""), self._virtual_screen())
        if saved:
            self.geometry(saved)
        self._normal_geometry = saved or self.geometry()

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
        # Set by the world map window while it deletes chunks from the
        # savegame; the server must not start until it's done.
        self._world_edit_in_progress = False
        # Set when the user stops with "backup before stop" enabled;
        # consumed by _finalize_stop, which takes the snapshot after
        # the process has exited (world files quiescent).
        self._pending_stop_backup  = False
        self._crash_times: deque   = deque(maxlen=20)
        self._cmd_history: list    = []
        self._cmd_history_pos      = 0
        self._cron_entries         = []
        self._cron_job_id          = None
        self._cron_warning_jobs    = []
        # Serialises concurrent writes to server_process.stdin from
        # different threads / timer paths (review §3.2). Cheap insurance
        # against interleaved bytes when e.g. an autorun tick collides
        # with a manual button click.
        self._stdin_lock           = threading.Lock()
        self._restart_warning_jobs = []
        self._cpu_history: deque   = deque(maxlen=120)
        self._mem_history: deque   = deque(maxlen=120)
        # Re-used psutil.Process for the resource panel — cpu_percent()
        # measures against the previous call on the SAME instance, so a
        # fresh instance every tick would report 0.0 forever.
        self._psutil_proc          = None

        # Players
        self._players: list        = []
        self._operators: set       = set()
        self._pending_role_query: deque = deque()
        # role cache: player_name -> role string
        self._player_roles: dict   = {}

        # Chat-log store: per-group history + user-named groups.
        # Loads from chat_log_<profile>.json on construction; saves
        # on flush (called from on_closing and on profile switches).
        self._chat_store = ChatLogStore(
            load_history=self._chat_log_load,
            save_history=self._chat_log_save,
        )
        self._chat_log_tab: "ChatLogTab | None" = None

        # Multi-line /list clients buffer (see _flush_list_buffer)
        self._list_buffer:        list = []
        self._list_buffer_active: bool = False
        self._list_buffer_job_id       = None

        # Player timer engine — session + lifetime playtime tracking.
        # Lazy-resolves the totals dict on every read so it always
        # points at the active profile's slot, even after a profile
        # switch. Mutating that dict mutates settings in place.
        self._player_timers = PlayerTimers(
            totals_provider=lambda: load_player_totals(self._settings))
        # Per-row label refs for in-place 1Hz updates without rebuild.
        # Maps player name → (session_label, total_label).
        self._player_timer_labels: dict = {}
        # Periodic-flush job id (cancelled on close)
        self._timer_flush_job_id = None

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

        # Autorun scheduler — fires console commands at fixed intervals
        # while the server is running. The rule list is read fresh on
        # every tick from settings (via the AutorunTab provider once
        # the tab exists), so live edits in the UI take effect on the
        # very next tick without re-attaching anything.
        self._autorun_scheduler = AutorunScheduler(
            rules_provider = self._autorun_rules_provider,
            send           = self._autorun_send,
            player_count   = self._autorun_player_count,
            audit          = lambda a: self.after_idle(
                self._autorun_record_audit, a),
        )
        self._autorun_tab: "AutorunTab | None" = None

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
        # Independent keep-last-N caps for the start/stop backup
        # families (startbackup-*.zip / stopbackup-*.zip). 0 = keep all.
        self.max_start_backups_var    = tk.StringVar(value="5")
        self.max_stop_backups_var     = tk.StringVar(value="5")
        self.backup_before_start_var  = tk.BooleanVar()
        self.backup_before_stop_var   = tk.BooleanVar()
        self.autosave_enabled_var     = tk.BooleanVar(value=False)
        self.autosave_interval_var    = tk.StringVar(value="30")
        self.autosave_cmd_var         = tk.BooleanVar(value=True)
        self.autorestart_var          = tk.BooleanVar()
        self.restart_interval_var     = tk.StringVar()
        # Player-aware restart/shutdown guards — controlled by the
        # three checkboxes added to the Settings tab. Default off so
        # behaviour is unchanged for existing users.
        self.check_players_before_restart_var           = tk.BooleanVar(value=False)
        self.check_players_before_scheduled_restart_var = tk.BooleanVar(value=False)
        self.check_players_before_shutdown_var          = tk.BooleanVar(value=False)
        # State for a deferred scheduled restart (waiting for empty server)
        self._deferred_restart_poll_id: Optional[str] = None
        self._deferred_restart_active: bool           = False
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
        self._init_folder_lists()
        self._restore_sidebar_tab()
        self._startup_complete = True

        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        BootSplash(self, self.F_NORMAL, on_done=self._after_boot,
                   scale=self._ui_scale)

    # ------------------------------------------------------------------
    # Post-boot
    # ------------------------------------------------------------------
    def _after_boot(self):
        self.deiconify()
        self.lift()
        if self._layout.get("zoomed"):
            self._set_zoomed(True)
        self.bind("<Configure>", self._track_normal_geometry, add="+")
        self._toast = ToastQueue(self, self.F_SMALL)
        self._blink_cursor()
        self._glow_title()
        self._tick_uptime()
        self._reschedule_player_count_poll()
        self._tick_player_timers()    # 1Hz label refresh
        self._schedule_timer_flush()  # 60s persistence flush
        self._tick_autorun()  # 1Hz scheduler tick (no-op while stopped)
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
        for problem in _COMMAND_FILE_PROBLEMS:
            self.append_console(f"Commands: {problem}", "error")
        self.append_console(
            "Hotkeys: Ctrl+L clear · Ctrl+Enter send · ↑/↓ history · "
            "Right-click console to copy", "system")
        LOG.info("%s %s started", APP_NAME, APP_VERSION)
        if self.auto_update_check_var.get():
            self.after(4000, self._check_for_updates)
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
        """Create the F_* fonts, or resize them in place. They're named
        Tk fonts, so every widget using one updates immediately — text
        size changes apply live, no restart."""
        pixel_font = self._mono_name.lower() in ("vt323", "share tech mono")
        try:
            aqua = self.tk.call("tk", "windowingsystem") == "aqua"
        except tk.TclError:
            aqua = False
        for attr, (size, bold) in font_sizes(self._user_scale, pixel_font,
                                             aqua).items():
            weight = "bold" if bold else "normal"
            font = getattr(self, attr, None)
            if isinstance(font, tkfont.Font):
                font.configure(family=self._mono_name, size=size, weight=weight)
            else:
                setattr(self, attr, tkfont.Font(self, family=self._mono_name,
                                                size=size, weight=weight))

    def _bump_ui_scale(self, delta):
        self._set_text_scale(self._user_scale + delta)

    def _reset_ui_scale(self):
        self._set_text_scale(1.0)

    def _set_text_scale(self, scale: float) -> None:
        scale = round(max(TEXT_SCALE_MIN, min(TEXT_SCALE_MAX, scale)), 2)
        if abs(scale - self._user_scale) < 0.001:
            return
        self._user_scale = scale
        self._ui_scale   = self._auto_scale * self._user_scale
        self._reapply_scale()

    def _reapply_scale(self):
        self._rebuild_fonts()
        self._ttk_style_ready = False
        self._setup_ttk_style()
        strip = getattr(self, "_tab_strip", None)
        if strip is not None:
            strip.relayout()
        self.update_idletasks()          # new font metrics, then re-wrap
        reflow_all()
        self.text_scale_var.set(f"{self._user_scale:.0%}")
        self._notify(f"Text size {self._user_scale:.0%}", level="info")
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
        # clam draws notebook and tab outlines in near-white unless told
        # otherwise; keep them in the theme's border colour.
        style.configure("Term.TNotebook",
                        background=Theme.BG_DARK, borderwidth=0,
                        tabmargins=[0, 0, 0, 0], bordercolor=Theme.BORDER,
                        lightcolor=Theme.BORDER, darkcolor=Theme.BORDER)
        style.configure("Term.TNotebook.Tab",
                        background=Theme.BG_DARK, foreground=Theme.AMBER_DIM,
                        padding=[14, 6], borderwidth=0, font=self.F_NORMAL,
                        bordercolor=Theme.BORDER, lightcolor=Theme.BG_DARK,
                        darkcolor=Theme.BG_DARK)
        style.map("Term.TNotebook.Tab",
                  background=[("selected", Theme.BG_PANEL), ("active", Theme.BG_PANEL)],
                  foreground=[("selected", Theme.AMBER_GLOW), ("active", Theme.AMBER)],
                  bordercolor=[("selected", Theme.AMBER_DIM)],
                  lightcolor=[("selected", Theme.BG_PANEL)])
        # Sidebar notebook: its tab row is drawn by ui.widgets.TabStrip.
        style.configure("Strip.TNotebook", background=Theme.BG_PANEL,
                        borderwidth=0, tabmargins=[0, 0, 0, 0],
                        bordercolor=Theme.BORDER, lightcolor=Theme.BORDER,
                        darkcolor=Theme.BORDER)
        style.layout("Strip.TNotebook.Tab", [])
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

        # Header (collapsible). The toolbar strip is always visible —
        # it carries the toggle button, the inline title, and a small
        # warning indicator. The fancy 3-column layout (full title,
        # version subtitle, hotkeys cheat sheet, full setup warning)
        # lives in `self._header_body`, which can be hidden via
        # pack_forget without disturbing any other layout.
        self._header_collapsed = bool(
            self._settings.get("header_collapsed", False))
        # `setup_warning_var` is created here (before the body is built)
        # because the toolbar's status indicator binds to the same var.
        self.setup_warning_var = tk.StringVar(value="")

        self._header_toolbar = tk.Frame(root_pad, bg=Theme.BG_DARK)
        self._header_toolbar.pack(fill=tk.X, pady=(2, 0))
        self._build_header_toolbar(self._header_toolbar)

        self._header_body = tk.Frame(root_pad, bg=Theme.BG_DARK)
        self._build_header_body(self._header_body)

        # Pack body unless we're starting collapsed. The horizontal
        # separator below it is part of the body so it disappears too.
        if not self._header_collapsed:
            self._header_body.pack(fill=tk.X, pady=(2, 8))
        else:
            # Even when collapsed, keep a thin separator so the toolbar
            # doesn't visually merge with the exe panel below.
            pass
        self._header_separator = tk.Frame(
            root_pad, bg=Theme.BORDER, height=1)
        self._header_separator.pack(fill=tk.X, pady=(0, 8))

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
        self.btn_start   = TermButton(ctrl, "▶ Start",   self._start_server_checked, variant="start", font_spec=self.F_BTN)
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
        self._main_paned = main_paned
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
                    main_paned.sashpos(
                        0, int(w * self._layout.get("main_sash", 0.6)))
                elif retry < 20:
                    self.after(100, lambda: _seed_main(retry + 1))
            except tk.TclError:
                pass
        self.after(150, _seed_main)
        main_paned.bind("<ButtonRelease-1>",
                        lambda _e: self._save_window_layout(), add="+")

        self.server_path_var.trace_add('write', lambda *_: self._recompute_setup_warning())
        self.mods_folder_var.trace_add('write', lambda *_: self._recompute_setup_warning())
        self._recompute_setup_warning()

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

        self.notebook = ttk.Notebook(nb_bg, style="Strip.TNotebook")
        self._tab_strip = TabStrip(nb_bg, self.notebook, self.F_SMALL)
        self._tab_strip.pack(fill=tk.X, pady=(0, 4))
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
            self._tab_strip.add(frame, text=tab_name)
            builder(frame)

        # NEW: Custom Commands tab
        custom_cmds_frame = tk.Frame(self.notebook, bg=Theme.BG_PANEL)
        self._tab_strip.add(custom_cmds_frame, text="CUSTOM CMDS")
        self._custom_cmds_tab = CustomCommandsTab(custom_cmds_frame, self)

        # NEW: Chat Log tab — per-group chat history, persisted.
        chat_log_frame = tk.Frame(self.notebook, bg=Theme.BG_PANEL)
        self._tab_strip.add(chat_log_frame, text="CHAT LOG")
        self._chat_log_tab = ChatLogTab(chat_log_frame, self)

        # NEW: Autorun tab
        autorun_frame = tk.Frame(self.notebook, bg=Theme.BG_PANEL)
        self._tab_strip.add(autorun_frame, text="AUTORUN")
        self._autorun_tab = AutorunTab(autorun_frame, self)

        # World map: savegame viewer + chunk pruning (opens a window).
        world_map_frame = tk.Frame(self.notebook, bg=Theme.BG_PANEL)
        self._tab_strip.add(world_map_frame, text="WORLD MAP")
        self._world_map_tab = WorldMapTab(world_map_frame, self)
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
                    side_paned.sashpos(
                        0, int(h * self._layout.get("side_sash", 0.25)))
                elif retry < 20:
                    self.after(100, lambda: _seed_side(retry + 1))
            except tk.TclError:
                pass
        self.after(180, _seed_side)
        side_paned.bind("<ButtonRelease-1>",
                        lambda _e: self._save_window_layout(), add="+")

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
                mine = isinstance(_entry, dict) and _entry.get("_source") == "user"
                self.cmd_tree.insert(
                    tk.END,
                    f"    {name}{'  ◆' if has_args else ''}{'  ★' if mine else ''}\n",
                    ("cmd",))
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
            source = ("  ·  ★ yours (vs_commands_user.json)"
                      if entry.get("_source") == "user" else "")
            self.cmd_details.insert(tk.END, f"{category}{source}\n\n", ("cat",))
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
        if _COMMAND_FILE_PROBLEMS:
            for problem in _COMMAND_FILE_PROBLEMS:
                self.append_console(f"Commands: {problem}", "error")
            self._notify(_COMMAND_FILE_PROBLEMS[0], level="error",
                         duration_ms=8000)
        else:
            self._notify(f"Reloaded — {total} commands", level="success")

    def _edit_user_commands(self):
        """Open vs_commands_user.json (created from a template if needed)
        in the system's editor for .json files."""
        try:
            path = ensure_user_file(script_dir())
        except OSError as e:
            self._notify(f"Could not create {USER_FILE}: {e}", level="error")
            return
        if not open_in_editor(path):
            self._notify(f"Open {path} in a text editor, then press Reload.",
                         level="info", duration_ms=8000)
        else:
            self._notify(f"Editing {USER_FILE} — press Reload when saved.",
                         level="info", duration_ms=6000)

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

    def _apply_theme(self, preset: str) -> None:
        """Switch colour preset live: every existing widget, text tag
        and canvas item is mapped from the old palette to the new one."""
        old = palette()
        Theme.apply_preset(preset)
        if preset == "custom":
            Theme.load_custom_colors(self._settings.get("custom_theme_colors", {}))
        self._theme_preset = preset
        self._ttk_style_ready = False
        self._setup_ttk_style()
        remap = ColorRemap(old, palette())
        if remap:
            retheme_tree(self, remap)
        try:
            self._refresh_tag_button_styles()   # mod browser tag chips
        except Exception:
            LOG.exception("restyling mod tag chips failed")

    def _on_theme_change(self):
        preset = self.theme_preset_var.get()
        self._apply_theme(preset)
        self._settings["theme_preset"] = preset
        save_settings(self._settings)
        self._notify(f"Theme: {preset}", level="info")

    def _save_custom_colors(self):
        colors = {}
        for key, var in self._custom_color_vars.items():
            val = var.get().strip()
            if val:
                colors[key] = val
        self._settings["custom_theme_colors"] = colors
        # Saving custom colours means wanting to see them: switch to the
        # custom preset (or refresh it) right away.
        self.theme_preset_var.set("custom")
        self._settings["theme_preset"] = "custom"
        save_settings(self._settings)
        self._apply_theme("custom")
        self._notify("Custom colors saved and applied.", level="success")

    def _profile_field_values(self) -> dict:
        """What the UI currently shows for each per-profile setting."""
        return {key: getattr(self, key + "_var").get() for key in PROFILE_FIELDS}

    def _profile_has_unsaved_changes(self) -> bool:
        return bool(unsaved_fields(get_active_profile(self._settings),
                                   self._profile_field_values()))

    def _save_profile_settings(self):
        profile = get_active_profile(self._settings)
        profile.update(self._profile_field_values())
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

    # ------------------------------------------------------------------
    # Window layout persistence
    # ------------------------------------------------------------------
    def _virtual_screen(self) -> tuple:
        """(x, y, width, height) of the desktop across all monitors."""
        if sys.platform == "win32":
            try:
                import ctypes
                metrics = ctypes.windll.user32.GetSystemMetrics
                box = tuple(metrics(i) for i in (76, 77, 78, 79))
                if box[2] > 0 and box[3] > 0:
                    return box
            except Exception:
                pass
        return (0, 0, self.winfo_screenwidth(), self.winfo_screenheight())

    def _is_zoomed(self) -> bool:
        try:
            if self.state() == "zoomed":                   # Windows / macOS
                return True
            return bool(self.attributes("-zoomed"))        # X11
        except tk.TclError:
            return False

    def _set_zoomed(self, zoomed: bool) -> None:
        try:
            self.state("zoomed" if zoomed else "normal")
        except tk.TclError:
            try:
                self.attributes("-zoomed", bool(zoomed))
            except tk.TclError:
                pass

    def _track_normal_geometry(self, event) -> None:
        # The root's binding also sees every child's <Configure>.
        if event.widget is not self:
            return
        try:
            if self.state() == "normal" and not self._is_zoomed():
                self._normal_geometry = self.geometry()
        except tk.TclError:
            pass

    def _restore_sidebar_tab(self) -> None:
        wanted = self._layout.get("tab")
        if not wanted:
            return
        for tab in self.notebook.tabs():
            if self.notebook.tab(tab, "text") == wanted:
                self.notebook.select(tab)
                return

    def _save_window_layout(self) -> None:
        """Remember window size/position, maximized state, the console
        and sidebar splits, and the open sidebar tab for next launch."""
        layout = dict(self._layout)
        layout["geometry"] = self._normal_geometry
        layout["zoomed"] = self._is_zoomed()
        for key, paned, size in (
                ("main_sash", getattr(self, "_main_paned", None), "width"),
                ("side_sash", getattr(self, "_sidebar_paned", None), "height")):
            try:
                total = getattr(paned, f"winfo_{size}")()
                if total > 50:
                    layout[key] = round(paned.sashpos(0) / total, 3)
            except (AttributeError, tk.TclError):
                pass
        try:
            layout["tab"] = self.notebook.tab(self.notebook.select(), "text")
        except tk.TclError:
            pass
        layout = normalize_window_layout(layout)
        if layout != self._settings.get("window_layout"):
            self._layout = layout
            self._settings["window_layout"] = layout
            try:
                save_settings(self._settings)
            except Exception:
                LOG.exception("saving window layout failed")

    # ------------------------------------------------------------------
    # MODS / BACKUP lists follow their folders
    # ------------------------------------------------------------------
    def _init_folder_lists(self) -> None:
        """Fill the MODS and BACKUP lists once the saved folder paths
        are applied (the tabs are built before that, so they used to
        start empty), then keep them current: on folder edits and
        whenever their tab is opened."""
        self._mods_list_sig = None
        self._folder_refresh_jobs: dict = {}
        self._refresh_backup_list()
        self._load_mods_if_changed(force=True)
        self.mods_folder_var.trace_add(
            "write", lambda *_: self._schedule_folder_refresh("mods"))
        self.backup_dir_var.trace_add(
            "write", lambda *_: self._schedule_folder_refresh("backups"))
        self.notebook.bind("<<NotebookTabChanged>>",
                           self._on_sidebar_tab_changed, add="+")

    def _schedule_folder_refresh(self, which: str) -> None:
        # Debounced: a path typed into SETTINGS fires on every keystroke.
        job = self._folder_refresh_jobs.pop(which, None)
        if job is not None:
            self.after_cancel(job)
        fn = (self._refresh_backup_list if which == "backups"
              else lambda: self._load_mods_if_changed(force=True))
        self._folder_refresh_jobs[which] = self.after(400, fn)

    def _on_sidebar_tab_changed(self, _event=None) -> None:
        try:
            text = self.notebook.tab(self.notebook.select(), "text")
        except tk.TclError:
            return
        if text == "BACKUP":
            self._refresh_backup_list()
        elif text == "MODS":
            self._load_mods_if_changed()

    def _load_mods_if_changed(self, force: bool = False) -> None:
        """Reload the installed-mods list if the Mods folder changed
        (path or contents) since the last load. Parsing every mod's
        modinfo isn't free, so tab switches skip it when nothing moved."""
        folder = self.mods_folder_var.get().strip()
        try:
            sig = (folder, os.path.getmtime(folder)) if folder else (folder, None)
        except OSError:
            sig = (folder, None)
        if force or sig != self._mods_list_sig:
            self._mods_list_sig = sig
            try:
                self.load_mods()
            except Exception:
                LOG.exception("loading the mod list failed")

    def _apply_default_paths(self):
        """Show the active profile's settings (missing ones get their
        defaults — the player checks default off, as before they existed)."""
        profile = get_active_profile(self._settings)
        for key, default in PROFILE_FIELDS.items():
            getattr(self, key + "_var").set(profile.get(key, default))

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
    # Window close
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Chat log persistence helpers
    # ------------------------------------------------------------------
    def _chat_log_path_for_active_profile(self) -> str:
        return chat_log_path(self._settings.get("active_profile"))

    def _chat_log_load(self) -> dict:
        """Read the per-profile chat history blob from disk.
        Returns {} if the file doesn't exist or is unreadable —
        ChatLogStore handles either gracefully."""
        path = self._chat_log_path_for_active_profile()
        if not os.path.isfile(path):
            return {}
        try:
            import json
            with open(path, "r", encoding="utf-8") as f:
                blob = json.load(f)
            return blob if isinstance(blob, dict) else {}
        except Exception:
            LOG.exception("chat log load failed: %s", path)
            return {}

    def _chat_log_save(self, blob: dict) -> None:
        """Atomic-write the chat history blob to disk."""
        path = self._chat_log_path_for_active_profile()
        tmp  = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            import json
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(blob, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            LOG.exception("chat log save failed: %s", path)
            # Best-effort tmp cleanup
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def _chat_store_changed(self) -> None:
        """Hook for the tab to call after rename / clear actions.
        Persists the current state immediately."""
        try:
            self._chat_store.flush()
        except Exception:
            LOG.exception("chat store flush failed")

    def on_closing(self):
        try:
            self._save_window_layout()
        except Exception:
            LOG.exception("saving window layout on close failed")
        # Flush the chat-log store first so any unsaved messages land
        # on disk even if the rest of shutdown is interrupted.
        try:
            self._chat_store.flush()
        except Exception:
            LOG.exception("chat store flush on close failed")

        # Final timer flush — even if the server is still running,
        # capture the in-flight session time before we exit.
        try:
            self._player_timers.flush()
            self._persist_player_totals()
        except Exception:
            pass
        if self._world_edit_in_progress:
            # Quitting mid-edit is safe: SQLite rolls back the open
            # transaction on next open, and a rewrite only replaces
            # the savegame once the new file is complete.
            if not messagebox.askokcancel(
                    "Quit",
                    "The world map is still modifying the savegame. "
                    "Quit anyway? Unfinished work is rolled back - no "
                    "chunk column is ever left half-deleted.",
                    icon="warning", parent=self):
                return
        if self.is_running:
            if not messagebox.askokcancel(
                    "Quit",
                    "The server is still running. Stop it and exit?",
                    parent=self):
                return
            self.cancel_autosave_job()
            self._cancel_cron_schedule()
            self._cancel_restart_warnings()
            self._cancel_deferred_restart_wait()
            self._set_status("SHUTTING DOWN", dot="stopping")
            # Skip the player-check guard here — the user already
            # confirmed the close-window prompt, no need to ask again.
            self.stop_server(on_done=self._final_destroy,
                             skip_player_check=True)
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
    # Mods tab — the code lives in ui/tab_mods.py; its functions are
    # attached as methods below the class (_MODS_TAB_METHODS).
    # ==================================================================
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
        # fmt_size delegated to core.utils.fmt_size for a single source of truth
        # (review §2.1). This shim is kept because many call sites use
        # app._fmt_size(...); a follow-up patch can switch them to direct
        # imports and drop the method entirely.
        return fmt_size(n)


# The mods tab is implemented in ui/tab_mods.py as functions taking the
# app as their first argument. The rest of the app — and those functions
# themselves — call them as app.<name>(...), so they're attached to the
# class as methods.
_MODS_TAB_METHODS = (
    "_build_mods_tab",
    "_build_mods_installed_subtab",
    "_build_mods_browse_subtab",
    "_build_mods_browse_left",
    "_build_mods_browse_right",
    "load_mods",
    "_selected_mod",
    "enable_selected_mod",
    "disable_selected_mod",
    "add_mod",
    "remove_selected_mod",
    "open_selected_mod_on_moddb",
    "_open_moddb_worker",
    "_open_url_in_browser",
    "_mod_op_ok",
    "init_moddb_catalogs_async",
    "_moddb_catalogs_worker",
    "_moddb_apply_catalogs",
    "_toggle_moddb_tag",
    "_refresh_tag_button_styles",
    "_clear_moddb_tags",
    "_schedule_moddb_search",
    "_run_moddb_search",
    "_moddb_search_worker",
    "_moddb_apply_search",
    "_rerender_moddb_results",
    "_on_moddb_row_click",
    "_load_mod_details_async",
    "_mod_detail_worker",
    "_apply_mod_detail",
    "_render_mod_detail",
    "_render_mod_files",
    "_pick_best_release",
    "_select_file_row",
    "_on_moddb_file_click",
    "_install_current_file",
    "_cancel_moddb_download",
    "_moddb_download_worker",
    "_finalize_moddb_download",
    "_set_moddb_progress",
    "check_mod_updates",
    "_update_check_worker",
    "_show_update_report",
    "_bulk_update",
    "_bulk_update_worker",
    "_finalize_bulk_update",
    "_set_moddb_status",
    "_open_current_mod_in_browser",
)
for _name in _MODS_TAB_METHODS:
    setattr(ServerManagerApp, _name, getattr(_tab_mods, _name))
del _name


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
    global _CLI_LOG_LEVEL
    parser = argparse.ArgumentParser(description="VSSM — VS Server Manager")
    parser.add_argument("--log-level",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        default=None,
                        help="Override log level (improvement #20)")
    args = parser.parse_args()
    if args.log_level:
        _CLI_LOG_LEVEL = args.log_level
        LOG.setLevel(getattr(logging, args.log_level))
        LOG.info("Log level set to %s via --log-level", args.log_level)
    # Without this Windows renders VSSM at 96 DPI and stretches it on
    # 125–200 % displays, which blurs all the text.
    enable_windows_dpi_awareness()
    try:
        app = ServerManagerApp()
        app.mainloop()
    except Exception as e:
        _fatal(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
