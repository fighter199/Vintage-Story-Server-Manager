"""
host/discord.py — Discord status posting: server address, player count
and periodic world-map images, sent to a channel webhook.

The scheduler (core.discord_status.DiscordStatusScheduler) decides when
an update is due on the Tk thread; the worker does the HTTP and the map
rendering on its own thread and hands results back through
self._discord_results, which _tick_discord drains on the Tk thread.
"""
from __future__ import annotations

import os
import queue
import threading
import time

# ── Local packages ─────────────────────────────────────────────────────
from core.constants import LOG
from core.discord_status import DiscordStatusScheduler, DiscordStatusWorker
from core.settings import (load_discord_config, save_discord_config,
                           save_settings)
from core.utils import read_server_info


class DiscordMixin:

    def _init_discord(self) -> None:
        """Called from ServerManagerApp.__init__ before the UI is built."""
        self._discord_scheduler = DiscordStatusScheduler()
        self._discord_results: queue.Queue = queue.Queue()
        self._discord_worker = DiscordStatusWorker(
            on_result=lambda r: self._discord_results.put(("result", r)))
        self._discord_tab = None
        self._discord_last_players: list = []
        # after() id while waiting for a map-export command to finish
        self._discord_map_job = None

    # ------------------------------------------------------------------
    # State snapshot
    # ------------------------------------------------------------------
    def _discord_savegame(self) -> str:
        """The live savegame: the most recently written .vcdbs, the same
        default the WORLD MAP tab picks."""
        best, best_mtime = "", -1.0
        for path in self._savegame_db_files():
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = path, mtime
        return best

    def _discord_snapshot(self) -> dict:
        """Current server state for a status post (Tk thread only)."""
        exe = self.server_path_var.get().strip()
        info = (read_server_info(os.path.dirname(exe)) if exe
                else {"name": "", "port": 42420, "max_clients": None})
        return {
            "online":      bool(self.is_running),
            "players":     list(self._players) if self.is_running else [],
            "max_players": info["max_clients"],
            "port":        info["port"],
            "server_name": info["name"],
            "uptime_secs": (time.time() - self.start_time
                            if self.is_running and self.start_time else None),
            "savegame":    self._discord_savegame(),
        }

    # ------------------------------------------------------------------
    # Status posts
    # ------------------------------------------------------------------
    def _discord_send(self, reason: str) -> None:
        cfg = load_discord_config(self._settings)
        if not cfg["webhook_url"]:
            return
        self._discord_worker.submit(cfg, self._discord_snapshot(), reason)
        self._discord_scheduler.mark_posted(cfg)

    def _discord_on_server_start(self) -> None:
        self._discord_last_players = []
        reason = self._discord_scheduler.on_server_start(
            load_discord_config(self._settings))
        if reason:
            self._discord_send(reason)

    def _discord_on_server_stop(self, reason: str) -> None:
        if self._discord_scheduler.on_server_stop(
                load_discord_config(self._settings)):
            self._discord_send(reason)

    def _discord_players_changed(self) -> None:
        """Called from _update_player_count on every list re-render."""
        if self._players != self._discord_last_players:
            self._discord_last_players = list(self._players)
            self._discord_scheduler.on_players_changed(
                load_discord_config(self._settings))

    # ------------------------------------------------------------------
    # Map images
    # ------------------------------------------------------------------
    def _discord_take_map(self, reason: str) -> None:
        """Start a map snapshot. For a mod-exported image, run the mod's
        export command first (if set and the server is up) and wait for
        it; the built-in renderer reads the savegame directly."""
        if self._discord_map_job is not None:
            return                          # one already in progress
        cfg = load_discord_config(self._settings)
        self._discord_scheduler.mark_map_posted(cfg)
        cmd = cfg["map_command"] if cfg["map_source"] == "file" else ""
        if cmd and self.is_running and self._send_internal_command(cmd):
            self.append_console(f"[discord] → {cmd}", "system")
            wait_ms = cfg["map_command_wait_secs"] * 1000
            self._discord_map_job = self.after(
                wait_ms, lambda: self._discord_submit_map(reason))
        else:
            self._discord_submit_map(reason)

    def _discord_submit_map(self, reason: str) -> None:
        self._discord_map_job = None
        cfg = load_discord_config(self._settings)
        if cfg["webhook_url"]:
            self._discord_worker.submit(cfg, self._discord_snapshot(),
                                        reason, kind="map")

    # ------------------------------------------------------------------
    # DISCORD tab hooks
    # ------------------------------------------------------------------
    def _discord_config_changed(self) -> None:
        self._discord_scheduler.reconfigure(load_discord_config(self._settings))

    def _discord_post_now(self) -> None:
        """Post Now: sends even when the feature is disabled or the
        server is offline, so the webhook can be tested."""
        self._discord_send("manual")

    def _discord_post_map_now(self) -> None:
        self._discord_take_map("manual")

    def _discord_detect_ip(self) -> None:
        def _work():
            ip = self._discord_worker.public_ip(refresh=True)
            self._discord_results.put(("ip", ip))
        threading.Thread(target=_work, name="discord-ip", daemon=True).start()

    # ------------------------------------------------------------------
    # Results + tick
    # ------------------------------------------------------------------
    def _discord_handle_result(self, result) -> None:
        if result.ok and result.message_id:
            cfg = load_discord_config(self._settings)
            key = "map_message_id" if result.kind == "map" else "message_id"
            # Only adopt the id if it belongs to the webhook still
            # configured — the user may have changed it mid-flight.
            if (cfg["webhook_url"] == result.webhook_url
                    and cfg[key] != result.message_id):
                cfg[key] = result.message_id
                save_discord_config(self._settings, cfg)
                save_settings(self._settings)
        if result.ok:
            LOG.info("discord %s (%s): %s", result.kind, result.reason,
                     result.message)
        else:
            LOG.warning("discord %s (%s) failed: %s", result.kind,
                        result.reason, result.message)
            if result.reason == "manual":
                self._notify(f"Discord: {result.message}", level="error",
                             duration_ms=5000)
        if self._discord_tab is not None:
            self._discord_tab.show_result(result)

    def _tick_discord(self) -> None:
        """1Hz: drain worker results, then fire any due update."""
        try:
            while True:
                kind, payload = self._discord_results.get_nowait()
                if kind == "result":
                    self._discord_handle_result(payload)
                elif kind == "ip" and self._discord_tab is not None:
                    self._discord_tab.show_detected_ip(payload)
        except queue.Empty:
            pass
        except Exception:
            LOG.exception("discord result handling failed")
        try:
            cfg = load_discord_config(self._settings)
            reason = self._discord_scheduler.tick(cfg)
            if reason:
                self._discord_send(reason)
            if self._discord_scheduler.map_due(cfg):
                self._discord_take_map("map")
        except Exception:
            LOG.exception("discord tick failed")
        self.after(1000, self._tick_discord)

    def _discord_flush_on_exit(self) -> None:
        """Let an in-flight post (typically the "offline" update from
        the stop that preceded exit) finish before the process ends."""
        try:
            self._discord_worker.wait_idle(5.0)
            self._discord_worker.shutdown()
        except Exception:
            pass
