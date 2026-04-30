"""
ui/tab_chat_log.py — "CHAT LOG" tab with per-group subtabs.

Shows live + persisted chat from each chat group the server has been
emitting messages on. Group `0` is general; other IDs are private/named
groups. Each subtab can be renamed via the right-click menu (or the
"Rename group" button in the toolbar). The leftmost subtab — "All" —
shows every message across every group, sorted chronologically.

Read-only by design (out of scope for this iteration: typing into a
group from VSSM).
"""
from __future__ import annotations

import tkinter as tk
from datetime import datetime
from tkinter import simpledialog, messagebox, ttk

from .theme import Theme
from .widgets import TermButton, panel_header


class ChatLogTab:
    """Owns and renders the CHAT LOG tab. The host (ServerManagerApp)
    owns the ChatLogStore and feeds new entries via `on_new_entry`."""

    # Tab key reserved for the "All" view (not a real group ID).
    ALL_KEY = "__all__"

    def __init__(self, parent: tk.Frame, app):
        self._parent  = parent
        self._app     = app
        self._store   = app._chat_store        # ChatLogStore from the host
        # gid -> the Tk Text widget rendering that group's messages
        self._text_widgets: dict[str, tk.Text] = {}
        # gid -> the ttk.Notebook tab id (used to update tab labels)
        self._tab_ids: dict[str, str] = {}
        # gid currently visible (for selective autoscroll suppression
        # when the user has scrolled up to read history)
        self._current_gid: str = self.ALL_KEY
        self._build(parent)
        self._populate_existing_groups()

    # ------------------------------------------------------------------
    # Public API used by the host
    # ------------------------------------------------------------------
    def on_new_entry(self, group_id: str, player: str,
                     message: str, ts: float) -> None:
        """Called by the host when a chat line was appended to the
        store. Updates the per-group Text widget AND the All view."""
        gid = str(group_id)
        # Make sure a tab exists for this group
        if gid not in self._text_widgets:
            self._add_group_tab(gid)
        # Append to the per-group view
        self._append_line(self._text_widgets[gid], ts, player, message)
        # Append to All
        all_text = self._text_widgets.get(self.ALL_KEY)
        if all_text is not None:
            self._append_line(all_text, ts, player, message,
                               group_label=self._store.display_name(gid))
        # Refresh tab labels (entry count badge)
        self._refresh_tab_label(gid)

    def reload_from_store(self) -> None:
        """Tear down and rebuild every tab from the current store
        contents. Called after a profile switch or a "Clear all"."""
        for gid, text in list(self._text_widgets.items()):
            try:
                if text.winfo_exists():
                    text.configure(state=tk.NORMAL)
                    text.delete("1.0", tk.END)
                    text.configure(state=tk.DISABLED)
            except tk.TclError:
                pass
        self._populate_existing_groups()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------
    def _build(self, parent: tk.Frame) -> None:
        outer = tk.Frame(parent, bg=Theme.BG_PANEL)
        outer.pack(fill=tk.BOTH, expand=True)

        # Header strip
        head = tk.Frame(outer, bg=Theme.BG_PANEL)
        head.pack(fill=tk.X)
        panel_header(head, "Chat log",
                     font_spec=self._app.F_HDR)
        sub = tk.Label(
            head,
            text=("One subtab per chat group seen. Group 0 is general "
                  "chat. Right-click a tab to rename it, or use the "
                  "Rename / Clear buttons below."),
            fg=Theme.MUTED, bg=Theme.BG_PANEL,
            font=self._app.F_SMALL, justify=tk.LEFT, wraplength=900,
        )
        sub.pack(anchor=tk.W, padx=6, pady=(0, 4))

        # Toolbar — rename, clear group, clear all
        toolbar = tk.Frame(outer, bg=Theme.BG_PANEL)
        toolbar.pack(fill=tk.X, padx=6, pady=(2, 4))
        TermButton(toolbar, "✎ Rename group", self._on_rename,
                   variant="amber", font_spec=self._app.F_SMALL,
                   padx=8, pady=3).pack(side=tk.LEFT, padx=(0, 4))
        TermButton(toolbar, "🗑 Clear group", self._on_clear_group,
                   variant="amber", font_spec=self._app.F_SMALL,
                   padx=8, pady=3).pack(side=tk.LEFT, padx=(0, 4))
        TermButton(toolbar, "🗑 Clear all", self._on_clear_all,
                   variant="stop", font_spec=self._app.F_SMALL,
                   padx=8, pady=3).pack(side=tk.LEFT, padx=(0, 12))

        # Status indicator on the right (e.g. "12 groups")
        self._status_var = tk.StringVar(value="")
        tk.Label(toolbar, textvariable=self._status_var,
                 fg=Theme.MUTED, bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL).pack(side=tk.RIGHT, padx=8)

        # Nested notebook for the subtabs
        self._notebook = ttk.Notebook(outer, style="Term.TNotebook")
        self._notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        self._notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._notebook.bind("<Button-3>", self._on_tab_right_click)

        # Always create the "All" tab first so it's the leftmost
        self._add_all_tab()

    def _add_all_tab(self) -> None:
        frame = tk.Frame(self._notebook, bg=Theme.BG_PANEL)
        text = self._make_text(frame)
        text.pack(fill=tk.BOTH, expand=True)
        self._notebook.add(frame, text="All")
        self._tab_ids[self.ALL_KEY] = str(frame)
        self._text_widgets[self.ALL_KEY] = text

    def _add_group_tab(self, gid: str) -> None:
        if gid in self._text_widgets:
            return
        frame = tk.Frame(self._notebook, bg=Theme.BG_PANEL)
        text = self._make_text(frame)
        text.pack(fill=tk.BOTH, expand=True)
        self._notebook.add(frame, text=self._tab_label(gid))
        self._tab_ids[gid] = str(frame)
        self._text_widgets[gid] = text
        self._refresh_status()

    def _make_text(self, parent: tk.Frame) -> tk.Text:
        """Build the Text+scrollbar widget pair used by every subtab."""
        wrap = tk.Frame(parent, bg=Theme.BORDER)
        wrap.pack(fill=tk.BOTH, expand=True)
        inner = tk.Frame(wrap, bg=Theme.BG_INPUT)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        text = tk.Text(
            inner,
            bg=Theme.BG_INPUT, fg=Theme.AMBER,
            insertbackground=Theme.AMBER_GLOW,
            selectbackground=Theme.BG_SELECT,
            selectforeground=Theme.AMBER_GLOW,
            font=self._app.F_NORMAL,
            bd=0, highlightthickness=0, wrap=tk.WORD,
            state=tk.DISABLED,
        )
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(inner, orient=tk.VERTICAL,
                            style="Term.Vertical.TScrollbar",
                            command=text.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        text.configure(yscrollcommand=sb.set)
        # Tags for visual differentiation. Player name in glow,
        # group prefix (in All view) dimmed.
        text.tag_configure("ts",     foreground=Theme.AMBER_DIM,
                            font=self._app.F_SMALL)
        text.tag_configure("group",  foreground=Theme.AMBER_DIM,
                            font=self._app.F_SMALL)
        text.tag_configure("player", foreground=Theme.AMBER_GLOW,
                            font=self._app.F_NORMAL)
        text.tag_configure("body",   foreground=Theme.AMBER,
                            font=self._app.F_NORMAL)
        # Right-click → copy
        text.bind("<Button-3>", lambda e, t=text: self._copy_selection(t))
        return text

    # ------------------------------------------------------------------
    # Populate from store
    # ------------------------------------------------------------------
    def _populate_existing_groups(self) -> None:
        # All known groups → one tab each
        for gid in self._store.known_group_ids():
            self._add_group_tab(gid)
            text = self._text_widgets[gid]
            for entry in self._store.entries(gid):
                self._append_line(text, entry.timestamp,
                                   entry.player, entry.message)
            self._refresh_tab_label(gid)
        # Re-fill the All view in chronological order
        all_text = self._text_widgets.get(self.ALL_KEY)
        if all_text is not None:
            for gid, entry in self._store.all_entries_sorted():
                self._append_line(all_text, entry.timestamp,
                                   entry.player, entry.message,
                                   group_label=self._store.display_name(gid))
        self._refresh_status()

    # ------------------------------------------------------------------
    # Append helper
    # ------------------------------------------------------------------
    def _append_line(self, text: tk.Text, ts: float, player: str,
                     message: str, group_label: str | None = None
                     ) -> None:
        """Append one chat line to a Text widget. Autoscrolls only if
        the user is already pinned to the bottom — if they've scrolled
        up to read history, we don't yank them away."""
        try:
            stamp = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        except (OSError, ValueError, OverflowError):
            stamp = "--:--:--"
        try:
            # Detect "user is at the bottom" before insert. yview()
            # returns (top, bottom) fractions; bottom >= 0.999 ≈ at end.
            _, bottom = text.yview()
            was_at_bottom = bottom >= 0.999
        except tk.TclError:
            was_at_bottom = True
        try:
            text.configure(state=tk.NORMAL)
            text.insert(tk.END, f"[{stamp}] ", "ts")
            if group_label:
                text.insert(tk.END, f"[{group_label}] ", "group")
            text.insert(tk.END, f"{player}: ", "player")
            text.insert(tk.END, f"{message}\n", "body")
            text.configure(state=tk.DISABLED)
            if was_at_bottom:
                text.see(tk.END)
        except tk.TclError:
            # Widget was destroyed (e.g. profile switch mid-tick).
            pass

    # ------------------------------------------------------------------
    # Toolbar handlers
    # ------------------------------------------------------------------
    def _current_group_id(self) -> str | None:
        """Return the gid of the currently-selected subtab, or None
        if the All tab is selected."""
        sel = self._notebook.select()
        for gid, frame_str in self._tab_ids.items():
            if frame_str == sel:
                return None if gid == self.ALL_KEY else gid
        return None

    def _on_tab_changed(self, _event=None) -> None:
        sel = self._notebook.select()
        for gid, frame_str in self._tab_ids.items():
            if frame_str == sel:
                self._current_gid = gid
                return

    def _on_rename(self) -> None:
        gid = self._current_group_id()
        if gid is None:
            self._app._notify("Pick a specific group tab to rename "
                              "(can't rename the All view).",
                              level="warn")
            return
        current = self._store.display_name(gid)
        new = simpledialog.askstring(
            "Rename group",
            f"New name for group {gid}\n"
            f"(current: {current!r}; blank resets to default):",
            parent=self._parent,
            initialvalue=(current if self._store.has_custom_name(gid)
                          else ""))
        if new is None:
            return  # cancelled
        self._store.set_name(gid, new)
        self._refresh_tab_label(gid)
        self._app._chat_store_changed()
        self._app._notify(
            f"Group {gid} renamed to {self._store.display_name(gid)!r}.",
            level="success", duration_ms=1800)

    def _on_clear_group(self) -> None:
        gid = self._current_group_id()
        if gid is None:
            self._app._notify("Pick a specific group tab to clear.",
                              level="warn")
            return
        ok = messagebox.askokcancel(
            "Clear group",
            f"Clear all chat history for "
            f"{self._store.display_name(gid)!r}?\n"
            f"(The group name itself is kept.)",
            parent=self._parent)
        if not ok:
            return
        self._store.clear_group(gid)
        # Empty the per-group text widget
        text = self._text_widgets.get(gid)
        if text is not None:
            try:
                text.configure(state=tk.NORMAL)
                text.delete("1.0", tk.END)
                text.configure(state=tk.DISABLED)
            except tk.TclError:
                pass
        # Rebuild the All view from scratch
        self._rebuild_all_view()
        self._refresh_tab_label(gid)
        self._app._chat_store_changed()

    def _on_clear_all(self) -> None:
        ok = messagebox.askokcancel(
            "Clear all chat history",
            "Clear chat history for EVERY group?\n"
            "(Group names are kept; tabs themselves stay until restart.)",
            parent=self._parent)
        if not ok:
            return
        self._store.clear_all()
        for gid, text in self._text_widgets.items():
            try:
                text.configure(state=tk.NORMAL)
                text.delete("1.0", tk.END)
                text.configure(state=tk.DISABLED)
            except tk.TclError:
                pass
            if gid != self.ALL_KEY:
                self._refresh_tab_label(gid)
        self._app._chat_store_changed()

    def _rebuild_all_view(self) -> None:
        all_text = self._text_widgets.get(self.ALL_KEY)
        if all_text is None:
            return
        try:
            all_text.configure(state=tk.NORMAL)
            all_text.delete("1.0", tk.END)
            all_text.configure(state=tk.DISABLED)
        except tk.TclError:
            return
        for gid, entry in self._store.all_entries_sorted():
            self._append_line(all_text, entry.timestamp,
                               entry.player, entry.message,
                               group_label=self._store.display_name(gid))

    # ------------------------------------------------------------------
    # Tab right-click menu (rename shortcut)
    # ------------------------------------------------------------------
    def _on_tab_right_click(self, event) -> None:
        # Figure out which tab the click landed on
        try:
            idx = self._notebook.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return
        try:
            tab_id = self._notebook.tabs()[idx]
        except (IndexError, tk.TclError):
            return
        # Map back to gid
        gid = None
        for g, frame_str in self._tab_ids.items():
            if frame_str == tab_id:
                gid = g
                break
        if gid is None or gid == self.ALL_KEY:
            return
        # Select the tab so the rename action targets it
        self._notebook.select(tab_id)
        self._on_rename()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _tab_label(self, gid: str) -> str:
        name = self._store.display_name(gid)
        n    = len(self._store.entries(gid))
        return f"{name}  ({n})" if n else name

    def _refresh_tab_label(self, gid: str) -> None:
        if gid not in self._tab_ids:
            return
        try:
            self._notebook.tab(self._tab_ids[gid],
                                text=self._tab_label(gid))
        except tk.TclError:
            pass

    def _refresh_status(self) -> None:
        n = len(self._store.known_group_ids())
        self._status_var.set(
            f"{n} group{'s' if n != 1 else ''}")

    def _copy_selection(self, text: tk.Text) -> None:
        try:
            sel = text.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            return
        try:
            self._app.clipboard_clear()
            self._app.clipboard_append(sel)
            self._app._notify(f"Copied {len(sel)} chars.",
                              level="info", duration_ms=1200)
        except tk.TclError:
            pass
