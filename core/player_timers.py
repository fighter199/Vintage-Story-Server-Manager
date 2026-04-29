"""
core/player_timers.py — Per-player session + lifetime playtime tracking.

Two timers per player:
    session  — wall-clock seconds since this player's most recent join.
               Resets to 0 each time they leave and rejoin.
    total    — cumulative seconds across every session ever recorded.
               Persisted to settings so it survives VSSM restarts.

Design notes:
    - The engine has zero Tk dependency. Time is injected via a `clock`
      callable so unit tests can run deterministically.
    - Persisted totals live as a flat dict {player_name: int_seconds}
      inside the active profile. Names are case-preserved exactly as
      the server reports them — VS player names are case-sensitive.
    - Active session start times are kept only in-memory (a player who
      is "online" when VSSM crashes will be treated as not-yet-online
      on the next launch, which is correct: we have no way to know
      whether they're actually still connected to the running server).
    - `flush(now)` accumulates all active sessions into totals without
      ending them. The host should call this every ~60s so a crash
      loses at most ~60s of playtime, and right before VSSM exits.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, Iterable


class PlayerTimers:
    """Tracks per-player session + lifetime playtime.

    Parameters
    ----------
    totals_provider : callable() -> dict
        Returns the persisted totals dict (mutable). The engine reads
        and mutates this dict directly so persistence is just a matter
        of saving the settings blob.
    clock : optional callable() -> float
        Defaults to time.time. Override for tests.
    """

    def __init__(
        self,
        totals_provider: Callable[[], Dict[str, int]],
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._totals_provider = totals_provider
        self._clock = clock or time.time
        # In-memory: name → epoch seconds when the current session started
        self._session_started: Dict[str, float] = {}
        # In-memory: name → seconds already flushed from this session
        # to totals (so flush() doesn't double-count). Reset on leave.
        self._session_flushed: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle hooks (called by the host on player events)
    # ------------------------------------------------------------------
    def record_join(self, name: str, now: float | None = None) -> None:
        """Start a session for `name`. If a session is already open
        (e.g. because we never saw the leave message), the existing
        session is left untouched — re-joining without a leave is a
        no-op rather than a reset, since the player has been "online"
        the whole time from the engine's point of view."""
        if not name:
            return
        if name in self._session_started:
            return
        if now is None:
            now = self._clock()
        self._session_started[name] = now
        self._session_flushed[name] = 0.0

    def record_leave(self, name: str, now: float | None = None) -> None:
        """End the session for `name`. The unflushed remainder of the
        session is added to the player's total. No-op if `name` had
        no open session."""
        if not name or name not in self._session_started:
            return
        if now is None:
            now = self._clock()
        elapsed = max(0.0, now - self._session_started[name])
        unflushed = elapsed - self._session_flushed.get(name, 0.0)
        if unflushed > 0:
            totals = self._totals_provider()
            # Use `is None`, not falsy: an empty dict is the common case
            # for a fresh profile and must still receive the write.
            if totals is not None:
                totals[name] = int(totals.get(name, 0)) + int(unflushed)
        self._session_started.pop(name, None)
        self._session_flushed.pop(name, None)

    def reset_all(self, now: float | None = None) -> None:
        """End every active session (e.g. when the server stops).
        Same accumulation rules as record_leave."""
        if now is None:
            now = self._clock()
        # Iterate over a copy because record_leave mutates the dict.
        for name in list(self._session_started.keys()):
            self.record_leave(name, now)

    # ------------------------------------------------------------------
    # Read accessors
    # ------------------------------------------------------------------
    def session_secs(self, name: str, now: float | None = None) -> int:
        """Return seconds since this player's current session started.
        Zero if they're not online."""
        if not name or name not in self._session_started:
            return 0
        if now is None:
            now = self._clock()
        return max(0, int(now - self._session_started[name]))

    def total_secs(self, name: str, now: float | None = None) -> int:
        """Return persisted total + current-session-not-yet-flushed.
        Includes time from any active session."""
        totals = self._totals_provider()
        base = int(totals.get(name, 0)) if totals is not None else 0
        if name not in self._session_started:
            return base
        if now is None:
            now = self._clock()
        elapsed = max(0.0, now - self._session_started[name])
        unflushed = elapsed - self._session_flushed.get(name, 0.0)
        return base + max(0, int(unflushed))

    def has_active_session(self, name: str) -> bool:
        return name in self._session_started

    def active_players(self) -> list[str]:
        return list(self._session_started.keys())

    # ------------------------------------------------------------------
    # Periodic flush (host calls this every ~60s + at shutdown)
    # ------------------------------------------------------------------
    def flush(self, now: float | None = None) -> int:
        """Accumulate the unflushed portion of every active session
        into totals, *without* ending the sessions.

        Returns the number of seconds flushed this call (sum across
        all active players).
        """
        if not self._session_started:
            return 0
        if now is None:
            now = self._clock()
        totals = self._totals_provider()
        if totals is None:
            return 0
        flushed_this_call = 0
        for name, started in self._session_started.items():
            elapsed = max(0.0, now - started)
            already = self._session_flushed.get(name, 0.0)
            delta = elapsed - already
            if delta > 0:
                totals[name] = int(totals.get(name, 0)) + int(delta)
                self._session_flushed[name] = already + int(delta)
                flushed_this_call += int(delta)
        return flushed_this_call

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def forget_player(self, name: str) -> None:
        """Drop both the active session AND the persisted total for
        a player (e.g. user clicked "Reset playtime" in the menu)."""
        self._session_started.pop(name, None)
        self._session_flushed.pop(name, None)
        totals = self._totals_provider()
        if totals is not None:
            totals.pop(name, None)


# ----------------------------------------------------------------------
# Display helpers
# ----------------------------------------------------------------------
def fmt_duration(secs: int) -> str:
    """Format a duration as H:MM:SS for short, or D days HH:MM for long.

    Examples:
        45             -> "0:00:45"
        125            -> "0:02:05"
        3725           -> "1:02:05"
        90061          -> "1d 01:01"
        604800         -> "7d 00:00"
    """
    secs = max(0, int(secs))
    if secs < 86400:
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}"
    days, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    return f"{days}d {h:02d}:{m:02d}"
