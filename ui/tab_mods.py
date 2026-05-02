"""
ui/tab_mods.py — Mod manager tab + ModDB browser.

This module exposes every mod-related action and UI builder as a
module-level function taking `app` (the ServerManagerApp instance) as
its first argument. ServerManagerApp keeps thin shim methods that
forward to these functions so existing buttons and Tk callbacks keep
working unchanged.

The pure helpers `_normalize_side`, `_side_badge`, `_fmt_size`, and
`_version_is_newer` remain as methods on ServerManagerApp itself —
they are heavily called from inside this module via `app._fmt_size(...)`
etc., and lifting them out would just produce a circular import.
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from datetime import datetime
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk

from core.constants import LOG
from core.utils import clean_mod_filename, fmt_size
from core.parsers import version_key
from mods.inspector import LocalModInspector
from .theme import Theme
from .widgets import (TermButton, TermEntry, TermText, TermCheckbutton,
                      Sparkline, ScrollableFrame, themed_frame,
                      panel_header, collapsible_section)


def _sorted_releases(releases: list) -> list:
    """Sort ModDB releases newest-version-first regardless of upload date.

    ModDB returns releases ordered by upload time, which interleaves
    when a maintainer alternates between branches (e.g. shipping a 0.5.x
    bugfix after a 1.0.x release). We sort by `modversion` so the file
    list always reads naturally — 1.0.7, 1.0.6, …, 1.0.0, 0.5.7, 0.5.6, …
    """
    if not releases:
        return releases
    try:
        return sorted(
            releases,
            key=lambda r: version_key(str(r.get("modversion") or "")),
            reverse=True,
        )
    except Exception:
        # Never let a sort bug break the UI — fall back to raw order.
        return list(releases)




def _build_mods_tab(app: 'ServerManagerApp', parent):
    """Mods tab hosts two sub-tabs: INSTALLED (local file manager) and
    BROWSE (online ModDB search + download).

    The Mods folder is configured in the Settings tab — both sub-tabs
    read from and write to it."""
    outer = tk.Frame(parent, bg=Theme.BG_PANEL)
    outer.pack(fill=tk.BOTH, expand=True)

    # Sub-notebook
    sub = ttk.Notebook(outer, style="Term.TNotebook")
    sub.pack(fill=tk.BOTH, expand=True, padx=4, pady=(6, 6))
    app._mods_subnotebook = sub

    installed_tab = tk.Frame(sub, bg=Theme.BG_PANEL)
    sub.add(installed_tab, text="INSTALLED")
    app._build_mods_installed_subtab(installed_tab)

    browse_tab = tk.Frame(sub, bg=Theme.BG_PANEL)
    sub.add(browse_tab, text="BROWSE")
    app._build_mods_browse_subtab(browse_tab)

# ------------------------------------------------------------------

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from VSSM import ServerManagerApp  # for type hints only

# Mods sub-tab — INSTALLED (original file manager)
# ------------------------------------------------------------------

def _build_mods_installed_subtab(app: 'ServerManagerApp', parent):
    pad = tk.Frame(parent, bg=Theme.BG_PANEL)
    pad.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    # Pack the action-button row FIRST, pinned to the bottom.
    btns = tk.Frame(pad, bg=Theme.BG_PANEL)
    btns.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
    # Tk var that the worker reads to decide whether to bypass the
    # on-disk update-check cache. Toggled by the "Force refresh"
    # checkbox below, AND temporarily by Shift-clicking Check Updates.
    if not hasattr(app, "_update_check_force_refresh_var"):
        app._update_check_force_refresh_var = tk.BooleanVar(value=False)

    for label, cmd, variant in [
        ("↻ Refresh",       app.load_mods,              "amber"),
        ("✓ Enable",        app.enable_selected_mod,    "start"),
        ("⏸ Disable",       app.disable_selected_mod,   "clear"),
        ("+ Add",           app.add_mod,                "amber"),
        ("✕ Remove",        app.remove_selected_mod,    "stop"),
        ("⟳ Check Updates", app.check_mod_updates,      "amber"),
        ("🌐 ModDB Page",   app.open_selected_mod_on_moddb,
                                                         "amber"),
    ]:
        btn = TermButton(btns, label, cmd,
                         variant=variant, font_spec=app.F_SMALL,
                         padx=8, pady=3)
        btn.pack(side=tk.LEFT, padx=2)
        # Shift+click on "Check Updates" → temporarily force a fresh
        # API hit for this run, even if the cache has fresh entries.
        # The flag auto-clears at the start of each check, so this
        # only affects the immediate run.
        if label.endswith("Check Updates"):
            def _shift_click_force(_e, _cmd=cmd):
                # Set both: the persistent var (which the toolbar
                # checkbox below reflects) and the immediate flag the
                # worker reads.
                try:
                    app._update_check_force_refresh_var.set(True)
                except Exception:
                    pass
                _cmd()
                # Auto-clear the var so subsequent ordinary clicks go
                # back to the cached fast path.
                try:
                    app.after(50, lambda:
                        app._update_check_force_refresh_var.set(False))
                except Exception:
                    pass
                return "break"  # don't fire the normal click handler
            btn.bind("<Shift-Button-1>", _shift_click_force)

    # Toolbar checkbox: persistent toggle for "force refresh on every
    # subsequent Check Updates click." Kept off by default (the cache
    # is the whole point); flick it on if you've just published a
    # release on ModDB and want every check to skip the cache for now.
    TermCheckbutton(
        btns, "Force refresh",
        app._update_check_force_refresh_var,
        font_spec=app.F_SMALL,
    ).pack(side=tk.LEFT, padx=(8, 2))

    # Listbox fills the middle.
    # Search row — sits above the listbox. Live-filters the cached
    # metadata (filename + modid + name + authors). Empty query shows
    # everything; case-insensitive substring match.
    search_row = tk.Frame(pad, bg=Theme.BG_PANEL)
    search_row.pack(fill=tk.X, pady=(2, 2))
    tk.Label(search_row, text="🔍",
              fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
              font=app.F_SMALL).pack(side=tk.LEFT, padx=(0, 4))
    if not hasattr(app, "_mod_search_var"):
        app._mod_search_var = tk.StringVar(value="")
    if not hasattr(app, "_mod_search_count_var"):
        app._mod_search_count_var = tk.StringVar(value="")
    search_entry = TermEntry(search_row,
                              textvariable=app._mod_search_var,
                              font_spec=app.F_SMALL)
    search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=2)
    # Clear button: one-click reset of the filter.
    def _clear_search(_e=None):
        try:
            app._mod_search_var.set("")
        except Exception:
            pass
    TermButton(search_row, "✕", _clear_search,
                variant="amber", font_spec=app.F_SMALL,
                padx=6, pady=1).pack(side=tk.LEFT, padx=(4, 0))
    # Result count label.
    tk.Label(search_row, textvariable=app._mod_search_count_var,
              fg=Theme.MUTED, bg=Theme.BG_PANEL,
              font=app.F_SMALL).pack(side=tk.LEFT, padx=(8, 0))
    # Live filter: rerun on every keystroke. The trace is registered
    # only once per app session — guarded by an attribute marker so
    # rebuilding the tab (theme switch, etc.) doesn't stack handlers.
    if not getattr(app, "_mod_search_trace_registered", False):
        app._mod_search_var.trace_add(
            "write", lambda *_: _apply_mod_filter(app))
        app._mod_search_trace_registered = True

    list_wrap = tk.Frame(pad, bg=Theme.BORDER)
    list_wrap.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
    list_inner = tk.Frame(list_wrap, bg=Theme.BG_INPUT)
    list_inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

    app.mod_listbox = tk.Listbox(list_inner,
                                   bg=Theme.BG_INPUT, fg=Theme.AMBER,
                                   selectbackground=Theme.BG_SELECT,
                                   selectforeground=Theme.AMBER_GLOW,
                                   font=app.F_CONSOLE,
                                   bd=0, highlightthickness=0,
                                   activestyle='none')
    app.mod_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    msb = ttk.Scrollbar(list_inner, orient=tk.VERTICAL,
                        style="Term.Vertical.TScrollbar",
                        command=app.mod_listbox.yview)
    msb.pack(side=tk.RIGHT, fill=tk.Y)
    app.mod_listbox.configure(yscrollcommand=msb.set)

# ------------------------------------------------------------------
# Mods sub-tab — BROWSE (online ModDB)
# ------------------------------------------------------------------

def _build_mods_browse_subtab(app: 'ServerManagerApp', parent):
    """Left column: search + filters + results list.
    Right column: mod details + file picker + install controls."""
    root = tk.Frame(parent, bg=Theme.BG_PANEL)
    root.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
    root.columnconfigure(0, weight=3, uniform="mb")
    root.columnconfigure(1, weight=4, uniform="mb")
    root.rowconfigure(0, weight=1)

    left = tk.Frame(root, bg=Theme.BG_PANEL)
    left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
    app._build_mods_browse_left(left)

    right = tk.Frame(root, bg=Theme.BG_PANEL)
    right.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
    app._build_mods_browse_right(right)

    # Status strip pinned along the bottom of the sub-tab.
    status_wrap = tk.Frame(parent, bg=Theme.BG_HEADER)
    status_wrap.pack(side=tk.BOTTOM, fill=tk.X)
    tk.Label(status_wrap, textvariable=app.moddb_status_var,
             fg=Theme.AMBER_DIM, bg=Theme.BG_HEADER,
             font=app.F_SMALL, anchor=tk.W,
             padx=12, pady=4).pack(fill=tk.X)

def _build_mods_browse_left(app: 'ServerManagerApp', parent):
    # Search row
    search_row = tk.Frame(parent, bg=Theme.BG_PANEL)
    search_row.pack(fill=tk.X, pady=(0, 4))
    tk.Label(search_row, text="SEARCH:",
             fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
             font=app.F_SMALL).pack(side=tk.LEFT)
    TermEntry(search_row, textvariable=app.moddb_search_var,
              font_spec=app.F_NORMAL).pack(side=tk.LEFT, fill=tk.X,
                                            expand=True, padx=6, ipady=2)
    TermButton(search_row, "Clear",
               lambda: (app.moddb_search_var.set(''),
                        app._moddb_selected_tagids.clear(),
                        app._refresh_tag_button_styles(),
                        app._schedule_moddb_search()),
               variant="amber", font_spec=app.F_SMALL,
               padx=10, pady=2).pack(side=tk.LEFT)
    app.moddb_search_var.trace_add(
        'write', lambda *a: app._schedule_moddb_search())

    # Sort + side + gv filters
    opts = tk.Frame(parent, bg=Theme.BG_PANEL)
    opts.pack(fill=tk.X, pady=(0, 4))

    tk.Label(opts, text="SORT:",
             fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
             font=app.F_SMALL).grid(row=0, column=0, sticky=tk.W,
                                     padx=(0, 4), pady=2)
    sort_cb = ttk.Combobox(opts, textvariable=app.moddb_sort_var,
                           values=[
                               "trendingpoints",  # trending
                               "downloads",       # most downloaded
                               "asset.created",   # newest
                               "lastreleased",    # recently updated
                               "comments",
                               "follows",
                           ],
                           state="readonly", width=16,
                           style="Term.TCombobox",
                           font=app.F_SMALL)
    sort_cb.grid(row=0, column=1, sticky=tk.W, pady=2)
    sort_cb.bind("<<ComboboxSelected>>",
                 lambda e: app._schedule_moddb_search(immediate=True))

    tk.Label(opts, text="SIDE:",
             fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
             font=app.F_SMALL).grid(row=0, column=2, sticky=tk.W,
                                     padx=(10, 4), pady=2)
    side_cb = ttk.Combobox(opts, textvariable=app.moddb_side_var,
                           values=[
                               "server_compat",  # Server + Both (default)
                               "server_only",    # strict Server only
                               "all",
                           ],
                           state="readonly", width=14,
                           style="Term.TCombobox",
                           font=app.F_SMALL)
    side_cb.grid(row=0, column=3, sticky=tk.W, pady=2)
    side_cb.bind("<<ComboboxSelected>>",
                 lambda e: app._rerender_moddb_results())

    tk.Label(opts, text="GAME VER:",
             fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
             font=app.F_SMALL).grid(row=1, column=0, sticky=tk.W,
                                     padx=(0, 4), pady=2)
    app.moddb_gv_combo = ttk.Combobox(opts, textvariable=app.moddb_gv_var,
                                       values=[""], state="readonly",
                                       style="Term.TCombobox",
                                       font=app.F_SMALL)
    app.moddb_gv_combo.grid(row=1, column=1, columnspan=3,
                             sticky=tk.EW, pady=2)
    app.moddb_gv_combo.bind("<<ComboboxSelected>>",
                             lambda e: app._schedule_moddb_search(immediate=True))
    opts.columnconfigure(3, weight=1)

    # --- Tag chips (collapsible) ---------------------------------
    # The outer header gets a "Clear tags" action button on the right.
    # The body contains a fixed-height scrollable flow of chip buttons
    # that's populated asynchronously from /api/tags.
    _, tag_body = collapsible_section(
        parent, "TAGS", font_spec=app.F_SMALL,
        pady=(4, 2),
        right_widget_factory=lambda hdr:
            TermButton(hdr, "Clear tags",
                       app._clear_moddb_tags,
                       variant="amber", font_spec=app.F_SMALL,
                       padx=6, pady=1).pack(side=tk.RIGHT))
    tag_wrap = tk.Frame(tag_body, bg=Theme.BORDER, height=72)
    tag_wrap.pack(fill=tk.X, pady=(0, 6))
    tag_wrap.pack_propagate(False)
    app.moddb_tag_scroll = ScrollableFrame(tag_wrap, bg=Theme.BG_INPUT)
    app.moddb_tag_scroll.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
    app._moddb_tag_body_note = tk.Label(
        app.moddb_tag_scroll.body,
        text="Loading tags…",
        fg=Theme.AMBER_FAINT, bg=Theme.BG_INPUT,
        font=app.F_SMALL, padx=6, pady=6)
    app._moddb_tag_body_note.pack(anchor=tk.W)

    # --- Results list (collapsible) ------------------------------
    # We DON'T collapse RESULTS by default — that's the main reason
    # users opened this tab. The arrow is just a nice-to-have for
    # users who want more vertical room for tags.
    _, results_body = collapsible_section(
        parent, "RESULTS", font_spec=app.F_SMALL,
        pady=(4, 2),
        right_widget_factory=lambda hdr:
            TermButton(hdr, "↻ Refresh",
                       lambda: app._schedule_moddb_search(immediate=True),
                       variant="amber", font_spec=app.F_SMALL,
                       padx=6, pady=1).pack(side=tk.RIGHT))
    # Results body should expand to fill remaining space — default
    # pack in collapsible_section uses fill=tk.X, so we repack:
    results_body.pack_configure(fill=tk.BOTH, expand=True)

    res_wrap = tk.Frame(results_body, bg=Theme.BORDER)
    res_wrap.pack(fill=tk.BOTH, expand=True)
    res_inner = tk.Frame(res_wrap, bg=Theme.BG_INPUT)
    res_inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

    app.moddb_results_text = tk.Text(res_inner,
                                      bg=Theme.BG_INPUT, fg=Theme.AMBER,
                                      font=app.F_CONSOLE,
                                      bd=0, highlightthickness=0,
                                      wrap=tk.NONE,
                                      cursor="arrow",
                                      state='disabled',
                                      padx=6, pady=6)
    app.moddb_results_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    rsb = ttk.Scrollbar(res_inner, orient=tk.VERTICAL,
                        style="Term.Vertical.TScrollbar",
                        command=app.moddb_results_text.yview)
    rsb.pack(side=tk.RIGHT, fill=tk.Y)
    app.moddb_results_text.configure(yscrollcommand=rsb.set)

    t = app.moddb_results_text
    t.tag_configure("row",       foreground=Theme.AMBER)
    t.tag_configure("row_name",  foreground=Theme.AMBER_GLOW)
    t.tag_configure("row_meta",  foreground=Theme.AMBER_DIM)
    t.tag_configure("side_srv",  foreground=Theme.GREEN)
    t.tag_configure("side_both", foreground=Theme.CYAN)
    t.tag_configure("side_cli",  foreground=Theme.RED)
    t.tag_configure("side_unk",  foreground=Theme.AMBER_FAINT)
    t.tag_configure("selected",
                    background=Theme.BG_SELECT,
                    foreground=Theme.AMBER_GLOW)
    t.tag_bind("row", "<Button-1>", app._on_moddb_row_click)
    t.tag_bind("row", "<Enter>",
               lambda e: t.config(cursor="hand2"))
    t.tag_bind("row", "<Leave>",
               lambda e: t.config(cursor="arrow"))

    app._moddb_row_index = {}  # line-number -> result index
    app._moddb_selected_row = None

def _build_mods_browse_right(app: 'ServerManagerApp', parent):
    """Right column layout, bottom-up so the install/cancel buttons are
    always visible regardless of font scaling:

        ┌─────────────────────────────┐
        │ DETAILS    (▸ collapsible)  │ ← top of a vertical PanedWindow
        ├─────────────────────────────┤
        │ FILES      (▸ collapsible)  │ ← bottom half
        └─────────────────────────────┘
        PROGRESS: ...                   ← pinned bottom
        [bar]
        [⬇ Install] [■ Cancel]          ← pinned bottom

    We pack the bottom-pinned widgets FIRST (side=tk.BOTTOM) so the
    upper vertical PanedWindow gets the squeeze. This mirrors the
    pattern used by the INSTALLED sub-tab and guarantees the install
    button can never be clipped.
    """
    # --- Bottom-pinned install / cancel row (pack first!) ---------
    btn_row = tk.Frame(parent, bg=Theme.BG_PANEL)
    btn_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
    app.moddb_install_btn = TermButton(
        btn_row, "⬇ Install", app._install_current_file,
        variant="start", font_spec=app.F_SMALL, padx=10, pady=4)
    app.moddb_install_btn.pack(side=tk.LEFT, padx=(0, 6))
    app.moddb_cancel_btn = TermButton(
        btn_row, "■ Cancel", app._cancel_moddb_download,
        variant="stop", font_spec=app.F_SMALL, padx=10, pady=4)
    app.moddb_cancel_btn.pack(side=tk.LEFT)
    app.moddb_cancel_btn.set_enabled(False)

    # --- Bottom-pinned progress bar -------------------------------
    bar_bg = tk.Frame(parent, bg=Theme.BORDER, height=8,
                      highlightthickness=0, bd=0)
    bar_bg.pack(side=tk.BOTTOM, fill=tk.X, pady=(2, 4))
    bar_inner = tk.Frame(bar_bg, bg=Theme.DIVIDER,
                         highlightthickness=0, bd=0)
    bar_inner.place(relx=0, rely=0, relwidth=1, relheight=1,
                    x=1, y=1, width=-2, height=-2)
    app.moddb_progress_fill = tk.Frame(bar_inner, bg=Theme.GREEN,
                                        highlightthickness=0, bd=0)
    app.moddb_progress_fill.place(relx=0, rely=0, relwidth=0, relheight=1)

    # --- Bottom-pinned progress text ------------------------------
    prog_row = tk.Frame(parent, bg=Theme.BG_PANEL)
    prog_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(2, 0))
    tk.Label(prog_row, text="PROGRESS:",
             fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
             font=app.F_SMALL).pack(side=tk.LEFT)
    app.moddb_progress_var = tk.StringVar(value="— idle —")
    tk.Label(prog_row, textvariable=app.moddb_progress_var,
             fg=Theme.AMBER, bg=Theme.BG_PANEL,
             font=app.F_SMALL).pack(side=tk.LEFT, padx=6)

    # --- Expanding area: Details + Files in a vertical PanedWindow -
    upper = ttk.PanedWindow(parent, orient=tk.VERTICAL,
                            style="Term.TPanedwindow")
    upper.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    # ---- DETAILS pane --------------------------------------------
    det_pane = tk.Frame(upper, bg=Theme.BG_PANEL)
    upper.add(det_pane, weight=3)

    det_hdr, det_body = collapsible_section(
        det_pane, "DETAILS", font_spec=app.F_SMALL,
        pady=(0, 2),
        right_widget_factory=lambda hdr:
            TermButton(hdr, "Open on ModDB",
                       app._open_current_mod_in_browser,
                       variant="amber", font_spec=app.F_SMALL,
                       padx=6, pady=1).pack(side=tk.RIGHT))
    det_wrap = tk.Frame(det_body, bg=Theme.BORDER)
    det_wrap.pack(fill=tk.BOTH, expand=True)
    det_inner = tk.Frame(det_wrap, bg=Theme.BG_INPUT)
    det_inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
    app.moddb_details_text = tk.Text(det_inner,
                                      bg=Theme.BG_INPUT,
                                      fg=Theme.AMBER_DIM,
                                      font=app.F_CONSOLE,
                                      bd=0, highlightthickness=0,
                                      wrap=tk.WORD,
                                      state='disabled',
                                      padx=8, pady=6)
    app.moddb_details_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    dsb = ttk.Scrollbar(det_inner, orient=tk.VERTICAL,
                        style="Term.Vertical.TScrollbar",
                        command=app.moddb_details_text.yview)
    dsb.pack(side=tk.RIGHT, fill=tk.Y)
    app.moddb_details_text.configure(yscrollcommand=dsb.set)
    d = app.moddb_details_text
    d.tag_configure("title", foreground=Theme.CYAN, font=app.F_HDR)
    d.tag_configure("meta",  foreground=Theme.AMBER_DIM)
    d.tag_configure("body",  foreground=Theme.AMBER)
    d.tag_configure("side_srv",  foreground=Theme.GREEN)
    d.tag_configure("side_both", foreground=Theme.CYAN)
    d.tag_configure("side_cli",  foreground=Theme.RED)
    d.tag_configure("side_unk",  foreground=Theme.AMBER_FAINT)

    # ---- FILES pane ----------------------------------------------
    files_pane = tk.Frame(upper, bg=Theme.BG_PANEL)
    upper.add(files_pane, weight=2)

    fhdr, fbody = collapsible_section(
        files_pane, "FILES / VERSIONS", font_spec=app.F_SMALL,
        pady=(4, 2))
    file_wrap = tk.Frame(fbody, bg=Theme.BORDER)
    file_wrap.pack(fill=tk.BOTH, expand=True)
    file_inner = tk.Frame(file_wrap, bg=Theme.BG_INPUT)
    file_inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
    app.moddb_files_text = tk.Text(file_inner,
                                    bg=Theme.BG_INPUT, fg=Theme.AMBER,
                                    font=app.F_CONSOLE,
                                    bd=0, highlightthickness=0,
                                    wrap=tk.NONE,
                                    cursor="arrow",
                                    state='disabled',
                                    padx=6, pady=4)
    app.moddb_files_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    fsb = ttk.Scrollbar(file_inner, orient=tk.VERTICAL,
                        style="Term.Vertical.TScrollbar",
                        command=app.moddb_files_text.yview)
    fsb.pack(side=tk.RIGHT, fill=tk.Y)
    app.moddb_files_text.configure(yscrollcommand=fsb.set)

    ft = app.moddb_files_text
    ft.tag_configure("file",      foreground=Theme.AMBER)
    ft.tag_configure("file_best", foreground=Theme.GREEN)
    ft.tag_configure("file_meta", foreground=Theme.AMBER_DIM)
    ft.tag_configure("selected",
                     background=Theme.BG_SELECT,
                     foreground=Theme.AMBER_GLOW)
    ft.tag_bind("file", "<Button-1>", app._on_moddb_file_click)
    ft.tag_bind("file_best", "<Button-1>", app._on_moddb_file_click)
    ft.tag_bind("file", "<Enter>",
                lambda e: ft.config(cursor="hand2"))
    ft.tag_bind("file", "<Leave>",
                lambda e: ft.config(cursor="arrow"))
    app._moddb_file_rows = {}  # line -> file dict
    app._moddb_file_selected_row = None

# ------------------------------------------------------------------
# Config tab
# ------------------------------------------------------------------

def load_mods(app: 'ServerManagerApp'):
    """Refresh the installed mod list.

    Reads every mod file's modinfo.json into `app._mod_metadata_cache`
    (filename -> info dict from LocalModInspector). Then applies the
    current search filter to repopulate the listbox.

    The cache is what powers the live search box above the listbox —
    the filter never re-parses, only this function does. Add / Remove /
    Enable / Disable all call back through here, so freshly-changed
    mods are reflected on the next render."""
    d = app.mods_folder_var.get()
    if not d or not os.path.isdir(d):
        # Empty cache so a stale list from a previous folder doesn't
        # linger after the user clears or breaks the path.
        app._mod_metadata_cache = {}
        try:
            app.mod_listbox.delete(0, tk.END)
        except Exception:
            pass
        _apply_mod_filter(app)
        return

    cache: dict = {}
    for f in sorted(os.listdir(d)):
        if not f.lower().endswith(('.jar', '.zip', '.dll', '.disabled')):
            continue
        full = os.path.join(d, f)
        try:
            info = LocalModInspector.read_mod_file(full)
        except Exception:
            # If reading throws (corrupt zip, race condition), still
            # show the file by filename. The filter will fall back to
            # filename-only matching for entries with no info.
            info = {"name": f, "modid": None, "version": None,
                    "side": None, "path": full,
                    "dependencies": {}, "error": "read failed"}
        cache[f] = info
    app._mod_metadata_cache = cache
    _apply_mod_filter(app)


def _apply_mod_filter(app: 'ServerManagerApp'):
    """Push the cached mod list through the current search query into
    the listbox. Called from load_mods() after a fresh parse, AND from
    the search-Entry trace whenever the user types.

    Match rules:
      - empty / whitespace query -> show everything
      - otherwise: case-insensitive substring against the union of
        the filename, modid, display name, and each author name
      - mods that failed to parse fall back to filename-only matching
    """
    cache: dict = getattr(app, "_mod_metadata_cache", {}) or {}
    query = ""
    try:
        query = (app._mod_search_var.get() or "").strip().lower()
    except Exception:
        query = ""

    # Preserve the user's current selection across filter changes
    # when possible — if their previously-selected file is still in
    # the new visible list, re-select it.
    prior_selection = None
    try:
        sel = app.mod_listbox.curselection()
        if sel:
            prior_selection = app.mod_listbox.get(sel[0])
    except Exception:
        pass

    matches: list[str] = []
    if not query:
        matches = sorted(cache.keys())
    else:
        for fn, info in cache.items():
            haystacks = [fn]
            if info:
                modid = info.get("modid")
                if modid:
                    haystacks.append(str(modid))
                name = info.get("name")
                # info["name"] defaults to the filename when the mod
                # had no modinfo, so we only add it when it differs.
                if name and name != fn:
                    haystacks.append(str(name))
                authors = info.get("authors") or []
                if isinstance(authors, list):
                    for a in authors:
                        if a:
                            haystacks.append(str(a))
                # Some legacy modinfo files have a singular "author"
                # field instead of "authors" (a list).
                author = info.get("author")
                if author:
                    haystacks.append(str(author))
            blob = " ".join(haystacks).lower()
            if query in blob:
                matches.append(fn)
        matches.sort()

    try:
        app.mod_listbox.delete(0, tk.END)
        for fn in matches:
            app.mod_listbox.insert(tk.END, fn)
    except Exception:
        return

    # Restore selection if possible.
    if prior_selection and prior_selection in matches:
        try:
            idx = matches.index(prior_selection)
            app.mod_listbox.selection_set(idx)
            app.mod_listbox.see(idx)
        except (ValueError, Exception):
            pass

    # Update the result-count label if it exists (built alongside the
    # search Entry — see _build_mods_installed_subtab).
    try:
        total = len(cache)
        shown = len(matches)
        if query:
            app._mod_search_count_var.set(f"{shown}/{total} match")
        else:
            app._mod_search_count_var.set(f"{total} mod{'s' if total != 1 else ''}")
    except Exception:
        pass

def _selected_mod(app: 'ServerManagerApp'):
    sel = app.mod_listbox.curselection()
    if not sel:
        return None
    return app.mod_listbox.get(sel[0])

def enable_selected_mod(app: 'ServerManagerApp'):
    mod = app._selected_mod()
    if not mod or not mod.endswith('.disabled'):
        return
    if not app._mod_op_ok("enable"):
        return
    d = app.mods_folder_var.get()
    new_name = mod[:-9]
    try:
        os.rename(os.path.join(d, mod), os.path.join(d, new_name))
        app.load_mods()
    except Exception as e:
        app._notify(f"Enable failed: {e}", level="error")

def disable_selected_mod(app: 'ServerManagerApp'):
    mod = app._selected_mod()
    if not mod or mod.endswith('.disabled'):
        return
    if not app._mod_op_ok("disable"):
        return
    d = app.mods_folder_var.get()
    try:
        os.rename(os.path.join(d, mod),
                  os.path.join(d, f"{mod}.disabled"))
        app.load_mods()
    except Exception as e:
        app._notify(f"Disable failed: {e}", level="error")

def add_mod(app: 'ServerManagerApp'):
    if not app._mod_op_ok("add"):
        return
    path = filedialog.askopenfilename(
        title="Select Mod File",
        initialdir=os.path.dirname(app.mods_folder_var.get()) or os.getcwd(),
        filetypes=[("Mod files", "*.jar *.zip *.dll")])
    if not path:
        return
    d = app.mods_folder_var.get()
    if not d or not os.path.isdir(d):
        app._notify("Set a valid Mods folder first.", level="warn")
        return
    try:
        shutil.copy2(path, d)
        app.load_mods()
    except Exception as e:
        app._notify(f"Add mod failed: {e}", level="error")

def remove_selected_mod(app: 'ServerManagerApp'):
    mod = app._selected_mod()
    if not mod:
        return
    if not app._mod_op_ok("remove"):
        return
    if not messagebox.askyesno("Remove Mod",
                               f"Delete '{mod}' from the Mods folder?"):
        return
    try:
        os.remove(os.path.join(app.mods_folder_var.get(), mod))
        app.load_mods()
    except Exception as e:
        app._notify(f"Remove failed: {e}", level="error")

# ------------------------------------------------------------------
# ModDB page lookup
# ------------------------------------------------------------------
# Open the selected installed mod's page on mods.vintagestory.at.
#
# The mod file's modinfo.json gives us the canonical modid string
# (the same one the mod author uses on ModDB). We hit the API in a
# background thread to resolve that to the canonical URL — this
# gives us the right page even if the mod author chose a different
# "urlalias" on ModDB than the local modid. If the API call fails
# (offline, mod not on ModDB, etc.) we fall back to a direct
# https://mods.vintagestory.at/{modid} URL, which will work for
# the common case where the urlalias matches the modid.
# ------------------------------------------------------------------

def open_selected_mod_on_moddb(app: 'ServerManagerApp'):
    filename = app._selected_mod()
    if not filename:
        app._notify("Select a mod first.", level="info")
        return
    full_path = os.path.join(app.mods_folder_var.get(), filename)
    if not os.path.exists(full_path):
        app._notify("Selected mod file is missing.", level="error")
        return

    info = LocalModInspector.read_mod_file(full_path)
    modid = info.get("modid") if info else None
    if not modid:
        app._notify(
            f"Could not read modinfo.json from '{filename}' — "
            "no modid available to look up.",
            level="error")
        return

    # Lookup goes off-thread; user gets a brief "looking up" toast
    # so they know the click registered.
    app._notify(f"Looking up '{modid}' on ModDB…",
                 level="info", duration_ms=2500)
    t = threading.Thread(
        target=app._open_moddb_worker,
        args=(modid, filename),
        daemon=True)
    t.start()

def _open_moddb_worker(app: 'ServerManagerApp', modid, filename):
    """Background: resolve the canonical ModDB URL for `modid`,
    then open it in the user's browser. UI work is marshalled
    back to the Tk thread via app.after(0, ...)."""
    url = None
    try:
        detail = app.moddb.get_mod(modid)
        if detail:
            if detail.get("urlalias"):
                url = f"{app.moddb.SITE_BASE}/{detail['urlalias']}"
            elif detail.get("assetid"):
                url = f"{app.moddb.SITE_BASE}/show/mod/{detail['assetid']}"
    except Exception as e:
        LOG.info("ModDB lookup for %r failed: %s — falling back to "
                 "direct URL", modid, e)

    # Fallback: a direct /{modid} link works for the common case
    # where the mod's urlalias on ModDB is the same as its modid.
    if not url:
        url = f"{app.moddb.SITE_BASE}/{urllib.parse.quote(str(modid))}"

    app.after(0, app._open_url_in_browser, url, filename)

def _open_url_in_browser(app: 'ServerManagerApp', url, label):
    """Open a URL in the default browser. Runs on the Tk thread."""
    try:
        import webbrowser
        webbrowser.open(url)
        app.append_console(f"Opened ModDB page for '{label}': {url}",
                            "system")
    except Exception as e:
        LOG.exception("webbrowser.open failed for %s", url)
        app._notify(f"Could not open browser: {e}", level="error")

def _mod_op_ok(app: 'ServerManagerApp', op_label: str)-> bool:
    """Guard against mod file ops while the server is running
    (improvement #28). On Windows, VS holds zip/dll files open and
    the OS would raise PermissionError — better UX to refuse up front.
    Linux is more permissive but changes won't take effect until
    restart, so we still warn."""
    if not app.is_running:
        return True
    if not messagebox.askyesno(
            "Server running",
            f"The server is running.\n\n"
            f"Modding files while the server has them open can fail "
            f"(especially on Windows) and mod changes won't take "
            f"effect until the server restarts.\n\n"
            f"Proceed with {op_label} anyway?"):
        return False
    return True

# ==================================================================
# ModDB Browser — online search, download, install
# ==================================================================
#
# All HTTP runs on worker threads; all UI updates are scheduled back
# onto the Tk main thread via app.after(). We keep a monotonically
# increasing request sequence so late-arriving responses from stale
# searches (user kept typing) get discarded.
#
# The "side" gate on download:
#   * Server, Universal/Both            → download without prompt
#   * Client                            → red confirm dialog required
#   * missing / unrecognized            → amber confirm dialog required
# ==================================================================

def init_moddb_catalogs_async(app: 'ServerManagerApp'):
    """Fetch /tags and /gameversions in the background, then populate
    the tag chips + game-version dropdown. Safe to call multiple times
    — we only actually fetch once per app session."""
    if getattr(app, "_moddb_catalogs_loaded", False):
        return
    app._set_moddb_status("Loading ModDB catalogs…")
    t = threading.Thread(target=app._moddb_catalogs_worker, daemon=True)
    t.start()

def _moddb_catalogs_worker(app: 'ServerManagerApp'):
    error = None
    tags = []
    gvs = []
    try:
        tags = app.moddb.get_tags()
        gvs = app.moddb.get_gameversions()
    except Exception as e:
        error = str(e)
    app.after(0, app._moddb_apply_catalogs, tags, gvs, error)

def _moddb_apply_catalogs(app: 'ServerManagerApp', tags, gvs, error):
    if error:
        app._set_moddb_status(f"ModDB offline: {error}")
        app._moddb_tag_body_note.configure(
            text=f"Could not load tags ({error}). "
                 f"Retry from ↻ Refresh.")
        return

    app._moddb_catalogs_loaded = True

    # Populate the game-version combobox. Prepend "" = any. The API
    # returns game versions newest-first; flip that so the dropdown
    # lists them in the opposite order (oldest first). "(any)" stays
    # pinned at the top since it's a mode, not a version.
    gv_values = [""]
    gv_labels = ["(any)"]
    app._moddb_gv_map = {"": None}   # display_label -> gv id or None
    for gv in reversed(gvs):
        name = str(gv.get("name") or gv.get("displayname") or "")
        gvid = gv.get("tagid") or gv.get("id")
        if name and gvid:
            label = name
            gv_labels.append(label)
            gv_values.append(label)
            app._moddb_gv_map[label] = gvid
    app.moddb_gv_combo.configure(values=gv_labels)
    app.moddb_gv_var.set("(any)")

    # Build tag chips
    for child in list(app.moddb_tag_scroll.body.winfo_children()):
        child.destroy()
    app._moddb_tag_buttons.clear()
    flow = tk.Frame(app.moddb_tag_scroll.body, bg=Theme.BG_INPUT)
    flow.pack(fill=tk.X, padx=4, pady=4)
    # We use a simple wrap-as-you-go layout: every chip is packed to
    # the left; tk's flow isn't auto, so we break rows manually based
    # on an estimated width budget.
    row_frame = tk.Frame(flow, bg=Theme.BG_INPUT)
    row_frame.pack(fill=tk.X, anchor=tk.W)
    row_budget = 0
    # Chip width budget is approximate — good enough for visual flow.
    ROW_MAX = 520
    for tag in tags:
        tagid = tag.get("tagid") or tag.get("id")
        name = str(tag.get("name") or "").strip()
        if not tagid or not name:
            continue
        est = max(60, 14 + 8 * len(name))
        if row_budget + est > ROW_MAX:
            row_frame = tk.Frame(flow, bg=Theme.BG_INPUT)
            row_frame.pack(fill=tk.X, anchor=tk.W)
            row_budget = 0
        btn = TermButton(row_frame, name,
                         lambda tid=tagid: app._toggle_moddb_tag(tid),
                         variant="amber", font_spec=app.F_SMALL,
                         padx=6, pady=1)
        btn.pack(side=tk.LEFT, padx=2, pady=2)
        app._moddb_tag_buttons[tagid] = btn
        row_budget += est + 4

    app._set_moddb_status(
        f"Loaded {len(tags)} tags, {len(gvs)} game versions. Ready.")
    # Kick off the first search automatically so the tab isn't blank.
    app._schedule_moddb_search(immediate=True)

def _toggle_moddb_tag(app: 'ServerManagerApp', tagid):
    if tagid in app._moddb_selected_tagids:
        app._moddb_selected_tagids.discard(tagid)
    else:
        app._moddb_selected_tagids.add(tagid)
    app._refresh_tag_button_styles()
    app._schedule_moddb_search(immediate=True)

def _refresh_tag_button_styles(app: 'ServerManagerApp'):
    # Highlight selected chips by toggling the fg/bg on the Label.
    for tagid, btn in app._moddb_tag_buttons.items():
        if tagid in app._moddb_selected_tagids:
            btn.configure(fg=Theme.AMBER_GLOW, bg=Theme.BG_SELECT)
            btn._bg = Theme.BG_SELECT
            btn._bg_hover = Theme.BG_SELECT
        else:
            btn.configure(fg=Theme.AMBER, bg=Theme.BG_BTN_AMBER)
            btn._bg = Theme.BG_BTN_AMBER
            btn._bg_hover = Theme.BG_BTN_AMBER_HOVER

def _clear_moddb_tags(app: 'ServerManagerApp'):
    if not app._moddb_selected_tagids:
        return
    app._moddb_selected_tagids.clear()
    app._refresh_tag_button_styles()
    app._schedule_moddb_search(immediate=True)

# --- search scheduling -------------------------------------------

def _schedule_moddb_search(app: 'ServerManagerApp', immediate=False):
    """Debounce the search — typing fires this many times a second,
    but we only want to hit the API once the user pauses."""
    if app._moddb_search_job is not None:
        try:
            app.after_cancel(app._moddb_search_job)
        except Exception:
            pass
        app._moddb_search_job = None
    delay = 0 if immediate else 400
    app._moddb_search_job = app.after(delay, app._run_moddb_search)

def _run_moddb_search(app: 'ServerManagerApp'):
    app._moddb_search_job = None
    if not getattr(app, "_moddb_catalogs_loaded", False):
        app.init_moddb_catalogs_async()
        return
    app._moddb_request_seq += 1
    seq = app._moddb_request_seq
    text = app.moddb_search_var.get().strip() or None
    tagids = sorted(app._moddb_selected_tagids) or None
    gv_label = app.moddb_gv_var.get()
    gv_id = app._moddb_gv_map.get(gv_label) if hasattr(app, "_moddb_gv_map") else None
    orderby = app.moddb_sort_var.get() or "trendingpoints"
    app._set_moddb_status("Searching ModDB…")
    t = threading.Thread(
        target=app._moddb_search_worker,
        args=(seq, text, tagids, gv_id, orderby),
        daemon=True)
    t.start()

def _moddb_search_worker(app: 'ServerManagerApp', seq, text, tagids, gv_id, orderby):
    try:
        mods = app.moddb.search_mods(
            text=text, tagids=tagids, gameversion=gv_id,
            orderby=orderby, orderdirection="desc")
        error = None
    except Exception as e:
        mods, error = [], str(e)
    app.after(0, app._moddb_apply_search, seq, mods, error)

def _moddb_apply_search(app: 'ServerManagerApp', seq, mods, error):
    # Ignore stale responses.
    if seq != app._moddb_request_seq:
        return
    if error:
        app._set_moddb_status(f"Search failed: {error}")
        app._moddb_results = []
    else:
        app._moddb_results = mods or []
    app._rerender_moddb_results()

def _rerender_moddb_results(app: 'ServerManagerApp'):
    """Rewrite the results text widget from app._moddb_results,
    honoring the current side-filter setting."""
    t = app.moddb_results_text
    t.configure(state='normal')
    t.delete("1.0", tk.END)
    app._moddb_row_index.clear()
    app._moddb_selected_row = None

    side_mode = app.moddb_side_var.get()
    shown = 0
    hidden_by_side = 0
    for i, mod in enumerate(app._moddb_results):
        side = app._normalize_side(mod.get("side"))
        if side_mode == "server_compat" and side == "client":
            hidden_by_side += 1
            continue
        if side_mode == "server_only" and side != "server":
            hidden_by_side += 1
            continue
        shown += 1

        # Row rendering — one logical row spans two visible lines.
        row_start = t.index("end-1c")
        row_num = int(row_start.split('.')[0])

        side_tag, side_label = app._side_badge(side)
        name = str(mod.get("name") or mod.get("modid") or "(unnamed)")
        t.insert(tk.END, f"  [{side_label:^6}]", (side_tag, "row"))
        t.insert(tk.END, f"  {name}\n", ("row_name", "row"))
        author = str(mod.get("author") or "?")
        downloads = mod.get("downloads") or 0
        summary = str(mod.get("summary") or "").strip()
        if len(summary) > 90:
            summary = summary[:87] + "…"
        meta = f"         by {author} · {downloads} downloads"
        if summary:
            meta += f" · {summary}"
        t.insert(tk.END, meta + "\n", ("row_meta", "row"))
        t.insert(tk.END, "\n", ("row",))

        # Index BOTH lines of the row so a click on either selects.
        for r in (row_num, row_num + 1, row_num + 2):
            app._moddb_row_index[r] = i

    if shown == 0:
        if app._moddb_results:
            t.insert(tk.END,
                     f"\n  All {hidden_by_side} result(s) hidden by "
                     f"side filter ('{side_mode}').\n",
                     ("row_meta",))
        else:
            t.insert(tk.END, "\n  No results.\n", ("row_meta",))

    t.configure(state='disabled')

    # Status strip summary
    if app._moddb_results:
        filt_bits = []
        if side_mode != "all":
            filt_bits.append(f"side={side_mode}")
        gv_label = app.moddb_gv_var.get()
        if gv_label and gv_label != "(any)":
            filt_bits.append(f"gv={gv_label}")
        if app._moddb_selected_tagids:
            filt_bits.append(f"{len(app._moddb_selected_tagids)} tag(s)")
        filt_desc = ", ".join(filt_bits) if filt_bits else "no filters"
        app._set_moddb_status(
            f"{shown} shown / {len(app._moddb_results)} results · {filt_desc}")

def _on_moddb_row_click(app: 'ServerManagerApp', event):
    row = int(app.moddb_results_text.index(f"@{event.x},{event.y}")
              .split('.')[0])
    idx = app._moddb_row_index.get(row)
    if idx is None:
        return
    # Highlight the whole logical row (three lines: header + meta + blank)
    t = app.moddb_results_text
    t.configure(state='normal')
    t.tag_remove("selected", "1.0", tk.END)
    # Find the header line for this idx
    header_row = None
    for r, i in app._moddb_row_index.items():
        if i == idx and (header_row is None or r < header_row):
            header_row = r
    if header_row is not None:
        t.tag_add("selected",
                  f"{header_row}.0", f"{header_row + 2}.end")
    t.configure(state='disabled')
    app._moddb_selected_row = idx
    app._load_mod_details_async(app._moddb_results[idx])

def _load_mod_details_async(app: 'ServerManagerApp', stub):
    mod_id = stub.get("modid") or stub.get("assetid")
    if not mod_id:
        return
    app._set_moddb_status(f"Loading details for {stub.get('name') or mod_id}…")
    seq = app._moddb_request_seq
    t = threading.Thread(
        target=app._mod_detail_worker,
        args=(seq, mod_id, stub),
        daemon=True)
    t.start()

def _mod_detail_worker(app: 'ServerManagerApp', seq, mod_id, stub):
    try:
        detail = app.moddb.get_mod(mod_id)
        error = None
    except Exception as e:
        detail, error = None, str(e)
    app.after(0, app._apply_mod_detail, seq, stub, detail, error)

def _apply_mod_detail(app: 'ServerManagerApp', seq, stub, detail, error):
    if seq != app._moddb_request_seq:
        return
    if error or not detail:
        app._set_moddb_status(f"Detail load failed: {error}")
        app._render_mod_detail(None, fallback_stub=stub, error=error)
        return
    # Sort releases once at the cache boundary so every downstream
    # reader (renderer, click handler, install pipeline, update
    # checker) sees the same true-newest-first order.
    if isinstance(detail.get("releases"), list):
        detail["releases"] = _sorted_releases(detail["releases"])
    app._moddb_current_mod = detail
    app._render_mod_detail(detail)
    app._set_moddb_status(f"Loaded '{detail.get('name') or stub.get('name')}'.")

def _render_mod_detail(app: 'ServerManagerApp', mod, fallback_stub=None, error=None):
    d = app.moddb_details_text
    d.configure(state='normal')
    d.delete("1.0", tk.END)
    if mod is None:
        m = fallback_stub or {}
        d.insert(tk.END, f"{m.get('name') or 'Unknown'}\n", ("title",))
        if error:
            d.insert(tk.END, f"Could not load details: {error}\n",
                     ("meta",))
        else:
            d.insert(tk.END, "No detail available.\n", ("meta",))
        d.configure(state='disabled')
        app._render_mod_files([])
        return

    side = app._normalize_side(mod.get("side"))
    side_tag, side_label = app._side_badge(side)
    name = str(mod.get("name") or "(unnamed)")
    d.insert(tk.END, f"{name}\n", ("title",))

    side_line_tag = side_tag
    d.insert(tk.END, f"SIDE: {side_label}   ",
             (side_line_tag,))
    d.insert(tk.END,
             f"by {mod.get('author') or '?'}   "
             f"downloads: {mod.get('downloads') or 0}   "
             f"follows: {mod.get('follows') or 0}\n",
             ("meta",))
    tags_list = mod.get("tags") or []
    if tags_list:
        d.insert(tk.END, "tags: ", ("meta",))
        d.insert(tk.END, ", ".join(str(t) for t in tags_list) + "\n",
                 ("body",))
    if mod.get("urlalias"):
        d.insert(tk.END, f"url: {app.moddb.SITE_BASE}/{mod['urlalias']}\n",
                 ("meta",))
    elif mod.get("assetid"):
        d.insert(tk.END,
                 f"url: {app.moddb.SITE_BASE}/show/mod/{mod['assetid']}\n",
                 ("meta",))
    d.insert(tk.END, "\n")

    desc = str(mod.get("text") or mod.get("description")
               or mod.get("summary") or "").strip()
    # The API's mod.text may contain HTML from the mod page; for a
    # terminal-style readout we strip tags naively so we never render
    # raw markup.
    desc = re.sub(r"<[^>]+>", "", desc)
    desc = re.sub(r"\s+\n", "\n", desc)
    if desc:
        d.insert(tk.END, desc + "\n", ("body",))
    d.configure(state='disabled')

    releases = mod.get("releases") or []
    app._render_mod_files(releases)

def _render_mod_files(app: 'ServerManagerApp', releases):
    """Render the file-picker. Each release typically has:
        releaseid, mainfile, filename, fileid, downloads,
        tags (game versions), modversion, created
    """
    ft = app.moddb_files_text
    ft.configure(state='normal')
    ft.delete("1.0", tk.END)
    app._moddb_file_rows.clear()
    app._moddb_file_selected_row = None
    app._moddb_current_file = None

    if not releases:
        ft.insert(tk.END, "  (no files)\n", ("file_meta",))
        ft.configure(state='disabled')
        return

    # Pick a "best match" release whose game-version tags include the
    # currently-selected gv label, so we can visually highlight it.
    gv_label = app.moddb_gv_var.get()
    selected_gv_name = gv_label if gv_label and gv_label != "(any)" else None
    best_idx = app._pick_best_release(releases, selected_gv_name)

    for i, rel in enumerate(releases):
        row_start = ft.index("end-1c")
        row_num = int(row_start.split('.')[0])
        filename = str(rel.get("filename") or "(file)")
        modversion = rel.get("modversion") or "?"
        gv_tags = rel.get("tags") or []
        gv_text = ", ".join(str(t) for t in gv_tags) if gv_tags else "?"
        created = rel.get("created") or ""
        try:
            size_bytes = int(rel.get("filesize") or 0)
        except (ValueError, TypeError):
            size_bytes = 0
        size_text = app._fmt_size(size_bytes) if size_bytes else "?"

        marker = "★" if i == best_idx else " "
        tag = "file_best" if i == best_idx else "file"
        ft.insert(tk.END,
                  f"  {marker} v{modversion}  [{gv_text}]  {size_text}\n",
                  (tag,))
        ft.insert(tk.END,
                  f"      {filename}  ({created[:10] if created else ''})\n",
                  ("file_meta",))
        app._moddb_file_rows[row_num] = rel
        app._moddb_file_rows[row_num + 1] = rel

    ft.configure(state='disabled')

    # Auto-select the best-match file
    if best_idx is not None and best_idx < len(releases):
        app._select_file_row(best_idx, releases)

def _pick_best_release(app: 'ServerManagerApp', releases, selected_gv_name):
    """Return index of the best release given the current gv filter.

    Preference order:
      1. A release whose tags include the selected gv name exactly.
      2. The first release (releases are typically sorted newest-first).
    """
    if selected_gv_name:
        for i, rel in enumerate(releases):
            tags = [str(t) for t in (rel.get("tags") or [])]
            if selected_gv_name in tags:
                return i
    return 0

def _select_file_row(app: 'ServerManagerApp', idx, releases):
    # Find the text row (line number) that maps to this release.
    header_line = None
    for line, rel in app._moddb_file_rows.items():
        if rel is releases[idx] and (header_line is None or line < header_line):
            header_line = line
    if header_line is None:
        return
    ft = app.moddb_files_text
    ft.configure(state='normal')
    ft.tag_remove("selected", "1.0", tk.END)
    ft.tag_add("selected", f"{header_line}.0", f"{header_line + 1}.end")
    ft.configure(state='disabled')
    app._moddb_file_selected_row = header_line
    app._moddb_current_file = releases[idx]

def _on_moddb_file_click(app: 'ServerManagerApp', event):
    row = int(app.moddb_files_text.index(f"@{event.x},{event.y}")
              .split('.')[0])
    rel = app._moddb_file_rows.get(row)
    if not rel or not app._moddb_current_mod:
        return
    releases = app._moddb_current_mod.get("releases") or []
    try:
        idx = releases.index(rel)
    except ValueError:
        return
    app._select_file_row(idx, releases)

# --- install pipeline --------------------------------------------

def _install_current_file(app: 'ServerManagerApp'):
    if app._moddb_download_active:
        app._notify("Another download is already running.", level="warn")
        return
    mod = app._moddb_current_mod
    rel = app._moddb_current_file
    if not mod or not rel:
        app._notify("Select a mod and a file to install.", level="warn")
        return

    side = app._normalize_side(mod.get("side"))
    # The side gate.
    if side == "client":
        if not messagebox.askyesno(
                "Client-side mod",
                f"The mod '{mod.get('name')}' is marked CLIENT SIDE ONLY.\n\n"
                "Installing it on a server is almost certainly useless and "
                "may cause startup errors or crashes.\n\n"
                "Install anyway?"):
            app._set_moddb_status("Install cancelled (client-side).")
            return
    elif side == "unknown":
        if not messagebox.askyesno(
                "Side not declared",
                f"The mod '{mod.get('name')}' does not declare which side "
                "it runs on.\n\nIf it turns out to be client-only it won't "
                "help your server. Continue?"):
            app._set_moddb_status("Install cancelled (side unknown).")
            return
    # "server" and "universal" proceed silently.

    # Warn if the server is actively running.
    if app.is_running:
        if not messagebox.askyesno(
                "Server running",
                "The server is running. The mod will be downloaded now "
                "but will not take effect until the server restarts.\n\n"
                "Continue?"):
            return

    # Resolve destination + duplicate handling
    mods_dir = app.mods_folder_var.get()
    if not mods_dir or not os.path.isdir(mods_dir):
        app._notify("Set a valid Mods folder first.", level="error")
        return
    url = rel.get("mainfile") or rel.get("file") or rel.get("filename")
    if url and not url.startswith("http"):
        # API often returns mainfile as a relative path; combine with site.
        url = app.moddb.SITE_BASE + "/" + url.lstrip("/")
    if not url:
        app._notify("Release has no file URL.", level="error")
        return
    if not app.moddb.is_trusted_url(url):
        app._notify(f"Refusing download from untrusted host: {url}",
                     level="error")
        return

    # Build the destination filename. ModDB's direct-download URLs
    # embed a cache-buster hash in the filename (e.g.
    # "mymod_1.2.3_abcdef123456789.zip"), so naively using the URL
    # basename leaves a long hex suffix on every installed file.
    #
    # Field hierarchy: ModDB's API returns the human-readable mod
    # slug as "urlalias" (e.g. "medievalexpansion"), and "modid" is
    # frequently a numeric primary key — using that produces names
    # like "4571.zip" which is meaningless. We pass urlalias first,
    # the helper rejects numeric values internally, and "name"
    # (display name) is a final slugify-able fallback.
    filename = clean_mod_filename(
        url=url,
        declared=rel.get("filename"),
        modid=mod.get("urlalias") or mod.get("modid"),
        version=rel.get("modversion") or rel.get("version"),
        name=mod.get("name"),
    )
    dest_path = os.path.join(mods_dir, filename)
    if os.path.exists(dest_path):
        choice = messagebox.askyesnocancel(
            "File exists",
            f"'{filename}' already exists in the Mods folder.\n\n"
            "Yes = overwrite\n"
            "No  = keep both (append timestamp to new file)\n"
            "Cancel = abort")
        if choice is None:
            return
        if choice is False:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            stem, ext = os.path.splitext(filename)
            filename = f"{stem}.{ts}{ext}"
            dest_path = os.path.join(mods_dir, filename)

    try:
        expected_size = int(rel.get("filesize") or 0) or None
    except (ValueError, TypeError):
        expected_size = None

    app._moddb_download_cancel["flag"] = False
    app._moddb_download_active = True
    app.moddb_install_btn.set_enabled(False)
    app.moddb_cancel_btn.set_enabled(True)
    app._set_moddb_progress(0, 0, "Starting…")
    app.append_console(
        f"Downloading '{mod.get('name')}' → {filename}", "system")

    t = threading.Thread(
        target=app._moddb_download_worker,
        args=(url, dest_path, expected_size, mod.get('name') or filename),
        daemon=True)
    t.start()

def _cancel_moddb_download(app: 'ServerManagerApp'):
    if not app._moddb_download_active:
        return
    app._moddb_download_cancel["flag"] = True
    app._set_moddb_status("Cancelling download…")

def _moddb_download_worker(app: 'ServerManagerApp', url, dest, expected_size, display_name):
    def progress_cb(got, total):
        app.after(0, app._set_moddb_progress, got, total, "Downloading")
    def cancel_cb():
        return app._moddb_download_cancel.get("flag", False)
    try:
        app.moddb.download_file(url, dest,
                                 progress_cb=progress_cb,
                                 cancel_flag=cancel_cb,
                                 expected_size=expected_size)
        error = None
    except Exception as e:
        error = str(e)
    app.after(0, app._finalize_moddb_download, dest, display_name, error)

def _finalize_moddb_download(app: 'ServerManagerApp', dest, display_name, error):
    app._moddb_download_active = False
    app.moddb_install_btn.set_enabled(True)
    app.moddb_cancel_btn.set_enabled(False)
    if error:
        app._set_moddb_status(f"Install failed: {error}")
        app._set_moddb_progress(0, 0, "failed")
        app.append_console(
            f"Download of '{display_name}' failed: {error}", "error")
        app._notify(f"Install failed: {error}", level="error")
        return
    app._set_moddb_status(f"Installed: {os.path.basename(dest)}")
    app._set_moddb_progress(1, 1, "done")
    app.append_console(
        f"✓ Installed {display_name} → {os.path.basename(dest)}",
        "success")
    app._notify(f"Installed: {os.path.basename(dest)}",
                 level="success")
    # Refresh installed list if the file landed in the active Mods folder.
    if os.path.dirname(dest) == app.mods_folder_var.get():
        app.load_mods()

def _set_moddb_progress(app: 'ServerManagerApp', got, total, label):
    if total and total > 0:
        frac = max(0.0, min(1.0, got / total))
        app.moddb_progress_fill.place_configure(relwidth=frac)
        app.moddb_progress_var.set(
            f"{label}  {app._fmt_size(got)} / {app._fmt_size(total)} "
            f"({frac * 100:.0f}%)")
    else:
        app.moddb_progress_var.set(f"{label}  {app._fmt_size(got)}")
        if label == "done":
            app.moddb_progress_fill.place_configure(relwidth=1)
        elif label == "failed":
            app.moddb_progress_fill.place_configure(relwidth=0)

# --- update checker ---------------------------------------------

def check_mod_updates(app: 'ServerManagerApp'):
    """Read every local mod's modinfo.json, then ask ModDB for the
    latest matching release. Present a summary dialog + offer bulk
    update for those that are stale.

    Uses the on-disk TTL cache by default; tick the "Force refresh"
    checkbox in the Mods tab toolbar to bypass it for one run."""
    mods_dir = app.mods_folder_var.get()
    if not mods_dir or not os.path.isdir(mods_dir):
        app._notify("Set a valid Mods folder first.", level="warn")
        return
    local = []
    for fn in sorted(os.listdir(mods_dir)):
        if not fn.lower().endswith(
                ('.zip', '.jar', '.cs', '.dll', '.disabled')):
            continue
        full = os.path.join(mods_dir, fn)
        info = LocalModInspector.read_mod_file(full)
        local.append(info)
    if not local:
        app._notify("No mods found locally.", level="info")
        return

    # Pull the force-refresh flag off the toolbar Tk var (set by the
    # checkbox added in _build_mods_browse_left). The plain bool
    # attribute is what the worker actually reads.
    try:
        var = getattr(app, "_update_check_force_refresh_var", None)
        app._update_check_force_refresh = bool(var.get()) if var is not None else False
    except Exception:
        app._update_check_force_refresh = False

    app._set_moddb_status(
        f"Checking updates for {len(local)} mod(s) "
        f"{'(force refresh) ' if app._update_check_force_refresh else ''}…")
    t = threading.Thread(
        target=app._update_check_worker,
        args=(local,),
        daemon=True)
    t.start()

def _update_check_worker(app: 'ServerManagerApp', local_mods):
    """For each local mod with a modid, query /api/mod/{modid} via
    the cached + parallelised path, compare versions, and collect
    results. Marshals back to the UI thread.

    Performance:
      - Network calls are run via ThreadPoolExecutor with
        ModDbClient.UPDATE_CHECK_PARALLELISM workers (default 8).
      - get_mod_cached() consults the on-disk TTL cache first
        (default 6h), so re-running the check inside the TTL
        window costs zero network.
      - The "Force refresh" checkbox in the Mods tab bypasses the
        cache for one check; the helper sets
        app._update_check_force_refresh.
    """
    import concurrent.futures

    # Lazy-attach the cache the first time we need it. The path lives
    # next to settings.json so it travels with portable installs.
    try:
        if getattr(app.moddb, "_mod_cache", None) is None:
            from core.settings import settings_path
            cache_path = os.path.join(
                os.path.dirname(settings_path()), "moddb_cache.json")
            app.moddb.attach_cache(cache_path)
    except Exception:
        # Cache attachment failure is non-fatal — fall through to
        # per-call fetches.
        pass

    force = bool(getattr(app, "_update_check_force_refresh", False))

    # Pre-pass: split mods that don't have a modid (no need to
    # consume thread-pool slots on them).
    no_modid = [info for info in local_mods if not info.get("modid")]
    have_modid = [info for info in local_mods if info.get("modid")]

    # Surface progress for cached vs. fetched as we go.
    cache_hits = [0]
    fetched    = [0]
    def _progress():
        done = cache_hits[0] + fetched[0]
        app.after(0, app._set_moddb_status,
                   f"Checking updates — {done}/{len(have_modid)} "
                   f"({cache_hits[0]} cached, {fetched[0]} fetched)…")

    def _check_one(info):
        modid = info.get("modid")
        try:
            had_cache = not force and app.moddb.has_fresh_cached(modid)
            detail = app.moddb.get_mod_cached(modid, force_refresh=force)
            if had_cache:
                cache_hits[0] += 1
            else:
                fetched[0] += 1
            _progress()
        except Exception as e:
            fetched[0] += 1
            _progress()
            return {"info": info, "status": "error", "error": str(e)}
        releases = _sorted_releases(detail.get("releases") or [])
        if not releases:
            return {"info": info, "status": "no_releases",
                    "detail": detail}
        latest     = releases[0]
        latest_ver = str(latest.get("modversion") or "")
        local_ver  = str(info.get("version") or "")
        is_newer   = app._version_is_newer(latest_ver, local_ver)
        return {
            "info":       info,
            "detail":     detail,
            "latest":     latest,
            "latest_ver": latest_ver,
            "local_ver":  local_ver,
            "status":     "outdated" if is_newer else "current",
        }

    report = [{"info": info, "status": "no_modid"} for info in no_modid]
    if have_modid:
        # ModDbClient is thread-safe across separate get_mod calls
        # (urllib.request is, and the SSL context is shared safely).
        max_workers = max(1, getattr(
            type(app.moddb), "UPDATE_CHECK_PARALLELISM", 8))
        # Cap at the number of pending mods — no point spinning up
        # 8 workers for 3 mods.
        max_workers = min(max_workers, len(have_modid))
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="vssm-modcheck") as ex:
            futures = [ex.submit(_check_one, info) for info in have_modid]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    report.append(fut.result())
                except Exception as e:
                    # _check_one shouldn't raise — anything getting here
                    # is a logic bug, not a per-mod failure. Log it as
                    # an "error" entry on a placeholder info dict so the
                    # UI still surfaces it.
                    report.append({"info": {"name": "(unknown)"},
                                   "status": "error", "error": str(e)})

    # Persist the cache so the next launch starts warm. Best-effort.
    try:
        app.moddb.save_cache()
    except Exception:
        pass

    app.after(0, app._show_update_report, report)

def _show_update_report(app: 'ServerManagerApp', report):
    outdated = [r for r in report if r.get("status") == "outdated"]
    errors   = [r for r in report if r.get("status") == "error"]
    no_id    = [r for r in report if r.get("status") == "no_modid"]
    current  = [r for r in report if r.get("status") == "current"]
    app._set_moddb_status(
        f"Update check: {len(outdated)} outdated, {len(current)} up-to-date, "
        f"{len(errors)} error(s), {len(no_id)} unreadable.")

    lines = []
    if outdated:
        lines.append("OUTDATED:")
        for r in outdated:
            lines.append(
                f"  • {r['info'].get('name')}  "
                f"{r.get('local_ver') or '?'} → {r.get('latest_ver') or '?'}")
    if current:
        lines.append("\nUP TO DATE:")
        for r in current[:20]:
            lines.append(f"  • {r['info'].get('name')} ({r.get('local_ver')})")
        if len(current) > 20:
            lines.append(f"  … and {len(current) - 20} more")
    if errors:
        lines.append("\nFAILED TO CHECK:")
        for r in errors:
            lines.append(f"  • {r['info'].get('name')}: {r.get('error')}")
    if no_id:
        lines.append("\nNO modid (skipped):")
        for r in no_id:
            lines.append(f"  • {r['info'].get('name')}")

    msg = "\n".join(lines) if lines else "No mods to report."

    if outdated:
        proceed = messagebox.askyesno(
            "Mod Update Report",
            f"{msg}\n\nUpdate the {len(outdated)} outdated mod(s) now?")
        if proceed:
            app._bulk_update(outdated)
    else:
        messagebox.showinfo("Mod Update Report", msg)

def _bulk_update(app: 'ServerManagerApp', outdated_reports):
    """Download and replace each outdated mod. Client-side warnings still
    apply; the user can opt to skip them all with a single confirm."""
    # Surface any client-only mods up-front so the user can skip them in bulk.
    client_side = []
    for r in outdated_reports:
        side = app._normalize_side(r.get("detail", {}).get("side"))
        if side == "client":
            client_side.append(r)

    skip_client = False
    if client_side:
        names = ", ".join(str(r["info"].get("name")) for r in client_side[:5])
        if len(client_side) > 5:
            names += f" and {len(client_side) - 5} more"
        skip_client = messagebox.askyesno(
            "Client-only mods in update list",
            f"{len(client_side)} of the outdated mods are CLIENT SIDE "
            f"ONLY: {names}.\n\nSkip all client-only mods?")

    to_process = []
    for r in outdated_reports:
        side = app._normalize_side(r.get("detail", {}).get("side"))
        if side == "client" and skip_client:
            continue
        to_process.append(r)

    if not to_process:
        app._notify("Nothing to update after filtering.", level="info")
        return

    app._set_moddb_status(
        f"Bulk update: {len(to_process)} mod(s) queued.")
    t = threading.Thread(
        target=app._bulk_update_worker,
        args=(to_process,),
        daemon=True)
    t.start()

def _bulk_update_worker(app: 'ServerManagerApp', reports):
    mods_dir = app.mods_folder_var.get()
    successes = 0
    failures = []
    for i, r in enumerate(reports, 1):
        info = r["info"]
        detail = r.get("detail") or {}
        latest = r.get("latest") or {}
        name = info.get("name") or "(unnamed)"
        app.after(0, app._set_moddb_status,
                   f"[{i}/{len(reports)}] Updating {name}…")
        url = latest.get("mainfile") or ""
        if url and not url.startswith("http"):
            url = app.moddb.SITE_BASE + "/" + url.lstrip("/")
        if not url or not app.moddb.is_trusted_url(url):
            failures.append((name, "no/untrusted URL"))
            continue
        try:
            expected_size = int(latest.get("filesize") or 0) or None
        except (ValueError, TypeError):
            expected_size = None
        filename = clean_mod_filename(
            url=url,
            declared=latest.get("filename"),
            # info.modid here comes from the LOCAL mod's
            # modinfo.json — already a real string identifier, not
            # a numeric ModDB primary key. Still goes through the
            # numeric-rejection guard inside the helper for safety.
            modid=info.get("modid"),
            version=latest.get("modversion") or latest.get("version"),
            name=info.get("name"),
        )
        dest = os.path.join(mods_dir, filename)
        # If the old file is different from the new filename, back up
        # and remove the old one after a successful download.
        old_path = info.get("path")
        try:
            app.moddb.download_file(url, dest,
                                     expected_size=expected_size)
            if old_path and os.path.exists(old_path) \
                    and os.path.abspath(old_path) != os.path.abspath(dest):
                try:
                    os.remove(old_path)
                except OSError:
                    pass
            successes += 1
        except Exception as e:
            failures.append((name, str(e)))
    app.after(0, app._finalize_bulk_update, successes, failures)

def _finalize_bulk_update(app: 'ServerManagerApp', successes, failures):
    app.load_mods()
    summary = f"{successes} updated"
    if failures:
        summary += f", {len(failures)} failed"
        app.append_console("Bulk update failures:", "warn")
        for name, err in failures:
            app.append_console(f"  • {name}: {err}", "error")
    else:
        summary += "."
    app._set_moddb_status(summary)
    app._notify(f"Bulk update: {summary}",
                 level="success" if not failures else "warn")

# --- helpers -----------------------------------------------------

def _set_moddb_status(app: 'ServerManagerApp', text):
    app.moddb_status_var.set(text)

def _open_current_mod_in_browser(app: 'ServerManagerApp'):
    mod = app._moddb_current_mod
    if not mod:
        app._notify("Select a mod first.", level="info")
        return
    url = None
    if mod.get("urlalias"):
        url = f"{app.moddb.SITE_BASE}/{mod['urlalias']}"
    elif mod.get("assetid"):
        url = f"{app.moddb.SITE_BASE}/show/mod/{mod['assetid']}"
    if not url:
        app._notify("No URL for this mod.", level="warn")
        return
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception as e:
        app._notify(f"Could not open browser: {e}", level="error")

