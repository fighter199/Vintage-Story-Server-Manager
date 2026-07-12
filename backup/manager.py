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
from core.utils import (backup_world_to_zip, backup_single_file_to_zip,
                        restore_backup_zip, fmt_size)


# ----------------------------------------------------------------------
# Backup families — filename prefix per backup reason
# ----------------------------------------------------------------------
# Start/stop backups get their own filename families so they're
# recognisable at a glance in the destination folder AND pruned
# independently of the regular manual/auto backups.
DEFAULT_PREFIX = "backup-"
REASON_PREFIXES = {
    "pre-start": "startbackup-",
    "stop":      "stopbackup-",
}


def prefix_for_reason(reason) -> str:
    """Map a backup reason ('manual', 'autosave', 'pre-start', 'stop')
    to the filename prefix of its family."""
    return REASON_PREFIXES.get(reason or "", DEFAULT_PREFIX)


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
    def get_max_start_backups(self) -> int: ...
    def get_max_stop_backups(self) -> int: ...
    def get_retention_mode(self) -> str: ...   # 'count' | 'days'
    def get_autosave_cmd_enabled(self) -> bool: ...
    def get_server_backups_dir(self) -> str: ...


class BackupManager:
    """Owns the in-flight backup state and exposes the operations the
    Backup tab buttons call."""

    def __init__(self, host: _HostProtocol):
        self._host = host
        self._in_progress = False
        self._cancel_flag = False
        self._last_progress_post = 0.0
        # Completion callback for the current run — called exactly once
        # with True (backup written) or False (failed / skipped), on the
        # Tk main thread. Lets the host sequence actions (server launch,
        # shutdown callbacks) strictly AFTER the zip is finished.
        self._on_done: Optional[Callable[[bool], None]] = None

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
        dst = os.path.join(
            dst_root, f"{prefix_for_reason('manual')}{timestamp}.zip")
        self._start_async_backup(dst=dst, silent=silent, reason="manual")
        return dst

    def start_async_backup(
        self,
        dst: Optional[str] = None,
        silent: bool = False,
        reason: str = "manual",
        on_done: Optional[Callable[[bool], None]] = None,
    ) -> None:
        """Public entry point used by auto-save / pre-start hooks.

        on_done, when given, is invoked exactly once on the Tk main
        thread: on_done(True) after a successful backup, on_done(False)
        if the backup failed or couldn't start (invalid folders,
        another backup already running)."""
        self._start_async_backup(dst=dst, silent=silent, reason=reason,
                                 on_done=on_done)

    def cancel_active_backup(self) -> None:
        if self._in_progress:
            self._cancel_flag = True
            self._host.append_console("Backup cancel requested…", "warn")

    def prune_old_backups(self, announce: bool = True) -> None:
        """Apply retention to every backup family independently:

          backup-*        — manual/auto backups; honours the count/days
                            retention mode + 'Keep last N' setting
          startbackup-*   — pre-start backups; keep-last-N cap
          stopbackup-*    — stop backups; keep-last-N cap

        A cap of 0 means keep-everything for that family."""
        dst_root = self._host.get_backup_dir()
        if not dst_root or not os.path.isdir(dst_root):
            return
        deleted = 0
        main_keep = self._family_cap("get_max_backups")
        if main_keep > 0:
            if self._host.get_retention_mode() == "days":
                self._prune_by_days(dst_root, main_keep, announce)
            else:
                deleted += self._prune_family(dst_root, DEFAULT_PREFIX,
                                              main_keep)
        for prefix, getter in (
                ("startbackup-", "get_max_start_backups"),
                ("stopbackup-",  "get_max_stop_backups")):
            keep = self._family_cap(getter)
            if keep > 0:
                deleted += self._prune_family(dst_root, prefix, keep)
        if announce and deleted:
            self._host._notify(
                f"Pruned {deleted} old backup(s).", level="success")
        if deleted:
            try:
                self._host._refresh_backup_list()
            except Exception:
                pass

    def _family_cap(self, getter_name: str) -> int:
        """Read a per-family keep-last-N cap off the host. 0 = keep all
        (also the result when the host doesn't implement the getter)."""
        try:
            return max(0, int(getattr(self._host, getter_name)()))
        except (AttributeError, ValueError, TypeError):
            return 0

    def _prune_family(self, dst_root: str, prefix: str,
                      max_keep: int) -> int:
        """Keep the newest `max_keep` backups whose filename starts with
        `prefix`; delete the rest. Returns the number deleted."""
        try:
            entries: list[tuple[float, str]] = []
            for name in os.listdir(dst_root):
                if not name.startswith(prefix):
                    continue
                full = os.path.join(dst_root, name)
                if (os.path.isdir(full)
                        or (os.path.isfile(full) and name.endswith(".zip"))):
                    entries.append((os.path.getmtime(full), full))
        except OSError:
            return 0
        entries.sort(key=lambda x: x[0], reverse=True)
        deleted = 0
        for _, path in entries[max_keep:]:
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                deleted += 1
            except OSError as e:
                self._host.append_console(
                    f"Prune error on {path}: {e}", "error")
        return deleted

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
        on_done: Optional[Callable[[bool], None]] = None,
    ) -> None:
        if self._in_progress:
            self._invoke_done(on_done, False)
            return
        src = self._host.get_world_folder()
        dst_root = self._host.get_backup_dir()
        if not src or not os.path.isdir(src) or not dst_root:
            # Backup not configured / impossible — report "not done" so
            # a sequenced action (server launch, shutdown callback)
            # still proceeds rather than hanging forever.
            self._invoke_done(on_done, False)
            return
        if dst is None:
            try:
                os.makedirs(dst_root, exist_ok=True)
            except OSError:
                pass
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dst = os.path.join(
                dst_root, f"{prefix_for_reason(reason)}{timestamp}.zip")
        self._in_progress = True
        self._cancel_flag = False
        self._on_done = on_done
        self._host.append_console(
            f"Backup starting ({reason}) → {os.path.basename(dst)}",
            "system")
        # Strategy (live /genbackup vs direct zip) is decided inside
        # the worker, off the Tk thread.
        t = threading.Thread(
            target=self._backup_worker,
            args=(src, dst, silent, reason),
            daemon=True,
        )
        t.start()

    # How long /genbackup gets to CREATE its output file before we fall
    # back to the legacy live zip (misconfigured world folder, ancient
    # server build, …). The file appears at the start of the copy and
    # then grows, so this only needs to cover command latency.
    GENBACKUP_APPEAR_TIMEOUT = 45
    # Hard cap on the whole server-side copy — a big world takes a few
    # minutes; past this something is wrong and the backup fails.
    GENBACKUP_TOTAL_TIMEOUT = 15 * 60

    def _backup_worker(self, src: str, dst: str, silent: bool,
                        reason: str) -> None:
        started = time.time()
        try:
            if self._host.is_running:
                # Zipping a live world risks catching the .vcdbs SQLite
                # DB mid-write (chunk gen, autosave) — use the server's
                # own consistent-snapshot mechanism instead.
                self._live_backup(src, dst)
            else:
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

    # ------------------------------------------------------------------
    # Live backup via /genbackup (server running)
    # ------------------------------------------------------------------
    def _console(self, msg: str, tag: str = "system") -> None:
        """Marshal a console line onto the Tk thread (worker-safe)."""
        try:
            self._host.after(0, self._host.append_console, msg, tag)
        except Exception:
            pass

    def _live_backup(self, src: str, dst: str) -> None:
        """Backup while the server is running.

        Vintage Story has no 'pause saving' command, so a direct zip of
        the world folder can capture the .vcdbs database mid-write and
        produce a corrupt backup. The known-good mitigation is the
        server's own `/genbackup` command, which writes a CONSISTENT
        copy of the live savegame into the server's Backups folder
        without pausing play. We trigger it, wait for the copy to
        finish, zip THAT file (stored under the live savegame's own
        name so restores round-trip), and remove the intermediate copy.

        Falls back to the legacy direct zip — with a clear warning —
        when the live savegame can't be identified or /genbackup never
        produces a file.
        """
        try:
            vcdbs = [n for n in os.listdir(src)
                     if n.lower().endswith(".vcdbs")]
        except OSError:
            vcdbs = []
        if len(vcdbs) != 1:
            self._console(
                f"⚠ Can't identify the live savegame ({len(vcdbs)} .vcdbs "
                f"files in the world folder) — falling back to direct "
                f"zip. Consistency not guaranteed.", "warn")
            self._legacy_live_zip(src, dst)
            return
        live_name = vcdbs[0]
        backups_dir = ""
        try:
            backups_dir = self._host.get_server_backups_dir() or ""
        except Exception:
            pass
        if not backups_dir:
            self._console(
                "⚠ Server Backups folder unknown — falling back to "
                "direct zip. Consistency not guaranteed.", "warn")
            self._legacy_live_zip(src, dst)
            return
        before: set = set()
        if os.path.isdir(backups_dir):
            try:
                before = set(os.listdir(backups_dir))
            except OSError:
                pass
        if not self._host._send_internal_command("/genbackup"):
            self._console(
                "⚠ Could not send /genbackup — falling back to direct "
                "zip. Consistency not guaranteed.", "warn")
            self._legacy_live_zip(src, dst)
            return
        self._console(
            "Requested consistent server-side backup (/genbackup)…")
        new_file = self._wait_for_genbackup_file(backups_dir, before)
        if new_file is None:
            self._console(
                f"⚠ /genbackup produced no file within "
                f"{self.GENBACKUP_APPEAR_TIMEOUT}s — falling back to "
                f"direct zip. Consistency not guaranteed.", "warn")
            self._legacy_live_zip(src, dst)
            return
        self._wait_until_stable(new_file)
        size = os.path.getsize(new_file)
        self._console(
            f"Server-side backup ready ({fmt_size(size)}) — zipping…")
        # Store under "<WorldFolderName>/<live savegame name>" so the
        # zip has the same layout as a stopped-server backup and the
        # restore path drops it straight into the configured world.
        arcname = (f"{os.path.basename(os.path.normpath(src))}/"
                   f"{live_name}")
        backup_single_file_to_zip(new_file, dst, arcname=arcname)
        # We caused this file to exist — remove it so the server's own
        # Backups folder doesn't silently double the disk usage.
        try:
            os.remove(new_file)
        except OSError as e:
            LOG.warning("could not remove genbackup source %s: %s",
                        new_file, e)

    def _wait_for_genbackup_file(self, backups_dir: str, before: set):
        """Poll the server's Backups folder until a new file appears.
        Returns its path, or None on timeout. The folder may not exist
        yet (first-ever backup) — the server creates it."""
        deadline = time.time() + self.GENBACKUP_APPEAR_TIMEOUT
        while time.time() < deadline:
            if self._cancel_flag:
                raise RuntimeError("Backup cancelled.")
            current: set = set()
            if os.path.isdir(backups_dir):
                try:
                    current = set(os.listdir(backups_dir))
                except OSError:
                    pass
            new = sorted(current - before)
            if new:
                return os.path.join(backups_dir, new[0])
            time.sleep(1.0)
        return None

    def _wait_until_stable(self, path: str) -> None:
        """Block until `path` has stopped growing AND is no longer held
        open for writing (Windows exclusive-open probe). Raises on
        cancel or when GENBACKUP_TOTAL_TIMEOUT is exceeded."""
        deadline = time.time() + self.GENBACKUP_TOTAL_TIMEOUT
        last_size = -1
        while True:
            if self._cancel_flag:
                raise RuntimeError("Backup cancelled.")
            if time.time() > deadline:
                raise RuntimeError(
                    "Server-side backup did not finish within "
                    f"{self.GENBACKUP_TOTAL_TIMEOUT // 60} minutes.")
            try:
                size = os.path.getsize(path)
            except OSError:
                size = -1
            if size > 0 and size == last_size:
                try:
                    with open(path, "r+b"):
                        pass
                    return
                except OSError:
                    pass  # still locked by the server — keep waiting
            last_size = size
            time.sleep(2.0)

    def _legacy_live_zip(self, src: str, dst: str) -> None:
        """Direct zip of the live world folder — the pre-/genbackup
        behaviour. Only used as a fallback; callers warn first."""
        if self._host.get_autosave_cmd_enabled():
            if self._host._send_internal_command("/autosavenow"):
                self._console("Requested /autosavenow before backup…")
                # Give the server a moment to finish the save.
                for _ in range(4):
                    if self._cancel_flag:
                        raise RuntimeError("Backup cancelled.")
                    time.sleep(0.5)
        backup_world_to_zip(
            src, dst,
            progress_cb=self._on_progress,
            cancel_flag=lambda: self._cancel_flag,
        )

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
        cb, self._on_done = self._on_done, None
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
        except Exception:
            pass
        self._invoke_done(cb, True)

    def _backup_failed(self, err: Exception, silent: bool,
                       reason: str) -> None:
        self._in_progress = False
        self._cancel_flag = False
        cb, self._on_done = self._on_done, None
        self._host.append_console(
            f"Backup error ({reason}): {err}", "error")
        if not silent:
            self._host._notify(
                f"Backup failed: {err}", level="error", duration_ms=5000)
        self._invoke_done(cb, False)

    @staticmethod
    def _invoke_done(cb: Optional[Callable[[bool], None]],
                     ok: bool) -> None:
        if cb is None:
            return
        try:
            cb(ok)
        except Exception:
            LOG.exception("backup on_done callback failed")

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
