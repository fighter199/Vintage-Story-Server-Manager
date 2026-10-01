"""
host/server.py — Server control: mod checks before start, starting, stopping and
restarting the process, savegame-lock waits, reading its output,
crash handling and sending commands.

Methods of ServerManagerApp (VSSM.py), moved here unchanged.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
from typing import Callable

# ── Local packages ─────────────────────────────────────────────────────
from core.constants import (APP_VERSION, LOG, SERVER_LOG,
                              OPERATOR_ROLES)
from core.parsers import (classify_line, parse_role_response, parse_chat_message, strip_log_prefix)
from mods.checks import check_mods, scan_mods_folder
from core.crash_report import (build_report, find_game_crash_logs, summarize,
                               write_report)
from core.processes import (default_data_path)
from core.utils import (is_port_free, find_vs_port)
from ui.theme import (Theme)
from core.chat_log import (parse_chat_with_group,
                            parse_ungrouped_chat, UNGROUPED_KEY)


class ServerMixin:

    # ------------------------------------------------------------------
    # Server control
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Mod checks (MODS → Check mods, and before a manual start)
    # ------------------------------------------------------------------
    def _server_game_version(self):
        """The game version the newest savegame was saved with (the
        server's version, or a little older), or None."""
        try:
            files = sorted(self._savegame_db_files(), key=os.path.getmtime)
            if files:
                from core.world_db import read_world_meta
                return read_world_meta(files[-1]).get("game_version")
        except Exception as e:
            LOG.debug("game version unknown: %s", e)
        return None


    def _mod_problems(self) -> tuple:
        folder = self.mods_folder_var.get().strip()
        version = self._server_game_version()
        return check_mods(scan_mods_folder(folder), version), version


    def _report_mod_problems(self, problems, version) -> None:
        tags = {"error": "error", "warn": "warn", "info": "system"}
        vtext = f" (game {version})" if version else " (game version unknown)"
        if not problems:
            self.append_console(f"Mod check{vtext}: no problems found.", "success")
            return
        self.append_console(f"Mod check{vtext}: {len(problems)} finding(s):", "system")
        for p in problems:
            self.append_console(f"  [{p.level}] {p}", tags[p.level])


    def check_mods_now(self):
        """MODS → 🩺 Check mods."""
        if not self.mods_folder_var.get().strip():
            self._notify("Set the mods folder first (SETTINGS).", level="warn")
            return
        problems, version = self._mod_problems()
        self._report_mod_problems(problems, version)
        errors = sum(p.level == "error" for p in problems)
        warns = sum(p.level == "warn" for p in problems)
        if errors or warns:
            self._notify(f"Mod check: {errors} problem(s), {warns} warning(s) — "
                         "details in the console.", level="error" if errors else "warn",
                         duration_ms=6000)
        else:
            self._notify("Mod check: no problems found.", level="success")


    def _start_server_checked(self):
        """The ▶ Start button: check the mods first and ask before
        starting into a likely failure. (Restarts, scheduled and
        automatic ones included, go straight to start_server.)"""
        if not self.is_running and not self._shutdown_in_progress \
                and self.mods_folder_var.get().strip():
            try:
                problems, version = self._mod_problems()
            except Exception:
                LOG.exception("mod check before start failed")
                problems, version = [], None
            errors = [p for p in problems if p.level == "error"]
            if errors:
                self._report_mod_problems(problems, version)
                listing = "\n".join(f"• {p}" for p in errors[:8])
                more = f"\n…and {len(errors) - 8} more" if len(errors) > 8 else ""
                if not messagebox.askyesno(
                        "Mod problems",
                        f"{len(errors)} mod problem(s) will probably stop the "
                        f"server from starting or crash it:\n\n{listing}{more}"
                        "\n\nStart anyway?", icon="warning", parent=self):
                    return
        self.start_server()


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
        if self._backup_manager.in_progress:
            # Never launch while a zip of the world is being written —
            # the server's first writes would race the backup.
            self._notify("Backup in progress — start once it finishes.",
                         level="warn")
            return
        if self._world_edit_in_progress:
            self._notify("World map is editing the savegame — start once "
                         "it finishes.", level="warn")
            return
        if self.backup_before_start_var.get():
            # Snapshot while the world is quiescent and only launch once
            # the zip is complete. (Previously the backup and the server
            # launch ran concurrently, so the server's first writes
            # could land mid-backup.)
            self.append_console(
                "Pre-start backup — server will launch when it "
                "completes…", "system")
            self._start_async_backup(
                silent=True, reason="pre-start",
                on_done=lambda _ok: self._launch_server_process())
            return
        self._launch_server_process()


    def _launch_server_process(self):
        """Spawn the server process. Split out of start_server so the
        pre-start backup can defer the launch until the zip is done."""
        if self.is_running or self._shutdown_in_progress:
            return
        if self._world_edit_in_progress:
            self._notify("World map is editing the savegame — server not "
                         "started.", level="warn")
            return
        exe = self.server_path_var.get().strip()
        if not exe or not os.path.isfile(exe):
            self._notify("Server executable not set or invalid.", level="error")
            return
        server_dir = os.path.dirname(exe)
        port = find_vs_port(server_dir)
        if not is_port_free(port):
            self.append_console(
                f"WARNING: Port {port} is already in use. Server may fail to bind.",
                "warn")
        # On Windows, VintagestoryServer.exe inherits the parent's
        # console by default. When VS detects an attached console it
        # reads commands from there directly, *not* from our piped
        # stdin — so /stop, /list, etc. silently disappear into the
        # void. We work around it by detaching the child into its own
        # process group AND suppressing the per-child console window;
        # with no console attached, VS falls back to reading stdin,
        # which is exactly the pipe we own.
        popen_kwargs = dict(
            cwd=server_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        if sys.platform.startswith("win"):
            # CREATE_NEW_PROCESS_GROUP (0x00000200) detaches from our
            #   console group so Ctrl+C in our window can't propagate
            #   and instakill the server.
            # CREATE_NO_WINDOW (0x08000000) suppresses the child's
            #   console; its stdout/stderr is already piped to us.
            popen_kwargs["creationflags"] = 0x00000200 | 0x08000000
        try:
            self.server_process = subprocess.Popen([exe], **popen_kwargs)
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
        # Arm the autorun scheduler at server-start time so all
        # interval deadlines are anchored to a fresh wall clock.
        try:
            self._autorun_scheduler.start()
        except Exception:
            LOG.exception("autorun scheduler start failed")
        # Fresh queue per run — a stale end-of-stream marker left over
        # from the previous run's reader thread must never be misread
        # as THIS process exiting (see _read_output).
        self.output_queue = queue.Queue()
        reader = threading.Thread(target=self._read_output, daemon=True)
        reader.start()
        self.after(100, self._process_queue)
        self._schedule_autosave()
        self._apply_cron_schedule()


    def stop_server(self, on_done: Callable | None = None,
                    skip_player_check: bool = False):
        """Stop the server.

        skip_player_check: if True, bypass the
        'Check for players before manual shutdown' guard. Used by
        restart_server (which has its own guard) and the close-window
        path (which already prompts the user separately).
        """
        if not self.is_running and not self.server_process:
            if on_done:
                on_done()
            return
        if self._shutdown_in_progress:
            if on_done:
                self._shutdown_callbacks.append(on_done)
            return
        # Guard: ask the user what to do when players are online.
        # We check this BEFORE registering the callback so that if the
        # user cancels, we don't leave a stale on_done waiting.
        if (not skip_player_check
                and self.check_players_before_shutdown_var.get()
                and self._players):
            self._prompt_players_online_action(
                action_label="shutdown",
                on_continue_now=lambda: self.stop_server(
                    on_done=on_done, skip_player_check=True),
                on_wait_until_empty=lambda: self._wait_for_empty_then(
                    lambda: self.stop_server(
                        on_done=on_done, skip_player_check=True),
                    action_label="shutdown"))
            return
        if on_done:
            self._shutdown_callbacks.append(on_done)
        # Stop backup: remember the request and take the snapshot in
        # _finalize_stop, AFTER the process has exited. Zipping while
        # the server flushed its final world save risked catching the
        # savegame DB mid-write; post-exit the files are quiescent.
        if self.backup_before_stop_var.get():
            self._pending_stop_backup = True
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


    # ------------------------------------------------------------------
    # Savegame DB lock probe (VSSM_DB_LOCK_RESTART_FIX_V1)
    # ------------------------------------------------------------------
    # On Windows, the SQLite handles backing VintagestoryServer's
    # *.vcdbs world database can stay locked for hundreds of ms to a
    # few seconds after the server process exits — especially when
    # the exit was via terminate()/kill() rather than a graceful
    # /stop. Launching a fresh VS process during that window crashes
    # it at "Loading configuration → opening savegame".
    #
    # We can detect the lock cheaply: try to open the file in r+b mode
    # (read+write, no truncation, no append). Windows refuses with
    # PermissionError if any other process holds an exclusive handle,
    # which is exactly the failure mode we want to wait out. Stray
    # -journal / -wal sidecar files also indicate SQLite recovery
    # may be needed and are treated as "not ready yet".
    #
    # The wait is bounded: after WAIT_CAP_SECS we surface a clear
    # error and DO NOT launch the server, instead of blindly retrying
    # into an inevitable crash. This is what makes the difference
    # between "VSSM recovered on its own" and "user had to restart
    # the manager to break the loop."

    def _savegame_db_files(self) -> list:
        """Return absolute paths of all .vcdbs files in the world
        folder. Empty list if the folder is unconfigured / unreadable
        — callers treat empty as "nothing to wait on" and proceed."""
        try:
            world = self.world_folder_var.get().strip()
        except Exception:
            world = ""
        if not world or not os.path.isdir(world):
            return []
        try:
            return [os.path.join(world, n) for n in os.listdir(world)
                    if n.lower().endswith(".vcdbs")]
        except OSError:
            return []


    def _savegame_lock_status(self) -> tuple:
        """Probe every .vcdbs in the world folder and report which
        files (if any) are still locked or have stray sidecar files.

        Returns (locked_paths, sidecar_paths). When both are empty,
        the DB is safe to open.
        """
        locked = []
        sidecars = []
        for db in self._savegame_db_files():
            # An exclusive r+b open is the canonical "is anything else
            # holding this file?" check on Windows. The file MUST exist
            # — a missing file means there's no lock to wait on.
            try:
                with open(db, "r+b"):
                    pass
            except FileNotFoundError:
                continue
            except (PermissionError, OSError) as e:
                LOG.debug("savegame still locked: %s (%s)", db, e)
                locked.append(db)
                continue
            # Sidecar files mean SQLite didn't finish a clean shutdown.
            # The .vcdbs itself may be openable, but the next process
            # has to roll the WAL/journal forward — that's the path
            # that occasionally tripped the "not writable" error.
            for suffix in ("-journal", "-wal", "-shm"):
                sc = db + suffix
                if os.path.exists(sc):
                    try:
                        size = os.path.getsize(sc)
                    except OSError:
                        size = 0
                    # An empty -shm is normal; a non-empty -journal/-wal
                    # is what we care about. Tracking all of them keeps
                    # the surfaced error message useful.
                    if size > 0 or suffix == "-journal":
                        sidecars.append(sc)
        return locked, sidecars


    def _wait_for_savegame_unlocked_then(self, then_call, *,
                                          context: str = "restart",
                                          attempts: int = 0,
                                          started: float = 0.0) -> None:
        """Poll the savegame .vcdbs file every 500 ms; once it's
        writable AND has no stray sidecar files, call `then_call()`.

        Bounded at WAIT_CAP_SECS so we never hang forever. If the
        cap is hit, we surface a notification + log line and DO NOT
        call then_call — the user can manually retry once the lock
        clears (almost always within a few seconds of seeing the
        error).
        """
        WAIT_CAP_SECS = 60
        POLL_MS = 500
        if started <= 0:
            started = time.time()
        locked, sidecars = self._savegame_lock_status()
        if not locked and not sidecars:
            if attempts > 0:
                waited_ms = int((time.time() - started) * 1000)
                self.append_console(
                    f"Savegame lock cleared after {waited_ms} ms — "
                    f"proceeding with {context}.",
                    "system")
                LOG.info("savegame lock cleared after %d ms (%s)",
                         waited_ms, context)
            try:
                then_call()
            except Exception:
                LOG.exception("post-lock-wait callback failed (%s)", context)
            return
        elapsed = time.time() - started
        if elapsed >= WAIT_CAP_SECS:
            # Don't relaunch into a guaranteed crash. Surface the
            # exact files still locked so the user knows what to
            # check (antivirus, leftover VS process, etc.).
            blockers = []
            if locked:
                blockers.append("locked: " + ", ".join(
                    os.path.basename(p) for p in locked))
            if sidecars:
                blockers.append("sidecars: " + ", ".join(
                    os.path.basename(p) for p in sidecars))
            detail = "; ".join(blockers) if blockers else "unknown"
            msg = (f"Savegame still locked after {WAIT_CAP_SECS}s — "
                   f"{context} aborted ({detail}). Check for a "
                   f"leftover VintagestoryServer.exe in Task Manager, "
                   f"or for antivirus scanning the world folder.")
            self.append_console(msg, "error")
            self._notify(
                "Savegame locked — server NOT restarted.",
                level="error", duration_ms=8000)
            LOG.error("savegame lock probe gave up after %ds (%s): %s",
                      WAIT_CAP_SECS, context, detail)
            return
        # Log progress on the first poll only — periodic polling
        # logs would spam the console for slow-clearing locks.
        if attempts == 0:
            self.append_console(
                f"Waiting for savegame lock to clear before {context}…",
                "system")
        self.after(POLL_MS, lambda: self._wait_for_savegame_unlocked_then(
            then_call,
            context=context,
            attempts=attempts + 1,
            started=started))


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
        # End every active session before we forget the player list,
        # so the just-finished sessions land in totals.
        try:
            self._player_timers.reset_all()
            self._persist_player_totals()
        except Exception:
            LOG.exception("player timer flush on stop failed")
        self._players = []
        self._rerender_players()
        self._operators.clear()
        self._pending_role_query.clear()
        self._player_roles.clear()
        self.server_process = None
        self._psutil_proc = None
        self.cancel_autosave_job()
        try:
            self._autorun_scheduler.stop()
        except Exception:
            pass
        self._cancel_cron_schedule()
        cbs = self._shutdown_callbacks[:]
        self._shutdown_callbacks.clear()

        def _run_callbacks(_ok: bool = True):
            for cb in cbs:
                try:
                    cb()
                except Exception:
                    pass

        # If a stop-time backup was requested, take it NOW — the process
        # has exited, so the world files are quiescent — and only then
        # fire the shutdown callbacks (restart's relaunch, window close).
        # This also fixes app-exit truncating the backup: the daemon
        # worker thread used to be killed when the window closed.
        if self._pending_stop_backup:
            self._pending_stop_backup = False
            self.append_console(
                "Stop backup starting — any restart/close continues "
                "when it finishes…", "system")
            self._start_async_backup(silent=True, reason="stop",
                                     on_done=_run_callbacks)
        else:
            _run_callbacks()


    def restart_server(self):
        """Public restart entry-point. Honors the
        'Check for players before manual restart' setting."""
        if not self.is_running:
            self._notify("Server is not running.", level="warn")
            return
        # Guard: ask the user what to do when players are online.
        if (self.check_players_before_restart_var.get()
                and self._players):
            self._prompt_players_online_action(
                action_label="restart",
                on_continue_now=self._do_restart_now,
                on_wait_until_empty=lambda: self._wait_for_empty_then(
                    self._do_restart_now, action_label="restart"))
            return
        self._do_restart_now()


    def _do_restart_now(self):
        """Unconditional restart — bypasses the player-check guard.
        Called either directly (when the guard is off / no players)
        or after the user picks 'continue anyway' / once the server
        is empty.

        VSSM_DB_LOCK_RESTART_FIX_V1 — waits for the savegame DB to actually
        release its file lock before relaunching. The previous 2s
        sleep was insufficient on Windows after a forced terminate(),
        leading to "Cannot open savegame database file ... it seems
        to be not writable!" crashes that the auto-restart loop then
        compounded into a crash-loop trip.
        """
        if not self.is_running:
            return
        self.append_console("Restarting server…", "system")
        self.stop_server(
            skip_player_check=True,
            on_done=lambda: self._wait_for_savegame_unlocked_then(
                self.start_server, context="restart"))


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
        # Capture the queue for THIS run — start_server swaps in a fresh
        # queue per launch, so a lingering reader from a previous run can
        # never inject its end-of-stream marker (None) into the new run
        # and trigger spurious crash detection.
        q = self.output_queue
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
                q.put(text)
        except Exception as e:
            LOG.debug("Reader thread ending: %s", e)
        q.put(None)


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
        # Each try-block guards against a single broken line crashing the
        # reader thread. They USED to be silent (except Exception: pass);
        # now they LOG.exception so a real bug surfaces in vserverman.log
        # instead of disappearing (review §2.4).
        try:
            tag = classify_line(stripped)
        except Exception:
            LOG.exception("classify_line failed on: %r", stripped)
            tag = "info"
        try:
            self.append_console(stripped, tag)
        except Exception:
            LOG.exception("append_console failed on: %r", stripped)
        try:
            self._parse_player_event(stripped)
        except Exception:
            LOG.exception("_parse_player_event failed on: %r", stripped)
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
            LOG.exception("parse_role_response failed on: %r", stripped)
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
        # Chat-log capture: feed grouped chat lines into the per-group
        # store + tab. Uses the dedicated parser that ALSO returns the
        # group ID; the older parse_chat_message above is kept because
        # it handles the angle-bracket form for custom-command dispatch.
        try:
            if tag == "chat":
                gid, p, m = parse_chat_with_group(
                    raw, strip_fn=strip_log_prefix)
                if gid is not None and p and self._chat_log_tab is not None:
                    now = time.time()
                    self._chat_store.append(gid, p, m or "", now=now)
                    # Marshal the UI update onto the Tk main thread —
                    # _handle_server_line runs from the output-queue
                    # processor which is already on the main thread,
                    # so this is mostly defensive.
                    self.after_idle(
                        self._chat_log_tab.on_new_entry, gid, p, m or "", now)
                else:
                    # No group ID — try the ungrouped parser for
                    # proximity / RP-mod chat shapes like
                    # 'Dan mentions "hello"'. The verb is folded
                    # into the rendered message so the roleplay
                    # flavor (mentions / states / exclaims / …) is
                    # preserved without a schema change.
                    verb, p2, body = parse_ungrouped_chat(
                        raw, strip_fn=strip_log_prefix)
                    if p2 and self._chat_log_tab is not None:
                        now = time.time()
                        rendered = (f"{verb} {body}"
                                    if verb else body)
                        self._chat_store.append(
                            UNGROUPED_KEY, p2, rendered, now=now)
                        self.after_idle(
                            self._chat_log_tab.on_new_entry,
                            UNGROUPED_KEY, p2, rendered, now)
        except Exception:
            LOG.exception("chat log capture failed")
        try:
            SERVER_LOG.info(stripped)
        except Exception:
            pass


    def _on_process_exit_unexpected(self):
        was_stopping = self._shutdown_in_progress
        started, proc = self.start_time, self.server_process
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
        self._write_crash_report(started, proc)
        self._record_crash()
        self._players = []
        self._rerender_players()
        self._operators.clear()
        self._pending_role_query.clear()
        self._player_roles.clear()
        self.server_process = None
        self._psutil_proc = None
        if self.autorestart_var.get():
            if self._crash_loop_tripped():
                msg = (f"Auto-restart disabled: {self.CRASH_LIMIT}+ crashes "
                       f"in {self.CRASH_WINDOW_SECS}s. Fix root cause and restart manually.")
                self.append_console(msg, "error")
                self._notify(msg, level="error", duration_ms=8000)
                return
            # VSSM_DB_LOCK_RESTART_FIX_V1 — gate the auto-restart on the
            # savegame DB actually being writable. A crash caused by
            # a stale DB lock will recur every 5s if we blindly relaunch;
            # the wait helper polls the .vcdbs file until it's free
            # (or surfaces a clear error after a safety cap).
            self.append_console("Auto-restart enabled, waiting for savegame lock to clear…", "system")
            self.after(5000, lambda: self._wait_for_savegame_unlocked_then(
                self.start_server, context="auto-restart"))


    def _write_crash_report(self, started, proc) -> str | None:
        """Save logs/crash-reports/crash-<time>.txt for an unexpected exit
        and say where it is, with a one-line guess at the cause."""
        exit_code = None
        try:
            if proc is not None:
                exit_code = proc.wait(timeout=2)
        except Exception:
            pass
        exe = self.server_path_var.get().strip()
        world = self.world_folder_var.get().strip()
        folders = [os.path.join(os.path.dirname(os.path.abspath(world)), "Logs")
                   if world else "",
                   os.path.join(default_data_path(), "Logs"),
                   os.path.join(os.path.dirname(exe), "Logs") if exe else ""]
        console = [f"[{ts}] {text}" for ts, text, _tag in self.all_output_lines]
        try:
            game_logs = find_game_crash_logs(
                folders, since=(started or time.time()) - 5)
            text = build_report(
                exit_code=exit_code, started=started, ended=time.time(),
                console=console, players=list(self._players),
                game_logs=game_logs, app_version=APP_VERSION, server_path=exe)
            from core.constants import log_dir
            path = write_report(log_dir(), text)
        except Exception:
            LOG.exception("writing the crash report failed")
            return None
        cause = summarize([t for _ts, t, _tag in self.all_output_lines
                           if t != "Server process exited."])
        code = f" (exit code {exit_code})" if exit_code is not None else ""
        self.append_console(f"Crash report saved{code}: {path}", "error")
        if cause:
            self.append_console(f"Likely cause: {cause}", "error")
        self._notify(f"Server crashed{code}. " + (f"Likely cause: {cause[:120]}"
                     if cause else "See the crash report in logs/crash-reports."),
                     level="error", duration_ms=10000)
        return path


    def _record_crash(self):
        now = time.time()
        self._crash_times.append(now)
        cutoff = now - self.CRASH_WINDOW_SECS
        while self._crash_times and self._crash_times[0] < cutoff:
            self._crash_times.popleft()


    def _crash_loop_tripped(self) -> bool:
        return len(self._crash_times) >= self.CRASH_LIMIT


    def _send_internal_command(self, cmd: str) -> bool:
        """Write a single command line to the server's stdin pipe.

        Vintage Story's CLI parses one command per line. We use CRLF on
        Windows because some VS builds detect the host platform and
        expect the platform-native line ending; the server's reader on
        Linux/macOS accepts both LF and CRLF, so CRLF is safe everywhere.
        """
        proc = self.server_process
        if not proc or proc.poll() is not None:
            LOG.debug("_send_internal_command: no live process (cmd=%r)", cmd)
            return False
        if not proc.stdin or proc.stdin.closed:
            LOG.warning("_send_internal_command: stdin closed (cmd=%r)", cmd)
            return False
        line_ending = b"\r\n" if sys.platform.startswith("win") else b"\n"
        payload = cmd.encode("utf-8", errors="replace") + line_ending
        try:
            # Serialise writes — multiple threads can land here
            # (autorun, cron, manual, chat dispatcher). Without the
            # lock, two interleaved writes would corrupt the byte
            # stream the server reads (review §3.2).
            with self._stdin_lock:
                proc.stdin.write(payload)
                proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            LOG.error("_send_internal_command pipe error: %s (cmd=%r)", e, cmd)
            return False
        except Exception as e:
            LOG.error("_send_internal_command failed: %s (cmd=%r)", e, cmd)
            return False
        LOG.debug("Sent: %r (%d bytes)", cmd, len(payload))
        return True


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
