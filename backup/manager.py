"""
backup/manager.py — World backup orchestration.

Encapsulates the logic that used to live as ~7 methods on the main
ServerManagerApp class:

  - backup_world          (start a manual backup)
  - cancel_active_backup  (signal the in-flight worker to abort)
  - prune_old_backups     (count- or day-based retention)
  - restore_backup        (pick a zip and restore over the world dir)

The manager talks to the host via a small HostProtocol so it can be
unit-tested with a fake host (no Tk, no real filesystem state).
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from datetime import datetime
from typing import Callable, Optional, Protocol

from core.constants import LOG
from core.utils import (backup_world_to_zip, restore_backup_zip, fmt_size)


class _HostProtocol(Protocol):
    """Subset of ServerManagerApp the BackupManager needs."""
    is_running: bool

    def append_console(self, text: str, tag: str = ...) -> None: ...
    def _notify(self, message: str, level: str = ...,
                duration_ms: int = ...) -> None: ...
    def _send_internal_command(self, cmd: str) -> bool: ...
    def after(self, ms: int, *args, **kwargs): ...

    # Path / config getters — return current values
    def get_world_folder(self) -> str: ...
    def get_backup_dir(self) -> str: ...
    def get_max_backups(self) -> int: ...
    def get_retention_mode(self) -> str: ...   # 'count' | 'days'
    def get_autosave_cmd_enabled(self) -> bool: ...


class BackupManager:
    """Owns the in-flight backup state and exposes the operations the
    Backup tab buttons call."""

    def __init__(self, host: _HostProtocol):
        self._host = host
        self._in_progress = False
        self._cancel_flag = False
        self._last_progress_post = 0.0

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------
    @property
    def in_progress(self) -> bool:
        return self._in_progress

    # ------------------------------------------------------------------
    # Public actions
    # ------------------------------------------------------------------
    def backup_world(self, silent: bool = False) -> Optional[str]:
        """Start a manual backup. Returns the destination path on
        successful kick-off, None otherwise."""
        if self._in_progress:
            if not silent:
                self._host._notify("Backup already in progress.",
                                    level="warn")
            return None
        src = self._host.get_world_folder()
        dst_root = self._host.get_backup_dir()
        if not src or not os.path.isdir(src) or not dst_root:
            if not silent:
                self._host._notify(
                    "Invalid source or destination folder.",
                    level="error")
            return None
        try:
            os.makedirs(dst_root, exist_ok=True)
        except OSError as e:
            self._host._notify(
                f"Could not create backup dir: {e}", level="error")
            return None
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(dst_root, f"backup-{timestamp}.zip")
        self._start_async_backup(dst=dst, silent=silent, reason="manual")
        return dst

    def start_async_backup(
        self,
        dst: Optional[str] = None,
        silent: bool = False,
        reason: str = "manual",
    ) -> None:
        """Public entry point used by auto-save / pre-start hooks."""
        self._start_async_backup(dst=dst, silent=silent, reason=reason)

    def cancel_active_backup(self) -> None:
        if self._in_progress:
            self._cancel_flag = True
            self._host.append_console("Backup cancel requested…", "warn")

    def prune_old_backups(self, announce: bool = True) -> None:
        dst_root = self._host.get_backup_dir()
        if not dst_root or not os.path.isdir(dst_root):
            return
        try:
            max_keep = int(self._host.get_max_backups())
        except (ValueError, TypeError):
            return
        if max_keep <= 0:
            return
        if self._host.get_retention_mode() == "days":
            self._prune_by_days(dst_root, max_keep, announce)
            return
        # Count-based retention
        try:
            entries: list[tuple[float, str]] = []
            for name in os.listdir(dst_root):
                if not name.startswith("backup-"):
                    continue
                full = os.path.join(dst_root, name)
                if (os.path.isdir(full)
                        or (os.path.isfile(full) and name.endswith(".zip"))):
                    entries.append((os.path.getmtime(full), full))
        except OSError:
            return
        entries.sort(key=lambda x: x[0], reverse=True)
        to_delete = entries[max_keep:]
        deleted = 0
        for _, path in to_delete:
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                deleted += 1
            except OSError as e:
                self._host.append_console(
                    f"Prune error on {path}: {e}", "error")
        if announce and deleted:
            self._host._notify(
                f"Pruned {deleted} old backup(s).", level="success")
        if deleted:
            try:
                self._host._refresh_backup_list()
            except (AttributeError, Exception):
                pass

    def restore_from_zip(self, zip_path: str) -> bool:
        """Restore the world from `zip_path`. Returns True on success.
        Caller is responsible for asking the user for confirmation."""
        world = self._host.get_world_folder()
        if not world:
            self._host._notify("World folder not configured.",
                                level="error")
            return False
        if self._host.is_running:
            self._host._notify(
                "Stop the server before restoring.", level="error")
            return False
        try:
            archived = restore_backup_zip(zip_path, world,
                                           archive_existing=True)
            msg = f"Restored from {os.path.basename(zip_path)}."
            if archived:
                msg += f" Old world → {os.path.basename(archived)}"
            self._host.append_console(msg, "success")
            self._host._notify("Restore complete.", level="success")
            return True
        except Exception as e:
            self._host._notify(f"Restore failed: {e}", level="error")
            self._host.append_console(f"Restore failed: {e}", "error")
            return False

    # ------------------------------------------------------------------
    # Internal — async worker
    # ------------------------------------------------------------------
    def _start_async_backup(
        self,
        dst: Optional[str] = None,
        silent: bool = False,
        reason: str = "manual",
    ) -> None:
        if self._in_progress:
            return
        src = self._host.get_world_folder()
        dst_root = self._host.get_backup_dir()
        if not src or not os.path.isdir(src) or not dst_root:
            return
        if dst is None:
            try:
                os.makedirs(dst_root, exist_ok=True)
            except OSError:
                pass
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dst = os.path.join(dst_root, f"backup-{timestamp}.zip")
        self._in_progress = True
        self._cancel_flag = False

        def kick():
            self._host.append_console(
                f"Backup starting ({reason}) → {os.path.basename(dst)}",
                "system")
            t = threading.Thread(
                target=self._backup_worker,
                args=(src, dst, silent, reason),
                daemon=True,
            )
            t.start()

        if self._host.is_running and self._host.get_autosave_cmd_enabled():
            self._host._send_internal_command("/autosavenow")
            self._host.append_console(
                "Requested /autosavenow before backup…", "system")
            self._host.after(2000, kick)
        else:
            kick()

    def _backup_worker(self, src: str, dst: str, silent: bool,
                        reason: str) -> None:
        started = time.time()
        try:
            backup_world_to_zip(
                src, dst,
                progress_cb=self._on_progress,
                cancel_flag=lambda: self._cancel_flag,
            )
            size = os.path.getsize(dst)
            elapsed = time.time() - started
            self._host.after(0, self._backup_done, dst, size, elapsed,
                              silent, reason)
        except Exception as e:
            LOG.exception("backup worker failed")
            self._host.after(0, self._backup_failed, e, silent, reason)

    def _on_progress(self, got: int, total: int) -> None:
        if self._last_progress_post + 0.25 > time.time():
            return
        self._last_progress_post = time.time()
        pct = int((got / max(1, total)) * 100)
        self._host.after(
            0,
            lambda p=pct: self._host._notify(
                f"Backup: {p}% ({got}/{total} files)",
                level="info", duration_ms=600))

    def _backup_done(self, dst: str, size: int, elapsed: float,
                     silent: bool, reason: str) -> None:
        self._in_progress = False
        self._cancel_flag = False
        self._host.append_console(
            f"✓ Backup → {os.path.basename(dst)} "
            f"({fmt_size(size)}, {elapsed:.1f}s)",
            "success")
        if not silent:
            self._host._notify(
                f"Backup complete: {os.path.basename(dst)}",
                level="success")
        self.prune_old_backups(announce=False)
        # Refresh the backup-list UI in the BACKUP tab if it exists.
        try:
            self._host._refresh_backup_list()
        except (AttributeError, Exception):
            pass

    def _backup_failed(self, err: Exception, silent: bool,
                       reason: str) -> None:
        self._in_progress = False
        self._cancel_flag = False
        self._host.append_console(
            f"Backup error ({reason}): {err}", "error")
        if not silent:
            self._host._notify(
                f"Backup failed: {err}", level="error", duration_ms=5000)

    def _prune_by_days(self, dst_root: str, days: int,
                       announce: bool) -> None:
        cutoff = time.time() - days * 86400
        deleted = 0
        try:
            for name in os.listdir(dst_root):
                if not name.startswith("backup-"):
                    continue
                full = os.path.join(dst_root, name)
                if os.path.getmtime(full) < cutoff:
                    try:
                        if os.path.isdir(full):
                            shutil.rmtree(full)
                        else:
                            os.remove(full)
                        deleted += 1
                    except OSError:
                        pass
        except OSError:
            pass
        if announce and deleted:
            self._host._notify(
                f"Pruned {deleted} backup(s) older than {days} days.",
                level="success")
