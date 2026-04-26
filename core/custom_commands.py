"""
core/custom_commands.py — Custom chat-command engine.

Players type a trigger word/phrase in chat; VSSM detects it,
checks the speaker's role and per-rule cooldown, captures any positional
arguments, and fires one or more console commands.

Design:
  - Rules are stored as plain dicts in settings (no DB needed).
  - Matching is done by the ChatCommandDispatcher in the output-processing
    pipeline — it receives (player_name, player_role, message) and returns
    a list of console commands to execute, or [].
  - Commands support these substitutions, expanded at dispatch time:
        {player}    → speaker's name
        {role}      → speaker's role
        {1} {2} …   → positional arguments after the trigger word
        {target}    → alias for {1}
        {args}      → all arguments joined by a single space
  - An empty 'roles' list means "any role can trigger this".
  - Rules can be disabled without deleting them.
  - Per-rule cooldown (cooldown_secs) is tracked per (rule, player) so
    one player spamming !warp doesn't lock everyone else out.
  - The dispatcher emits an audit record for every fire, surfaced via a
    callback so the UI can render it in real time.

Rule schema:
    {
      "trigger":               "!warp",
      "response":              "/tp {player} 0 150 0",
      "roles":                 ["admin"],   # [] means anyone
      "enabled":               True,
      "cooldown_secs":         0,           # 0 means no cooldown
      "confirmed_destructive": False,       # opt-in for /stop /ban /op …
    }
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


# Console commands that grant elevated power or affect the whole server.
# A rule whose response contains any of these requires explicit opt-in
# via the `confirmed_destructive: True` field; otherwise validate_rule
# flags it. This guards against typos in the role list accidentally
# letting a guest run /stop.
DESTRUCTIVE_KEYWORDS: tuple[str, ...] = (
    "/stop", "/shutdown", "/ban", "/unban", "/op", "/deop",
    "/kick", "/whitelist", "/blacklist", "/role ", "/grant",
    "/revoke", "/genworld", "/regenchunk", "/setspawn",
)


@dataclass
class AuditRecord:
    """A single dispatch event — emitted via the dispatcher's listener."""
    timestamp: float
    player: str
    role: str
    trigger: str
    message: str
    commands: list[str] = field(default_factory=list)
    skipped_reason: Optional[str] = None  # 'cooldown', 'role', 'disabled', None
    cooldown_remaining: float = 0.0


class ChatCommandDispatcher:
    """Evaluates incoming chat messages against the loaded rule set."""

    def __init__(
        self,
        get_rules: Callable[[], list[dict]],
        audit_listener: Optional[Callable[[AuditRecord], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """
        get_rules:       callable returning the current list of rule dicts.
                         Called on every message so edits take effect at once.
        audit_listener:  optional callable invoked with an AuditRecord for
                         every match (fired or skipped).
        clock:           injectable monotonic clock for testing.
        """
        self._get_rules = get_rules
        self._audit_listener = audit_listener
        self._clock = clock
        # (rule_trigger, player_name) -> last_fire_time
        self._last_fire: dict[tuple[str, str], float] = {}

    def set_audit_listener(
        self,
        listener: Optional[Callable[[AuditRecord], None]],
    ) -> None:
        self._audit_listener = listener

    def reset_cooldowns(self) -> None:
        self._last_fire.clear()

    def dispatch(
        self,
        player_name: str,
        player_role: str,
        message: str,
    ) -> list[str]:
        """Return a (possibly empty) list of console-command strings to send.

        player_role should be lowercase (e.g. 'suplayer', 'admin').
        """
        results: list[str] = []
        msg = message.strip()
        now = self._clock()

        for rule in self._get_rules():
            trigger = (rule.get("trigger") or "").strip()
            if not trigger:
                continue
            args = _extract_args(msg, trigger)
            if args is None:
                continue  # Trigger didn't match this message at all.

            # From here on the message DID match this rule's trigger,
            # so any rejection produces an audit record.
            audit = AuditRecord(
                timestamp=time.time(),
                player=player_name,
                role=player_role,
                trigger=trigger,
                message=msg,
            )

            if not rule.get("enabled", True):
                audit.skipped_reason = "disabled"
                self._emit(audit)
                continue

            allowed_roles = rule.get("roles") or []
            if allowed_roles:
                if player_role.lower() not in [r.lower() for r in allowed_roles]:
                    audit.skipped_reason = "role"
                    self._emit(audit)
                    continue

            cooldown = float(rule.get("cooldown_secs") or 0)
            if cooldown > 0:
                key = (trigger.lower(), player_name.lower())
                last = self._last_fire.get(key, 0.0)
                remaining = cooldown - (now - last)
                if remaining > 0:
                    audit.skipped_reason = "cooldown"
                    audit.cooldown_remaining = remaining
                    self._emit(audit)
                    continue

            response = rule.get("response") or ""
            if not response:
                continue
            if (_contains_destructive(response)
                    and not rule.get("confirmed_destructive")):
                audit.skipped_reason = "unconfirmed_destructive"
                self._emit(audit)
                continue

            commands = _expand_response(response, player_name,
                                        player_role, args)
            if not commands:
                continue

            if cooldown > 0:
                key = (trigger.lower(), player_name.lower())
                self._last_fire[key] = now

            audit.commands = list(commands)
            self._emit(audit)
            results.extend(commands)

        return results

    def _emit(self, audit: AuditRecord) -> None:
        if self._audit_listener is None:
            return
        try:
            self._audit_listener(audit)
        except Exception:
            # Never let a bad listener break dispatch.
            pass


# -----------------------------------------------------------------------
# Trigger matching + argument extraction
# -----------------------------------------------------------------------
def _extract_args(message: str, trigger: str) -> Optional[list[str]]:
    """If `trigger` matches the start of `message` at a word boundary,
    return the list of remaining whitespace-separated tokens.
    Otherwise return None.

        !warp spawn 1 2  with trigger '!warp'  →  ['spawn', '1', '2']
        !warp            with trigger '!warp'  →  []
        !warpzone        with trigger '!warp'  →  None
        hello !warp      with trigger '!warp'  →  None  (must be at start)
    """
    t = re.escape(trigger.strip())
    m = re.match(r"(?i)" + t + r"(\s+(.*))?$", message)
    if not m:
        return None
    rest = (m.group(2) or "").strip()
    return rest.split() if rest else []


def _expand_response(
    response: str,
    player: str,
    role: str,
    args: list[str],
) -> list[str]:
    """Expand placeholders in each line of the response. Lines that
    reference args that weren't supplied are dropped (so a typo like
    `!give` with no args doesn't leak `{2}` into the console)."""
    out: list[str] = []
    args_str = " ".join(args)
    for raw in response.splitlines():
        line = raw.strip()
        if not line:
            continue

        def _repl_pos(match: re.Match) -> str:
            tok = match.group(1)
            if tok == "target":
                idx = 0
            else:
                idx = int(tok) - 1
            if 0 <= idx < len(args):
                return args[idx]
            return "\x00MISSING\x00"

        expanded = re.sub(r"\{(target|[1-9])\}", _repl_pos, line)
        if "\x00MISSING\x00" in expanded:
            # Required positional arg wasn't supplied; skip this line.
            continue

        expanded = expanded.replace("{player}", player)
        expanded = expanded.replace("{role}",   role)
        expanded = expanded.replace("{args}",   args_str)
        out.append(expanded)
    return out


def _contains_destructive(response: str) -> bool:
    low = response.lower()
    return any(k in low for k in DESTRUCTIVE_KEYWORDS)


# Backwards-compatible name used by unit tests / older code.
def _matches_trigger(message: str, trigger: str) -> bool:
    return _extract_args(message, trigger) is not None


# -----------------------------------------------------------------------
# Rule validation helpers (used by the UI editor)
# -----------------------------------------------------------------------
def validate_rule(rule: dict) -> list[str]:
    """Return a list of human-readable error strings, or [] if valid."""
    errors: list[str] = []
    trigger = (rule.get("trigger") or "").strip()
    if not trigger:
        errors.append("Trigger cannot be empty.")
    if " " in trigger:
        errors.append("Trigger cannot contain spaces (use one word).")

    response = (rule.get("response") or "").strip()
    if not response:
        errors.append("Response command(s) cannot be empty.")

    roles = rule.get("roles")
    if roles is not None and not isinstance(roles, list):
        errors.append("'roles' must be a list.")

    cd = rule.get("cooldown_secs", 0)
    try:
        cd_val = float(cd)
        if cd_val < 0:
            errors.append("Cooldown must be ≥ 0.")
    except (TypeError, ValueError):
        errors.append("Cooldown must be a number.")

    if (response and _contains_destructive(response)
            and not rule.get("confirmed_destructive")):
        errors.append(
            "Response contains a destructive command "
            "(/stop, /ban, /op, etc.) — tick the 'I understand "
            "this is destructive' box to confirm.")

    return errors


def make_empty_rule() -> dict:
    return {
        "trigger":               "",
        "response":              "",
        "roles":                 [],
        "enabled":               True,
        "cooldown_secs":         0,
        "confirmed_destructive": False,
    }


def normalize_rule(rule: dict) -> dict:
    """Fill in any fields missing from older rule schemas. Returns the
    same dict (mutated) for chaining."""
    rule.setdefault("trigger", "")
    rule.setdefault("response", "")
    rule.setdefault("roles", [])
    rule.setdefault("enabled", True)
    rule.setdefault("cooldown_secs", 0)
    rule.setdefault("confirmed_destructive", False)
    return rule


# -----------------------------------------------------------------------
# Import / export — rule-only payload, NEVER touches settings.
# -----------------------------------------------------------------------
# These helpers live here (next to the rule engine) rather than in
# core/settings.py so it's structurally impossible for them to reach
# into the settings dict and accidentally export crash thresholds, theme
# preferences, profile paths, or anything else that isn't a rule.
#
# The on-disk wrapper format:
#     {
#       "vssm_custom_commands_version": 1,
#       "rules": [ {trigger, response, roles, enabled,
#                   cooldown_secs, confirmed_destructive}, ... ]
#     }
#
# A bare top-level list of rule dicts is also accepted on import for
# convenience and backwards-compat with v3.1's earlier shape.

EXPORT_WRAPPER_KEY = "vssm_custom_commands_version"
EXPORT_WRAPPER_VERSION = 1

# v3.1 shipped this older key name; recognise it on import so files
# already on disk continue to load.
_LEGACY_WRAPPER_KEY = "vserverman_rules_version"


def export_rules_to_json(rules: list[dict]) -> str:
    """Serialise the given rule list to a pretty JSON string.

    Only the rule list is written — never any settings, paths, or
    profile data — so a shared file can't leak the exporter's local
    config.
    """
    # Defensive copy + normalise so an exported file always has every
    # field a fresh installation expects, regardless of when the rule
    # was originally created.
    clean: list[dict] = []
    for r in rules or []:
        if not isinstance(r, dict):
            continue
        copy = {
            "trigger":               r.get("trigger") or "",
            "response":              r.get("response") or "",
            "roles":                 list(r.get("roles") or []),
            "enabled":               bool(r.get("enabled", True)),
            "cooldown_secs":         r.get("cooldown_secs", 0),
            "confirmed_destructive": bool(r.get("confirmed_destructive")),
        }
        clean.append(copy)
    return json.dumps(
        {EXPORT_WRAPPER_KEY: EXPORT_WRAPPER_VERSION, "rules": clean},
        indent=2,
    )


def import_rules_from_json(payload: str) -> list[dict]:
    """Parse a JSON document produced by export_rules_to_json (or a bare
    list of rule dicts) and return a normalised, validated rule list.

    Raises ValueError on malformed input or on any rule that fails
    validate_rule().
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise ValueError(f"Not valid JSON: {e}") from e

    # Accept three shapes:
    #   1. a bare list of rule dicts
    #   2. a wrapped object {"vssm_custom_commands_version": N, "rules": [...]}
    #   3. legacy wrapper {"vserverman_rules_version": 1, "rules": [...]}
    if isinstance(data, list):
        raw_rules = data
    elif isinstance(data, dict) and isinstance(data.get("rules"), list):
        # Reject obvious mis-imports: if the file looks like a settings
        # dump (has any of these keys), bail early with a clear error
        # instead of silently picking out the rules.
        settings_only_keys = {
            "active_profile", "profiles", "ui_scale_override",
            "theme_preset", "crash_limit", "crash_window_secs",
            "log_level", "player_count_poll_secs", "_schema_version",
        }
        leaked = sorted(set(data.keys()) & settings_only_keys)
        if leaked:
            raise ValueError(
                "This file looks like a full settings export, not a "
                "custom-commands export — refusing to import. "
                f"Unexpected keys: {', '.join(leaked)}.")
        raw_rules = data["rules"]
    else:
        raise ValueError(
            "Expected a JSON list of rules, or an object with a 'rules' key.")

    rules: list[dict] = []
    for i, r in enumerate(raw_rules):
        if not isinstance(r, dict):
            raise ValueError(f"Rule {i} is not an object.")
        normalize_rule(r)
        errs = validate_rule(r)
        if errs:
            raise ValueError(
                f"Rule {i} ({r.get('trigger') or '?'}): {'; '.join(errs)}")
        rules.append(r)
    return rules
