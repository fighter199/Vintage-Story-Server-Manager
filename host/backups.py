"""
host/backups.py — Backups: the BackupManager host hooks, backup/restore actions and
the BACKUP tab's list.

Methods of ServerManagerApp (VSSM.py), moved here unchanged.
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Callable

# ── Local packages ─────────────────────────────────────────────────────
from core.constants import (LOG)
from core.processes import (find_external_servers, describe as describe_servers)
from core.utils import (fmt_size)
from ui.theme import (Theme)


class BackupsMixin:

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


    def get_max_start_backups(self) -> int:
        try:
            return int(self.max_start_backups_var.get())
        except (ValueError, TypeError):
            return 0


    def get_max_stop_backups(self) -> int:
        try:
            return int(self.max_stop_backups_var.get())
        except (ValueError, TypeError):
            return 0


    def get_server_backups_dir(self) -> str:
        """The Vintage Story server's own Backups folder — a sibling of
        the Saves/world folder in the VintagestoryData layout. This is
        where /genbackup writes its consistent savegame copies."""
        world = (self.world_folder_var.get() or "").strip()
        if not world:
            return ""
        return os.path.join(os.path.dirname(os.path.abspath(world)),
                            "Backups")


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
        if not self.is_running and self._external_servers():
            self.append_console(
                "A Vintage Story server VSSM didn't start seems to be running "
                "on this world — VSSM can't ask it for a consistent "
                "/genbackup, so this backup may catch the savegame mid-write.",
                "warn")
        return self._backup_manager.backup_world(silent=silent)


    def _external_servers(self, world: str = "") -> list:
        """Running Vintage Story servers (other than VSSM's own) that may
        be using `world` (default: the configured world folder)."""
        own = []
        if self.server_process is not None:
            own.append(self.server_process.pid)
        try:
            return find_external_servers(world or self.get_world_folder(),
                                         own_pids=own)
        except Exception:
            LOG.exception("external server check failed")
            return []


    def _confirm_no_external_server(self, action: str, world: str = "",
                                    parent=None) -> bool:
        """True if it's OK to go ahead with `action` on the savegame: no
        outside server is using it, or the user says to continue."""
        servers = self._external_servers(world)
        if not servers:
            return True
        sure = any(s["certain"] for s in servers)
        what = ("is running and uses this world's data folder" if sure else
                "is running (VSSM can't tell which world it uses)")
        return messagebox.askyesno(
            "Server running outside VSSM",
            f"A Vintage Story server that VSSM didn't start {what}:\n\n"
            f"{describe_servers(servers)}\n\n"
            f"If it uses this savegame, {action} now can corrupt the world "
            "or be undone by the server. Stop that server first.\n\n"
            "Continue anyway?",
            icon="warning", default="no", parent=parent or self)


    def _start_async_backup(self, dst=None, silent: bool = False,
                             reason: str = "manual",
                             on_done: Callable[[bool], None] | None = None
                             ) -> None:
        self._backup_manager.start_async_backup(dst=dst, silent=silent,
                                                  reason=reason,
                                                  on_done=on_done)


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
        if not self.is_running and \
                not self._confirm_no_external_server("restoring a backup"):
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
        if not self.is_running and \
                not self._confirm_no_external_server("restoring a backup"):
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
