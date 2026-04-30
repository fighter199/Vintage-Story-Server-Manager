"""
core/autorun.py — Autorun command scheduler.

Each Autorun rule fires one or more console commands at a fixed interval
while the server is running. The scheduler is decoupled from the UI:
- Rules are plain dicts (so they round-trip through settings JSON).
- Time and side-effects are injected: the scheduler takes a `clock` and
  a `send` callback, making it trivially unit-testable without Tk or a
  real server process.

Rule schema (kept narrow on purpose):

    {
        "name":             "Hourly save",      # human-readable label
        "enabled":          True,
        "interval_secs":    3600,               # > 0
        "commands":         "/autosavenow",     # multi-line allowed
        "run_on_start":     True,               # fire once when server starts
        "pause_when_empty": False,              # skip ticks if 0 players
    }

The scheduler is reset at server start and torn down at server stop, so
intervals are measured "from server start" and don't drift across
restarts. Pausing is observed *at tick time* — a paused tick still
schedules the next one, it just doesn't send anything.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

# Field-name allowlist for rule normalization. Anything not in this set
# is dropped so a malformed import can't smuggle in arbitrary keys that
# would later round-trip through settings.json.
_ALLOWED_KEYS = frozenset({
    "name", "enabled", "interval_secs", "commands",
    "run_on_start", "pause_when_empty",
})


# ----------------------------------------------------------------------
# Rule helpers
# ----------------------------------------------------------------------
def make_empty_rule() -> dict:
    """Return a fresh rule with sensible defaults."""
    return {
        "name":             "",
        "enabled":          True,
        "interval_secs":    300,        # 5 min
        "commands":         "",
        "run_on_start":     False,
        "pause_when_empty": False,
    }


def normalize_rule(rule: dict) -> dict:
    """Coerce a rule dict in-place into the canonical schema, dropping
    unknown keys and clamping numeric ranges. Mutates and returns rule.
    """
    if not isinstance(rule, dict):
        raise TypeError("rule must be a dict")

    # Drop unknown keys
    for k in list(rule.keys()):
        if k not in _ALLOWED_KEYS:
            del rule[k]

    rule.setdefault("name",             "")
    rule.setdefault("enabled",          True)
    rule.setdefault("interval_secs",    300)
    rule.setdefault("commands",         "")
    rule.setdefault("run_on_start",     False)
    rule.setdefault("pause_when_empty", False)

    # Type/range coercion
    try:
        rule["interval_secs"] = max(1, int(rule["interval_secs"]))
    except (TypeError, ValueError):
        rule["interval_secs"] = 300

    rule["enabled"]          = bool(rule["enabled"])
    rule["run_on_start"]     = bool(rule["run_on_start"])
    rule["pause_when_empty"] = bool(rule["pause_when_empty"])
    rule["name"]             = str(rule.get("name") or "").strip()
    rule["commands"]         = str(rule.get("commands") or "")
    return rule


def validate_rule(rule: dict) -> tuple[bool, str]:
    """Return (ok, reason). reason is empty when ok is True."""
    if not isinstance(rule, dict):
        return False, "rule is not a dict"
    name = (rule.get("name") or "").strip()
    if not name:
        return False, "name is required"
    try:
        interval = int(rule.get("interval_secs", 0))
    except (TypeError, ValueError):
        return False, "interval_secs must be a number"
    if interval < 1:
        return False, "interval_secs must be >= 1"
    cmds = expand_commands(rule.get("commands") or "")
    if not cmds:
        return False, "at least one command is required"
    return True, ""


def expand_commands(text: str) -> list[str]:
    """Split a multi-line command block into individual commands.
    Empty lines and lines starting with `#` are ignored (comments)."""
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


# ----------------------------------------------------------------------
# Audit record (parallel to AuditRecord in custom_commands)
# ----------------------------------------------------------------------
@dataclass
class AutorunAudit:
    """One scheduler decision — fired or skipped, with the reason."""
    timestamp:  float
    rule_name:  str
    fired:      bool
    commands:   list[str] = field(default_factory=list)
    skipped_reason: str = ""   # "disabled" | "paused_empty" | ""


# ----------------------------------------------------------------------
# Scheduler
# ----------------------------------------------------------------------
class AutorunScheduler:
    """Pure-logic scheduler. Has no Tk dependency.

    The host calls `start(now)` when the server comes up, `tick(now)`
    periodically (in practice once per second), and `stop()` when the
    server goes down.

    Each rule has its own internal `next_fire` timestamp. tick() walks
    every rule and fires those whose deadline has passed.

    Parameters
    ----------
    rules_provider : callable returning list[dict]
        Called every tick so live edits in the UI are picked up
        immediately without reattaching anything.
    send : callable(str) -> None
        Receives one expanded command per call (multi-line rules
        produce multiple calls in a single tick).
    player_count : callable() -> int
        Used by the pause_when_empty gate.
    audit : optional callable(AutorunAudit) -> None
        Fired for every tick decision (useful for the UI panel).
    clock : optional callable() -> float
        Defaults to time.time. Override for tests.
    """

    def __init__(
        self,
        rules_provider: Callable[[], Iterable[dict]],
        send:           Callable[[str], None],
        player_count:   Callable[[], int],
        audit:          Callable[[AutorunAudit], None] | None = None,
        clock:          Callable[[], float] | None = None,
    ) -> None:
        self._rules_provider = rules_provider
        self._send           = send
        self._player_count   = player_count
        self._audit          = audit
        self._clock          = clock or time.time
        # rule_id -> next_fire epoch seconds
        self._next_fire: dict[str, float] = {}
        self._running = False
        self._started_at: float = 0.0

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    def start(self, now: float | None = None) -> None:
        """Reset state and arm all rule deadlines.
        Fires `run_on_start` rules immediately.
        """
        if now is None:
            now = self._clock()
        self._running = True
        self._started_at = now
        self._next_fire.clear()
        for rule in self._snapshot():
            rid = self._rule_id(rule)
            if rule.get("run_on_start"):
                # Fire NOW + schedule the regular interval after.
                self._fire_if_allowed(rule, now)
                self._next_fire[rid] = now + max(1, int(
                    rule.get("interval_secs", 300)))
            else:
                self._next_fire[rid] = now + max(1, int(
                    rule.get("interval_secs", 300)))

    def stop(self) -> None:
        """Disarm all rules. Safe to call even if already stopped."""
        self._running = False
        self._next_fire.clear()

    def tick(self, now: float | None = None) -> int:
        """Process one scheduler tick. Returns number of commands sent.
        Safe to call when stopped (no-op)."""
        if not self._running:
            return 0
        if now is None:
            now = self._clock()
        sent = 0
        seen_ids: set[str] = set()
        for rule in self._snapshot():
            rid = self._rule_id(rule)
            seen_ids.add(rid)
            # New rule appeared mid-session — arm it
            if rid not in self._next_fire:
                self._next_fire[rid] = now + max(1, int(
                    rule.get("interval_secs", 300)))
                continue
            if now < self._next_fire[rid]:
                continue
            # Time's up. Fire (or skip with reason) and reschedule
            # regardless of fire/skip — pause is observed at tick time,
            # not by holding the schedule still.
            sent += self._fire_if_allowed(rule, now)
            self._next_fire[rid] = now + max(1, int(
                rule.get("interval_secs", 300)))
        # Drop deadlines for rules that vanished from the provider.
        for rid in list(self._next_fire):
            if rid not in seen_ids:
                del self._next_fire[rid]
        return sent

    # ------------------------------------------------------------------
    def _snapshot(self) -> list[dict]:
        """Pull the current rule list from the provider, normalize each
        copy so the scheduler never trips on a malformed entry."""
        out: list[dict] = []
        try:
            for r in (self._rules_provider() or []):
                if isinstance(r, dict):
                    out.append(normalize_rule(dict(r)))
        except Exception:
            return []
        return out

    @staticmethod
    def _rule_id(rule: dict) -> str:
        """Stable identity string. We use name as the key — names are
        required by validate_rule, and a duplicate name means duplicate
        deadline tracking, which is what the user would expect."""
        return (rule.get("name") or "").strip().lower()

    def _fire_if_allowed(self, rule: dict, now: float) -> int:
        """Apply gates and (maybe) send commands. Returns the number
        of console commands actually written."""
        name = rule.get("name") or "(unnamed)"
        if not rule.get("enabled", True):
            self._emit(AutorunAudit(now, name, False,
                                     skipped_reason="disabled"))
            return 0
        if rule.get("pause_when_empty"):
            try:
                if int(self._player_count()) <= 0:
                    self._emit(AutorunAudit(now, name, False,
                                             skipped_reason="paused_empty"))
                    return 0
            except Exception:
                pass
        cmds = expand_commands(rule.get("commands") or "")
        if not cmds:
            return 0
        for c in cmds:
            try:
                self._send(c)
            except Exception:
                # Don't let one broken send tank the rest.
                pass
        self._emit(AutorunAudit(now, name, True, commands=list(cmds)))
        return len(cmds)

    def _emit(self, audit: AutorunAudit) -> None:
        if self._audit is None:
            return
        try:
            self._audit(audit)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Inspection helpers (for UI)
    # ------------------------------------------------------------------
    def seconds_to_next(self, rule_name: str, now: float | None = None
                        ) -> float | None:
        """Return seconds until the named rule's next fire, or None
        if the rule isn't armed (scheduler stopped, or unknown name)."""
        if not self._running:
            return None
        if now is None:
            now = self._clock()
        rid = (rule_name or "").strip().lower()
        deadline = self._next_fire.get(rid)
        if deadline is None:
            return None
        return max(0.0, deadline - now)
