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
import shutil
import threading
import urllib.parse
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING
import tkinter as tk
from tkinter import ttk

from core.constants import LOG
from mods.inspector import LocalModInspector
from .theme import Theme
from .widgets import (TermButton, TermEntry, TermCheckbutton,
                      ScrollableFrame, collapsible_section,
                      flow_children)

if TYPE_CHECKING:
    from VSSM import ServerManagerApp  # for type hints only


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
        ("🩺 Check mods",   app.check_mods_now,         "amber"),
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
    flow_children(btns, spacing=4)

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
    msb.pack(side=tk.RIGHT, fill=tk.Y, before=app.mod_listbox)
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
        fg=Theme.MUTED, bg=Theme.BG_INPUT,
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
    rsb.pack(side=tk.RIGHT, fill=tk.Y, before=app.moddb_results_text)
    app.moddb_results_text.configure(yscrollcommand=rsb.set)

    t = app.moddb_results_text
    t.tag_configure("row",       foreground=Theme.AMBER)
    t.tag_configure("row_name",  foreground=Theme.AMBER_GLOW)
    t.tag_configure("row_meta",  foreground=Theme.AMBER_DIM)
    t.tag_configure("side_srv",  foreground=Theme.GREEN)
    t.tag_configure("side_both", foreground=Theme.CYAN)
    t.tag_configure("side_cli",  foreground=Theme.RED)
    t.tag_configure("side_unk",  foreground=Theme.MUTED)
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
    dsb.pack(side=tk.RIGHT, fill=tk.Y, before=app.moddb_details_text)
    app.moddb_details_text.configure(yscrollcommand=dsb.set)
    d = app.moddb_details_text
    d.tag_configure("title", foreground=Theme.CYAN, font=app.F_HDR)
    d.tag_configure("meta",  foreground=Theme.AMBER_DIM)
    d.tag_configure("body",  foreground=Theme.AMBER)
    d.tag_configure("side_srv",  foreground=Theme.GREEN)
    d.tag_configure("side_both", foreground=Theme.CYAN)
    d.tag_configure("side_cli",  foreground=Theme.RED)
    d.tag_configure("side_unk",  foreground=Theme.MUTED)

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
    fsb.pack(side=tk.RIGHT, fill=tk.Y, before=app.moddb_files_text)
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


# The browser and update code live in their own modules; re-exported so
# VSSM.py finds every mods-tab function here (_MODS_TAB_METHODS).
from .mods_browser import (  # noqa: E402,F401
    init_moddb_catalogs_async,
    _moddb_catalogs_worker,
    _moddb_apply_catalogs,
    _toggle_moddb_tag,
    _refresh_tag_button_styles,
    _clear_moddb_tags,
    _schedule_moddb_search,
    _run_moddb_search,
    _moddb_search_worker,
    _moddb_apply_search,
    _rerender_moddb_results,
    _on_moddb_row_click,
    _load_mod_details_async,
    _mod_detail_worker,
    _apply_mod_detail,
    _render_mod_detail,
    _render_mod_files,
    _pick_best_release,
    _select_file_row,
    _on_moddb_file_click,
    _install_current_file,
    _cancel_moddb_download,
    _moddb_download_worker,
    _finalize_moddb_download,
    _set_moddb_progress,
    check_mod_updates,
    _update_check_worker,
    _show_update_report,
    _bulk_update,
    _bulk_update_worker,
    _finalize_bulk_update,
    _set_moddb_status,
    _open_current_mod_in_browser,
)
