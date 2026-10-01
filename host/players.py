"""
host/players.py — Players: the player list and resource panel, playtime tracking,
join/leave and /list parsing, the player-count poll and the
player-aware restart/shutdown guards.

Methods of ServerManagerApp (VSSM.py), moved here unchanged.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import Callable

# ── Local packages ─────────────────────────────────────────────────────
from core.constants import (LOG)
from core.parsers import (parse_player_event, split_client_list)
from core.settings import (save_settings)
from core.utils import (fmt_size)
from ui.theme import (Theme)
from ui.widgets import (Sparkline, ScrollableFrame, panel_header)
from core.player_timers import fmt_duration

# Optional psutil
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


class PlayersMixin:

    # ------------------------------------------------------------------
    # Players + resources panel
    # ------------------------------------------------------------------
    def _build_players(self, parent):
        self._players_scroll = ScrollableFrame(parent, bg=Theme.BG_PANEL)
        self._players_scroll.pack(fill=tk.BOTH, expand=True)
        host = self._players_scroll.body

        player_body = tk.Frame(host, bg=Theme.BG_PANEL)
        _, self.player_header_label = panel_header(
            host, "Player List", right_text="0 online",
            font_spec=self.F_HDR, collapsible=True, body=player_body)
        self.player_list_frame = tk.Frame(player_body, bg=Theme.BG_PANEL)
        self.player_list_frame.pack(fill=tk.X, padx=10, pady=(8, 8))
        self._render_empty_players()

        res_body = tk.Frame(host, bg=Theme.BG_PANEL)
        panel_header(host, "Resources", font_spec=self.F_HDR,
                     collapsible=True, body=res_body)
        res = tk.Frame(res_body, bg=Theme.BG_PANEL)
        res.pack(fill=tk.X, padx=10, pady=(8, 10))
        self.cpu_bar, self.cpu_label, self.cpu_fill, self.cpu_spark = \
            self._make_resource_bar(res, "CPU Usage")
        self.mem_bar, self.mem_label, self.mem_fill, self.mem_spark = \
            self._make_resource_bar(res, "Memory")
        if not PSUTIL_AVAILABLE:
            tk.Label(res, text="(psutil not installed — metrics disabled)",
                     fg=Theme.MUTED, bg=Theme.BG_PANEL,
                     font=self.F_SMALL).pack(anchor=tk.W, pady=(4, 0))


    def _make_resource_bar(self, parent, label_text):
        row = tk.Frame(parent, bg=Theme.BG_PANEL)
        row.pack(fill=tk.X, pady=4)
        top = tk.Frame(row, bg=Theme.BG_PANEL)
        top.pack(fill=tk.X)
        tk.Label(top, text=label_text, fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
                 font=self.F_SMALL).pack(side=tk.LEFT)
        value_label = tk.Label(top, text="--", fg=Theme.AMBER_DIM,
                               bg=Theme.BG_PANEL, font=self.F_SMALL)
        value_label.pack(side=tk.RIGHT)
        bar_bg = tk.Frame(row, bg=Theme.BORDER, height=10)
        bar_bg.pack(fill=tk.X, pady=(2, 0))
        bar_inner = tk.Frame(bar_bg, bg=Theme.DIVIDER)
        bar_inner.place(relx=0, rely=0, relwidth=1, relheight=1, x=1, y=1, width=-2, height=-2)
        fill = tk.Frame(bar_inner, bg=Theme.GREEN)
        fill.place(relx=0, rely=0, relwidth=0, relheight=1)
        spark = Sparkline(row, width=180, height=32, capacity=60,
                          color=Theme.AMBER, bg=Theme.BG_INPUT)
        spark.pack(fill=tk.X, pady=(4, 0))
        return bar_inner, value_label, fill, spark


    def _set_resource_bar(self, fill_widget, label_widget, label_text, frac):
        frac = max(0.0, min(1.0, frac))
        color = Theme.GREEN if frac < 0.6 else (Theme.AMBER if frac < 0.85 else Theme.RED)
        fill_widget.configure(bg=color)
        fill_widget.place_configure(relwidth=frac)
        label_widget.configure(text=label_text, fg=color)


    def _render_empty_players(self):
        tk.Label(self.player_list_frame,
                 text="— No players connected —",
                 fg=Theme.MUTED, bg=Theme.BG_PANEL,
                 font=self.F_NORMAL, pady=20).pack()


    def _render_player_row(self, name: str):
        row = tk.Frame(self.player_list_frame, bg=Theme.BG_PANEL)
        row.pack(fill=tk.X, pady=2)
        badge = tk.Label(row, text=name[:2].upper() if name else "??",
                         fg=Theme.AMBER_GLOW, bg=Theme.DIVIDER,
                         font=self.F_SMALL, width=3, height=1,
                         highlightthickness=1, highlightbackground=Theme.AMBER_DIM,
                         cursor="hand2")
        badge.pack(side=tk.LEFT, padx=(0, 8))
        name_lbl = tk.Label(row, text=name, fg=Theme.AMBER_GLOW,
                            bg=Theme.BG_PANEL, font=self.F_NORMAL, cursor="hand2")
        name_lbl.pack(side=tk.LEFT)

        def _copy_name(_e=None, n=name):
            try:
                self.clipboard_clear()
                self.clipboard_append(n)
                self._notify(f"Copied '{n}'.", level="info", duration_ms=1500)
            except Exception:
                pass

        # Show role badge if known
        role = self._player_roles.get(name)
        if role:
            tk.Label(row, text=f"[{role}]",
                     fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
                     font=self.F_SMALL).pack(side=tk.LEFT, padx=(4, 0))

        # Playtime labels — pinned to the RIGHT edge of the row. Total
        # is packed first so it appears outside (further-right) of the
        # session label, matching the visual order in the comments.
        # Both labels are stored on self._player_timer_labels so the
        # 1Hz tick can update them in place without rebuilding the row.
        total_lbl = tk.Label(
            row, text="Σ 0:00:00",
            fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
            font=self.F_SMALL, cursor="hand2")
        total_lbl.pack(side=tk.RIGHT, padx=(6, 0))
        session_lbl = tk.Label(
            row, text="🕐 0:00:00",
            fg=Theme.AMBER, bg=Theme.BG_PANEL,
            font=self.F_SMALL)
        session_lbl.pack(side=tk.RIGHT, padx=(6, 0))
        self._player_timer_labels[name] = (session_lbl, total_lbl)
        # Tooltip-ish: hovering or right-click on total reveals options
        # via the existing player popup, so we only need a one-time
        # update here for the initial values.
        try:
            session_lbl.configure(text=f"🕐 {fmt_duration(self._player_timers.session_secs(name))}")
            total_lbl.configure(text=f"Σ {fmt_duration(self._player_timers.total_secs(name))}")
        except Exception:
            pass

        for w in (badge, name_lbl):
            w.bind("<Button-1>", _copy_name)

        def _popup(event, n=name):
            self._show_player_menu(event, n)
        for w in (row, badge, name_lbl):
            w.bind("<Button-3>", _popup)
            w.bind("<Button-2>", _popup)


    # ------------------------------------------------------------------
    # Player playtime tick + persistence
    # ------------------------------------------------------------------
    def _tick_player_timers(self):
        """1Hz tick: refresh the session/total labels on each row in
        place. We update the LABELS rather than re-rendering the row
        so there's no flicker and no destroy/recreate cost when many
        players are online."""
        try:
            for name, (sess_lbl, total_lbl) in list(
                    self._player_timer_labels.items()):
                if name not in self._players:
                    continue
                try:
                    sess_lbl.configure(
                        text=f"🕐 {fmt_duration(self._player_timers.session_secs(name))}")
                    total_lbl.configure(
                        text=f"Σ {fmt_duration(self._player_timers.total_secs(name))}")
                except tk.TclError:
                    # Row was destroyed mid-tick (e.g. profile switch).
                    self._player_timer_labels.pop(name, None)
        except Exception:
            LOG.exception("player timer tick failed")
        self.after(1000, self._tick_player_timers)


    def _schedule_timer_flush(self):
        """Schedule the next periodic flush (every 60s).
        Flushing accumulates each active session's elapsed time into
        the persisted totals dict so a crash loses at most ~60s."""
        if self._timer_flush_job_id is not None:
            try:
                self.after_cancel(self._timer_flush_job_id)
            except Exception:
                pass
        self._timer_flush_job_id = self.after(
            60_000, self._timer_flush_tick)


    def _timer_flush_tick(self):
        self._timer_flush_job_id = None
        try:
            if self._player_timers.flush() > 0:
                self._persist_player_totals()
        except Exception:
            LOG.exception("player timer flush failed")
        # Always reschedule, even when no players are online — the
        # cost is one no-op call per minute.
        self._schedule_timer_flush()


    def _persist_player_totals(self):
        """Write the in-memory totals dict back to settings on disk.
        The dict itself is owned by settings (mutated in place by the
        timer engine), so all we need to do is save_settings."""
        try:
            save_settings(self._settings)
        except Exception:
            LOG.exception("save player totals failed")


    def _show_player_menu(self, event, player_name: str):
        if not self.is_running:
            return
        m = tk.Menu(self, tearoff=0, bg=Theme.BG_PANEL, fg=Theme.AMBER,
                    activebackground=Theme.BG_SELECT,
                    activeforeground=Theme.AMBER_GLOW,
                    bd=0, font=self.F_SMALL)
        m.add_command(label=f"Copy name: {player_name}",
                      command=lambda n=player_name: self._copy_to_clipboard(n))
        m.add_separator()
        is_op = player_name in self._operators
        if is_op:
            m.add_command(label="Remove operator (de-OP)",
                          command=lambda n=player_name: self._deop_player(n))
        else:
            m.add_command(label="Make operator (OP)",
                          command=lambda n=player_name: self._op_player(n))
        m.add_separator()
        m.add_command(label="Kick...",
                      command=lambda n=player_name: self._prompt_and_kick(n))
        # Improvement #14: ban confirmation dialog
        m.add_command(label="Ban...",
                      command=lambda n=player_name: self._prompt_and_ban(n))
        # Teleports only between online players: the console has no
        # position of its own, so "/tp <name>" alone does nothing useful.
        others = [p for p in self._players if p != player_name]
        m.add_separator()
        if others:
            menu_opts = dict(tearoff=0, bg=Theme.BG_PANEL, fg=Theme.AMBER,
                             activebackground=Theme.BG_SELECT,
                             activeforeground=Theme.AMBER_GLOW, bd=0,
                             font=self.F_SMALL)
            send_to = tk.Menu(m, **menu_opts)
            bring = tk.Menu(m, **menu_opts)
            for other in sorted(others, key=str.lower):
                send_to.add_command(
                    label=other,
                    command=lambda a=player_name, b=other: self._teleport_player(a, b))
                bring.add_command(
                    label=other,
                    command=lambda a=other, b=player_name: self._teleport_player(a, b))
            m.add_cascade(label=f"Teleport {player_name} to", menu=send_to)
            m.add_cascade(label=f"Teleport to {player_name}", menu=bring)
        else:
            m.add_command(label="Teleport (no other players online)",
                          state=tk.DISABLED)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()


    def _op_player(self, name):
        if self._run_admin_cmd(f"/op {name}"):
            self._operators.add(name)


    def _deop_player(self, name):
        if self._run_admin_cmd(f"/player {name} role suplayer"):
            self._operators.discard(name)


    def _run_admin_cmd(self, cmd):
        if self._send_internal_command(cmd):
            self.append_console(f"❯ {cmd}", "echo")
            return True
        self.append_console(f"Could not send: {cmd}", "error")
        return False


    def _copy_to_clipboard(self, text):
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._notify(f"Copied '{text}'.", level="info", duration_ms=1500)
        except Exception:
            pass


    def _prompt_and_kick(self, name):
        from tkinter import simpledialog
        reason = simpledialog.askstring(
            "Kick Player", f"Reason for kicking {name}?", parent=self)
        if reason is None:
            return
        self._send_internal_command(f"/kick {name} {reason.strip()}")
        self.append_console(f"❯ /kick {name} {reason.strip()}", "echo")


    def _prompt_and_ban(self, name):
        """Ban with explicit confirmation dialog (improvement #14)."""
        if not messagebox.askyesno(
                "Confirm Ban",
                f"Are you sure you want to BAN {name}?\nThis cannot be undone from VSSM.",
                icon="warning", parent=self):
            return
        from tkinter import simpledialog
        reason = simpledialog.askstring(
            "Ban Player", f"Reason for banning {name}?", parent=self)
        if reason is None:
            return
        self._send_internal_command(f"/ban {name} {reason.strip()}")
        self.append_console(f"❯ /ban {name} {reason.strip()}", "echo")


    def _teleport_player(self, who: str, destination: str):
        """Move online player `who` to online player `destination`."""
        self._run_admin_cmd(f"/tp {who} {destination}")


    def _prompt_and_run(self, title, prompt, cmd_builder):
        from tkinter import simpledialog
        reason = simpledialog.askstring(title, prompt, parent=self)
        if reason is None:
            return
        cmd = cmd_builder(reason.strip())
        self._send_internal_command(cmd)
        self.append_console(f"❯ {cmd}", "echo")


    # ------------------------------------------------------------------
    # Player tracking
    # ------------------------------------------------------------------
    # Multi-line /list clients accumulator. The parser emits a
    # "list_header" event when it sees "List of online Players",
    # then one "list_entry" per "Playing [N] Name [ip]:port …" row.
    # _list_buffer collects names; _flush_list_buffer commits them
    # to _sync_players_from_list (which adds missing + removes
    # vanished, providing the user-requested fail-safe).
    def _arm_list_buffer_backstop(self):
        """(Re-)arm the 1s timer that flushes the list buffer if no
        more entries arrive. Safe to call even when no buffer is
        active — it will just no-op on flush."""
        if getattr(self, "_list_buffer_job_id", None) is not None:
            try:
                self.after_cancel(self._list_buffer_job_id)
            except Exception:
                pass
        self._list_buffer_job_id = self.after(
            1000, self._flush_list_buffer)


    def _flush_list_buffer(self):
        """Commit the buffered /list clients result to the player
        list. No-op if no buffer is active. Always cancels the
        backstop timer."""
        if getattr(self, "_list_buffer_job_id", None) is not None:
            try:
                self.after_cancel(self._list_buffer_job_id)
            except Exception:
                pass
            self._list_buffer_job_id = None
        if not getattr(self, "_list_buffer_active", False):
            return
        names = list(self._list_buffer)
        self._list_buffer = []
        self._list_buffer_active = False
        # _sync_players_from_list does the actual diff: any name in
        # `names` that isn't currently tracked is added (with role
        # query queued); any currently-tracked name not in `names`
        # is removed. This is the fail-safe.
        self._sync_players_from_list(names)


    def _parse_player_event(self, line: str):
        event, payload = parse_player_event(line)
        if event == "join":
            self._flush_list_buffer()  # any partial list ends here
            self._add_player(payload)
        elif event == "leave":
            self._flush_list_buffer()
            self._remove_player(payload)
        elif event == "list_header":
            # Open a fresh accumulation window. If a previous window
            # somehow stayed open (shouldn't happen — every header is
            # followed by entries + a non-list line), discard it.
            self._list_buffer = []
            self._list_buffer_active = True
            # Backstop: if no further matching line arrives within 1s,
            # flush whatever we have. Cancelled and re-armed on each
            # entry; cancelled and fired on the next non-list line.
            self._arm_list_buffer_backstop()
        elif event == "list_entry":
            if self._list_buffer_active:
                if payload and payload not in self._list_buffer:
                    self._list_buffer.append(payload)
                self._arm_list_buffer_backstop()
            # If a list_entry arrives without a preceding header (e.g.
            # we missed the header due to chunked output), we still
            # treat it as a one-off join hint — the periodic sync will
            # catch up on the next /list clients.
        elif event == "list":
            # Older inline "Connected players: …" format; some VS
            # builds may still emit this. Bidirectional sync, just
            # like the multi-line path.
            self._flush_list_buffer()
            if not payload or payload.lower() in ("none", "no one", "-"):
                self._sync_players_from_list([])
            else:
                names = split_client_list(payload)
                self._sync_players_from_list(names)
        else:
            # Any other line means the multi-line list block (if open)
            # has ended. Flush whatever we have.
            if self._list_buffer_active:
                self._flush_list_buffer()


    def _sync_players_from_list(self, names: list):
        new_set = list(dict.fromkeys(names))
        if new_set == self._players:
            return
        # Diff against the current list to feed implicit join/leave
        # events to the timer engine. Without this, /list clients
        # responses (which is how we detect players who were already
        # connected when VSSM started) wouldn't kick the session
        # timers into life.
        before = set(self._players)
        after  = set(new_set)
        for joined in after - before:
            self._player_timers.record_join(joined)
        for left in before - after:
            self._player_timers.record_leave(left)
        if before != after:
            self._persist_player_totals()
        self._players = new_set
        self._rerender_players()


    def _add_player(self, name: str):
        if name in self._players:
            return
        self._players.append(name)
        self._player_timers.record_join(name)
        self._rerender_players()
        def _fire(n=name):
            if self._send_internal_command(f"/player {n} role"):
                self._pending_role_query.append(n)
        self.after(2000, _fire)


    def _remove_player(self, name: str):
        if name not in self._players:
            return
        self._players.remove(name)
        self._player_timers.record_leave(name)
        # Persist immediately — leaves are exactly the moments where
        # losing data hurts most (the just-completed session is now
        # in the totals dict and a crash before the next 60s flush
        # would erase it).
        self._persist_player_totals()
        self._operators.discard(name)
        self._player_roles.pop(name, None)
        self._rerender_players()


    def _rerender_players(self):
        # Every row widget is about to be destroyed — drop the label
        # refs so entries for players who left don't accumulate forever
        # (_render_player_row re-registers the current players below).
        self._player_timer_labels.clear()
        for child in list(self.player_list_frame.winfo_children()):
            child.destroy()
        if not self._players:
            self._render_empty_players()
        else:
            for name in self._players:
                self._render_player_row(name)
        self._update_player_count()


    def _update_player_count(self):
        n = len(self._players)
        self.player_count_var.set(str(n))
        if hasattr(self, "player_header_label") and self.player_header_label:
            self.player_header_label.configure(text=f"{n} online")


    # ------------------------------------------------------------------
    # Player-aware restart/shutdown guards (NEW)
    # ------------------------------------------------------------------
    def _prompt_players_online_action(
        self,
        action_label: str,
        on_continue_now: Callable[[], None],
        on_wait_until_empty: Callable[[], None],
    ) -> None:
        """Show a 3-button dialog when the user triggers a manual
        restart/shutdown while players are online.

        Buttons:
          Yes  → wait until server is empty, then perform the action
          No   → perform the action immediately
          Cancel → do nothing
        """
        n = len(self._players)
        # Build a short list of names for the prompt — cap at 6 so the
        # dialog stays readable on a 100-player server.
        names_preview = ", ".join(self._players[:6])
        if n > 6:
            names_preview += f", … (+{n - 6} more)"
        title = f"Players online — {action_label}"
        body = (
            f"{n} player{'s' if n != 1 else ''} currently online:\n"
            f"  {names_preview}\n\n"
            f"Yes  →  Wait until the server is empty, then {action_label}\n"
            f"No   →  {action_label.capitalize()} now anyway\n"
            f"Cancel →  Don't {action_label}"
        )
        # askyesnocancel returns True / False / None
        choice = messagebox.askyesnocancel(title, body, parent=self)
        if choice is None:
            self.append_console(
                f"{action_label.capitalize()} cancelled by user.", "system")
            return
        if choice:
            on_wait_until_empty()
        else:
            on_continue_now()


    def _wait_for_empty_then(
        self,
        action: Callable[[], None],
        action_label: str = "action",
        poll_interval_ms: int = 5000,
    ) -> None:
        """Poll _players every `poll_interval_ms` ms; when empty, run
        `action`. Cancels any previous deferred wait first so we never
        end up with two of these running at once."""
        self._cancel_deferred_restart_wait()
        self._deferred_restart_active = True
        self.append_console(
            f"Waiting for empty server before {action_label} "
            f"({len(self._players)} online)…",
            "system")
        self._notify(
            f"{action_label.capitalize()} pending — waiting for empty server.",
            level="info", duration_ms=4000)

        def _tick():
            self._deferred_restart_poll_id = None
            if not self._deferred_restart_active:
                return  # cancelled
            if not self.is_running:
                # Server died on us — abort the deferred action.
                self.append_console(
                    f"Deferred {action_label} aborted: server stopped.",
                    "warn")
                self._deferred_restart_active = False
                return
            if not self._players:
                self._deferred_restart_active = False
                self.append_console(
                    f"Server empty — proceeding with {action_label}.",
                    "system")
                try:
                    action()
                except Exception:
                    LOG.exception("deferred %s failed", action_label)
                return
            # Still players online — nudge the player list and poll again.
            try:
                self._send_internal_command("/list clients")
            except Exception:
                pass
            self._deferred_restart_poll_id = self.after(
                poll_interval_ms, _tick)

        # First tick scheduled immediately so we react to a server
        # that's already empty by the time the user clicked Yes.
        self._deferred_restart_poll_id = self.after(0, _tick)


    def _cancel_deferred_restart_wait(self) -> None:
        """Stop polling for an empty server, if a deferred action is
        currently waiting. Safe to call any time."""
        self._deferred_restart_active = False
        if self._deferred_restart_poll_id is not None:
            try:
                self.after_cancel(self._deferred_restart_poll_id)
            except Exception:
                pass
            self._deferred_restart_poll_id = None


    # ------------------------------------------------------------------
    # Periodic player-count poller — sends `/list clients` at a
    # user-configurable interval while the server is running. The
    # response feeds back through _parse_player_event → 'list' branch.
    # ------------------------------------------------------------------
    def _player_poll_interval_secs(self) -> int:
        """Read the configured interval, with 0 meaning 'disabled'."""
        try:
            return max(0, int(
                self._settings.get("player_count_poll_secs", 30)))
        except (ValueError, TypeError):
            return 30


    def _reschedule_player_count_poll(self) -> None:
        """Cancel any pending poll and schedule the next one based on
        the current setting. Safe to call from anywhere (settings save,
        server start/stop, etc.)."""
        jid = getattr(self, "_player_poll_job_id", None)
        if jid is not None:
            try:
                self.after_cancel(jid)
            except Exception:
                pass
            self._player_poll_job_id = None
        secs = self._player_poll_interval_secs()
        if secs <= 0:
            return  # Polling disabled.
        self._player_poll_job_id = self.after(
            secs * 1000, self._player_count_poll_tick)


    def _player_count_poll_tick(self) -> None:
        """Fire one /list clients ping if the server's running, then
        reschedule. Silently no-ops when the server isn't running so
        we don't spam errors into the console."""
        self._player_poll_job_id = None
        try:
            if self.is_running and self.server_process is not None:
                # _send_internal_command already only writes to the
                # server's stdin; no console echo to suppress here.
                self._send_internal_command("/list clients")
        except Exception:
            LOG.exception("player-count poll tick failed")
        # Always reschedule (even if we didn't send) so changes to
        # is_running pick up on the next tick.
        secs = self._player_poll_interval_secs()
        if secs > 0:
            self._player_poll_job_id = self.after(
                secs * 1000, self._player_count_poll_tick)


    def _update_resources(self):
        try:
            proc = self.server_process
            if proc is None or proc.poll() is not None:
                return
            # psutil.Process.cpu_percent(interval=None) reports CPU used
            # since the previous call on the SAME instance — a freshly
            # constructed instance has no baseline and returns 0.0. So
            # keep one instance per server run and prime it on the first
            # tick (real readings start on the second).
            ps = self._psutil_proc
            if ps is None or ps.pid != proc.pid:
                ps = psutil.Process(proc.pid)
                self._psutil_proc = ps
                ps.cpu_percent(interval=None)
                return
            # cpu_percent sums across cores (can exceed 100); normalise
            # by core count so the bar matches whole-machine usage.
            cpu_frac = (ps.cpu_percent(interval=None) / 100.0
                        / (psutil.cpu_count() or 1))
            mem_info = ps.memory_info()
            total_mem = psutil.virtual_memory().total
            mem_frac = mem_info.rss / max(1, total_mem)
            self._set_resource_bar(self.cpu_fill, self.cpu_label,
                                   f"{cpu_frac * 100:.0f}%", cpu_frac)
            self._set_resource_bar(self.mem_fill, self.mem_label,
                                   f"{fmt_size(mem_info.rss)}", mem_frac)
            self.cpu_spark.push(cpu_frac)
            self.mem_spark.push(mem_frac)
        except Exception:
            pass
