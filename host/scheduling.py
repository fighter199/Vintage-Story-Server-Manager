"""
host/scheduling.py — Scheduling: autosave, cron-style restarts and the autorun
scheduler hooks.

Methods of ServerManagerApp (VSSM.py), moved here unchanged.
"""
from __future__ import annotations


# ── Local packages ─────────────────────────────────────────────────────
from core.constants import (LOG)
from core.parsers import (parse_cron_expr, seconds_until_next)


class SchedulingMixin:

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
        # VSSM_DB_LOCK_RESTART_FIX_V1 — the previous version sent /autosavenow
        # here AND _start_async_backup(reason="autosave") sent it again
        # ~20ms later (it has its own pre-backup save hook). One per
        # autosave is correct; two was wasted IO and visible in the
        # logs as duplicate "/autosavenow" sends at every interval.
        # _start_async_backup handles the save+backup sequence itself,
        # so we just announce the tick and hand off.
        if self.is_running:
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
        # If the user opted in to deferring scheduled restarts when
        # players are online, hand off to the wait-loop instead of
        # firing immediately. The wait-loop polls _players and runs
        # the restart as soon as the list is empty.
        if (self.check_players_before_scheduled_restart_var.get()
                and self._players):
            self.append_console(
                f"Scheduled restart deferred — {len(self._players)} "
                f"player(s) online. Will fire once the server is empty.",
                "warn")
            self._notify(
                "Scheduled restart deferred until empty.",
                level="info", duration_ms=4000)
            try:
                self.broadcast(
                    "Scheduled restart deferred until all players "
                    "have logged off.")
            except Exception:
                pass
            self._wait_for_empty_then(
                self._cron_fire_now, action_label="scheduled restart")
            return
        self._cron_fire_now()


    def _cron_fire_now(self):
        """Fire a scheduled restart immediately, then schedule the
        next cron tick. Bypasses the manual-restart player-check
        guard since the cron path has its own wait-for-empty logic."""
        if not self.is_running:
            self._schedule_next_cron()
            return
        self.append_console("Scheduled restart firing…", "system")
        # Use _do_restart_now so we don't double-prompt the user;
        # the cron path's player-check is the deferral above.
        self._do_restart_now()
        self.after(10000, self._schedule_next_cron)


    def _cancel_cron_schedule(self):
        if self._cron_job_id:
            try:
                self.after_cancel(self._cron_job_id)
            except Exception:
                pass
            self._cron_job_id = None
        self._cancel_restart_warnings()
        # Also cancel any deferred-restart wait that's still running.
        self._cancel_deferred_restart_wait()


    # ------------------------------------------------------------------
    # Autorun integration
    # ------------------------------------------------------------------
    def _autorun_rules_provider(self):
        """Provider used by AutorunScheduler. Reads the live rule list
        from the active profile every tick so UI edits take effect
        immediately."""
        try:
            from core.settings import load_autorun_rules
            return load_autorun_rules(self._settings)
        except Exception:
            return []


    def _autorun_send(self, cmd: str) -> None:
        """Send callback for AutorunScheduler. Echoes into the local
        console for visibility, and only writes to stdin while the
        server is actually running."""
        if not self.is_running:
            return
        self._send_internal_command(cmd)
        try:
            self.append_console(f"[autorun] → {cmd}", "system")
        except Exception:
            pass
        LOG.info("autorun  %s", cmd)


    def _autorun_player_count(self) -> int:
        try:
            return len(self._players or [])
        except Exception:
            return 0


    def _autorun_record_audit(self, audit) -> None:
        """Forward a scheduler decision to the tab, if it's been built."""
        if self._autorun_tab is not None:
            try:
                self._autorun_tab.record_audit(audit)
            except Exception:
                LOG.exception("autorun audit record failed")


    def _autorun_rules_changed(self) -> None:
        """Hook for the tab to call after the user saves a rule.
        The scheduler reads the provider every tick, so there's no
        cache to invalidate — but if a brand-new rule has run_on_start
        set, we'd miss it (start() already ran). Best-effort: if the
        server is up and the new rule's run_on_start flag is set,
        wait until the next tick which will arm it normally."""
        # Currently a no-op; reserved as a host-side hook so the tab
        # has a clean place to call into without poking scheduler
        # internals. Live edits already work via the per-tick provider.
        return


    def _autorun_fire_rule(self, rule_name: str) -> int:
        """Host hook for the AUTORUN tab's "Run on save" checkbox and
        "▶ Run Now" button. Asks the scheduler to fire the named rule
        once, immediately, and reset its interval so the next periodic
        fire is `interval_secs` from now.

        Returns the number of console commands actually sent. A return
        of 0 means a gate (enabled, pause_when_empty) blocked the fire,
        the scheduler isn't started (no live server), or the named
        rule doesn't exist. The scheduler emits an audit record in
        every case, so the tab's audit strip explains the outcome.
        """
        try:
            return self._autorun_scheduler.fire_now(rule_name)
        except Exception:
            LOG.exception("autorun fire_now failed for %r", rule_name)
            return 0


    def _tick_autorun(self) -> None:
        """1Hz tick: ask the scheduler to process any due rules."""
        try:
            if self.is_running:
                self._autorun_scheduler.tick()
        except Exception:
            LOG.exception("autorun tick failed")
        # Always reschedule, even when stopped, so we pick up the
        # next start_server transition without a separate hook.
        self.after(1000, self._tick_autorun)
