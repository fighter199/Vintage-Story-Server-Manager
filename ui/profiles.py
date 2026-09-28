"""
ui/profiles.py — The profile row at the top of SETTINGS.

A profile is one server set-up (see core/profiles.py): its executable,
folders, schedules, custom commands, autorun rules, chat log and
playtime. The row shows the active profile in a drop-down — picking
another switches to it — with New, Duplicate, Rename and Delete.

Switching is refused while the server runs, a backup or restore is
going, or the world map is editing a savegame: the profile decides
which server and world VSSM controls, so it can't change under them.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from core.chat_log import ChatLogStore
from core.constants import APP_VERSION, LOG
from core.profiles import (ProfileError, active_profile_name, create_profile,
                           delete_profile, profile_names, rename_profile,
                           set_active_profile, validate_name)
from core.settings import save_settings

from .theme import Theme
from .widgets import TermButton, auto_wrap, flow_children

APP_TITLE = "VSSM — Vintage Story Server Manager"


class ProfileBar:
    def __init__(self, parent, app):
        self._app = app
        self._var = tk.StringVar(value=active_profile_name(app._settings))

        row = tk.Frame(parent, bg=Theme.BG_PANEL)
        row.pack(fill=tk.X, padx=10, pady=(8, 0))
        tk.Label(row, text="Profile:", fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
                 font=app.F_SMALL).pack(side=tk.LEFT)
        self._combo = ttk.Combobox(row, textvariable=self._var, width=20,
                                   state="readonly", style="Term.TCombobox",
                                   font=app.F_NORMAL)
        self._combo.pack(side=tk.LEFT, padx=(6, 0))
        self._combo.bind("<<ComboboxSelected>>", self._on_selected)
        for text, command, variant in (("+ New", self.new, "amber"),
                                       ("⧉ Duplicate", self.duplicate, "amber"),
                                       ("✎ Rename", self.rename, "amber"),
                                       ("🗑 Delete", self.delete, "stop")):
            button = TermButton(row, text, command, variant=variant,
                                font_spec=app.F_SMALL, padx=8, pady=2)
            button.pack(side=tk.LEFT, padx=(6, 0))
            if command == self.delete:
                self._delete_btn = button
        flow_children(row)
        auto_wrap(tk.Label(
            parent, fg=Theme.MUTED, bg=Theme.BG_PANEL, font=app.F_SMALL,
            text=("Each profile has its own server, folders, schedules, "
                  "custom commands, autorun rules, chat log and playtime."))
        ).pack(anchor=tk.W, fill=tk.X, padx=10, pady=(2, 0))
        self.refresh()

    # ------------------------------------------------------------------
    @property
    def _settings(self) -> dict:
        return self._app._settings

    def refresh(self) -> None:
        """Show the current profile list, and name the active profile in
        the window title and header (when there's more than the default
        one)."""
        names = profile_names(self._settings)
        active = active_profile_name(self._settings)
        self._combo.configure(values=names)
        self._var.set(active)
        suffix = "" if names == ["default"] else f" · {active}"
        self._app.title(APP_TITLE + suffix)
        header = getattr(self._app, "_header_title", None)
        if header is not None:
            header.configure(text=f"VSSM v{APP_VERSION} — Vintage Story "
                                  f"Server Manager{suffix}")

    def _busy_reason(self) -> str | None:
        app = self._app
        if app.is_running or app._shutdown_in_progress:
            return ("The server is running. Stop it before changing "
                    "profile — the profile decides which server and "
                    "world VSSM controls.")
        if app._backup_manager.in_progress:
            return "A backup or restore is running. Try again once it has finished."
        if app._world_edit_in_progress:
            return ("The world map is editing a savegame. Try again once "
                    "it has finished.")
        return None

    def _check_idle(self, title: str) -> bool:
        reason = self._busy_reason()
        if reason:
            messagebox.showinfo(title, reason, parent=self._app)
            return False
        return True

    def _settle_unsaved(self, doing: str) -> bool:
        """Offer to save edits to the active profile's settings first.
        False if the user cancelled."""
        app = self._app
        if not app._profile_has_unsaved_changes():
            return True
        name = active_profile_name(self._settings)
        choice = messagebox.askyesnocancel(
            "Unsaved settings",
            f'Settings of the profile "{name}" were changed but not '
            f"saved.\n\nSave them before {doing}?", parent=app)
        if choice is None:
            return False
        if choice:
            app._save_profile_settings()
        return True

    def _ask_name(self, title: str, prompt: str, initial: str = "",
                  ignore: str | None = None) -> str | None:
        while True:
            name = simpledialog.askstring(title, prompt, initialvalue=initial,
                                          parent=self._app)
            if name is None:
                return None
            try:
                return validate_name(self._settings, name, ignore=ignore)
            except ProfileError as e:
                messagebox.showerror(title, str(e), parent=self._app)
                initial = name

    # ------------------------------------------------------------------
    # Switching
    # ------------------------------------------------------------------
    def _on_selected(self, _event=None) -> None:
        if not self.switch(self._var.get()):
            self._var.set(active_profile_name(self._settings))
        self._combo.selection_clear()

    def switch(self, name: str) -> bool:
        if name == active_profile_name(self._settings):
            return True
        if not self._check_idle("Switch profile"):
            return False
        if not self._settle_unsaved(f'switching to "{name}"'):
            return False
        self._activate(name)
        self._app._notify(f'Profile "{name}" loaded.', level="success")
        return True

    def _activate(self, name: str) -> None:
        app = self._app
        try:
            app._chat_store.flush()
            app._player_timers.flush()
        except Exception:
            LOG.exception("flushing the old profile's data failed")
        set_active_profile(self._settings, name)
        save_settings(self._settings)
        LOG.info("switched to profile %r", name)

        # Everything below reads the new profile. The MODS and BACKUP
        # lists follow their folder fields on their own.
        app._apply_default_paths()
        app._chat_store = ChatLogStore(load_history=app._chat_log_load,
                                       save_history=app._chat_log_save)
        chat_tab = getattr(app, "_chat_log_tab", None)
        if chat_tab is not None:
            chat_tab._store = app._chat_store
            chat_tab.reload_from_store()
        for attr in ("_custom_cmds_tab", "_autorun_tab"):
            tab = getattr(app, attr, None)
            if tab is not None:
                tab.reload_from_settings()
        world_map = getattr(app, "_world_map_tab", None)
        if world_map is not None:
            world_map.refresh_saves()
        try:
            app._apply_cron_schedule()
        except Exception:
            LOG.exception("applying the profile's restart schedule failed")
        self.refresh()

    # ------------------------------------------------------------------
    # New / Duplicate / Rename / Delete
    # ------------------------------------------------------------------
    def new(self) -> None:
        if not self._check_idle("New profile"):
            return
        name = self._ask_name(
            "New profile",
            "Name for the new profile.\n\nIt starts with empty folders: "
            "set them below, then click Save Settings.")
        if name is None or not self._settle_unsaved("creating a new profile"):
            return
        self._create_and_switch(name, None)

    def duplicate(self) -> None:
        if not self._check_idle("Duplicate profile"):
            return
        source = active_profile_name(self._settings)
        name = self._ask_name(
            "Duplicate profile",
            f'Name for the copy of "{source}".\n\nIt gets the same folders, '
            "schedules, custom commands and autorun rules — but not the "
            "chat log or playtime.", initial=f"{source} copy")
        if name is None or not self._settle_unsaved(f'copying "{source}"'):
            return
        self._create_and_switch(name, source)

    def _create_and_switch(self, name: str, copy_from: str | None) -> None:
        try:
            name = create_profile(self._settings, name, copy_from=copy_from)
        except ProfileError as e:
            messagebox.showerror("Profile", str(e), parent=self._app)
            return
        self._activate(name)
        self._app._notify(f'Profile "{name}" created.', level="success")

    def rename(self) -> None:
        old = active_profile_name(self._settings)
        new = self._ask_name("Rename profile", f'New name for "{old}":',
                             initial=old, ignore=old)
        if new is None or new == old:
            return
        try:
            self._app._chat_store.flush()     # the file moves with the name
        except Exception:
            LOG.exception("chat store flush before rename failed")
        try:
            rename_profile(self._settings, old, new)
        except ProfileError as e:
            messagebox.showerror("Rename profile", str(e), parent=self._app)
            return
        save_settings(self._settings)
        self.refresh()
        self._app._notify(f'Profile renamed to "{new}".', level="success")

    def delete(self) -> None:
        active = active_profile_name(self._settings)
        others = [n for n in profile_names(self._settings) if n != active]
        if not others:
            messagebox.showinfo(
                "Delete profile",
                "This is the only profile, so there's nothing to delete. "
                "(The profile in use can't be deleted — switch to another "
                "one first.)", parent=self._app)
            return
        app = self._app
        menu = tk.Menu(app, tearoff=0, bg=Theme.BG_PANEL, fg=Theme.AMBER,
                       activebackground=Theme.BG_SELECT,
                       activeforeground=Theme.AMBER_GLOW, bd=0,
                       font=app.F_SMALL)
        menu.add_command(label="Delete which profile?", state=tk.DISABLED)
        for name in others:
            menu.add_command(label=name,
                             command=lambda n=name: self._confirm_delete(n))
        btn = self._delete_btn
        try:
            menu.tk_popup(btn.winfo_rootx(),
                          btn.winfo_rooty() + btn.winfo_height())
        finally:
            menu.grab_release()

    def _confirm_delete(self, name: str) -> None:
        if not messagebox.askyesno(
                "Delete profile",
                f'Delete the profile "{name}"?\n\nIts settings, custom '
                "commands, autorun rules, playtime totals and chat log are "
                "removed. The server, world and backups on disk are not "
                "touched.", icon=messagebox.WARNING, parent=self._app):
            return
        try:
            delete_profile(self._settings, name)
        except ProfileError as e:
            messagebox.showerror("Delete profile", str(e), parent=self._app)
            return
        save_settings(self._settings)
        self.refresh()
        self._app._notify(f'Profile "{name}" deleted.', level="info")
