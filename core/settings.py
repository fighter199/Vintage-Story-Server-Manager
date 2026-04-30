"""
core/settings.py — Persistent settings: load, save, migrate, profiles.

Improvements over v2:
  - Atomic write (tmp → rename) so a crash mid-save can't corrupt the file
  - Schema v3 adds 'custom_commands' at top level
  - Schema v4 promotes custom_commands to per-profile (each profile gets
    its own rule list; the global list becomes the default for new profiles)
  - Pre-migration backup file is written before any schema bump
  - Configurable crash-loop threshold and log level
  - Custom-command import / export helpers (round-trip a JSON array of rules)
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from typing import Any, Iterable

from .constants import LOG, script_dir, SETTINGS_SCHEMA_VERSION


def settings_path() -> str:
    return os.path.join(script_dir(), "vserverman_settings.json")

def chat_log_path(profile: str | None = None) -> str:
    """Return the path to the per-profile chat-history file.

    Lives next to settings.json so users with VSSM rooted on a
    portable drive get the chat archive moving with them. Filename
    is chat_log_<profile>.json so multiple profiles co-exist.
    """
    base = os.path.dirname(settings_path())
    name = (profile or "default").strip() or "default"
    # Sanitise: only allow filename-safe chars in the profile slug.
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)
    return os.path.join(base, f"chat_log_{safe}.json")



# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def load_settings() -> dict:
    """Load settings, migrating older schemas. Always returns a valid dict."""
    path = settings_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("settings root is not an object")
        return _migrate(data, path)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError) as e:
        if os.path.exists(path):
            LOG.warning("settings load failed (%s) — using defaults", e)
        return _default_settings()


def save_settings(data: dict) -> bool:
    """Atomically write settings to disk.  Writes to .tmp, then renames."""
    path = settings_path()
    tmp = path + ".tmp"
    try:
        payload = {**data, "_schema_version": SETTINGS_SCHEMA_VERSION}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
        return True
    except OSError as e:
        LOG.error("save_settings failed: %s", e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def get_active_profile(settings: dict) -> dict:
    """Return the mutable settings dict for the active profile."""
    if not isinstance(settings, dict):
        return {}
    profiles = settings.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        profiles = {}
        settings["profiles"] = profiles
    name = settings.get("active_profile") or "default"
    if not isinstance(name, str):
        name = "default"
        settings["active_profile"] = name
    return profiles.setdefault(name, {})


# -----------------------------------------------------------------------
# Custom-commands persistence
# -----------------------------------------------------------------------
# Since schema v4 each profile owns its own custom_commands list. The
# top-level key is treated as a fallback / template for new profiles.
def load_custom_commands(settings: dict) -> list[dict]:
    """Return the list of custom command rule dicts for the active profile.

    Falls back to the top-level list if the profile doesn't have its own
    (which is the case for profiles created before schema v4).
    """
    profile = get_active_profile(settings)
    cmds = profile.get("custom_commands")
    if isinstance(cmds, list):
        return cmds
    cmds = settings.get("custom_commands")
    if isinstance(cmds, list):
        return cmds
    return []


def save_custom_commands(settings: dict, commands: list[dict]) -> None:
    """Persist the custom command list into the active profile (in-place)."""
    profile = get_active_profile(settings)
    profile["custom_commands"] = commands
    # Mirror to top-level as well so old code paths keep working until
    # everything has migrated to per-profile reads.
    settings["custom_commands"] = commands


def export_rules_to_json(rules):
    """Deprecated re-export — the implementation now lives in
    core.custom_commands so it can never accidentally include any
    settings keys. This shim is kept so external callers keep working;
    new code should import from core.custom_commands directly.
    """
    from .custom_commands import export_rules_to_json as _impl
    return _impl(rules)


def import_rules_from_json(payload):
    """Deprecated re-export — see export_rules_to_json above."""
    from .custom_commands import import_rules_from_json as _impl
    return _impl(payload)




# -----------------------------------------------------------------------
# Autorun rules persistence (per-profile, like custom_commands)
# -----------------------------------------------------------------------
def load_autorun_rules(settings: dict) -> list[dict]:
    """Return the autorun rule list for the active profile.

    Falls back to an empty list if the active profile has nothing
    configured (the common case for profiles that existed before the
    schema v6 migration touched them)."""
    profile = get_active_profile(settings)
    rules = profile.get("autorun_rules")
    if isinstance(rules, list):
        return rules
    return []


def save_autorun_rules(settings: dict, rules: list[dict]) -> None:
    """Persist the autorun rule list into the active profile (in-place)."""
    profile = get_active_profile(settings)
    profile["autorun_rules"] = rules



# -----------------------------------------------------------------------
# Player playtime totals (per-profile, like custom_commands and autorun)
# -----------------------------------------------------------------------
def load_player_totals(settings: dict) -> dict:
    """Return the persisted player-playtime-totals dict for the active
    profile. Mutating the returned dict in place mutates the settings
    blob — callers are expected to follow up with save_settings to
    persist."""
    profile = get_active_profile(settings)
    totals = profile.get("player_totals")
    if not isinstance(totals, dict):
        totals = {}
        profile["player_totals"] = totals
    return totals


def save_player_totals(settings: dict, totals: dict) -> None:
    """Replace the persisted totals dict for the active profile."""
    profile = get_active_profile(settings)
    profile["player_totals"] = dict(totals or {})

# -----------------------------------------------------------------------
# Schema migration
# -----------------------------------------------------------------------
def _default_settings() -> dict:
    return {
        "_schema_version":   SETTINGS_SCHEMA_VERSION,
        "active_profile":    "default",
        "profiles":          {"default": {"custom_commands": []}},
        "ui_scale_override": 1.0,
        "theme_preset":      "amber",
        "custom_commands":   [],
        "crash_limit":       3,
        "crash_window_secs": 600,
        "log_level":         "DEBUG",
        "player_count_poll_secs": 30,  # 0 = disabled
        "header_collapsed":   False,  # 0 = disabled; otherwise seconds between
                                       # automatic /list clients pings while
                                       # the server is running.
    }


def _migrate(data: dict, path: str) -> dict:
    schema = data.get("_schema_version", 1)
    if schema >= SETTINGS_SCHEMA_VERSION:
        # Still heal lightly: ensure structural invariants.
        return _heal(data)

    # Backup before we touch anything.
    _write_pre_migration_backup(path, schema)

    # v1 → v2: flat → profiles
    if schema < 2:
        top_level_keys = {"_schema_version", "profiles", "active_profile",
                          "ui_scale_override", "theme_preset", "log_to_file"}
        legacy = {k: v for k, v in data.items() if k not in top_level_keys}
        data = {
            "_schema_version":   2,
            "active_profile":    "default",
            "profiles":          {"default": legacy},
            "ui_scale_override": data.get("ui_scale_override", 1.0),
            "theme_preset":      data.get("theme_preset", "amber"),
        }
        LOG.info("Migrated settings schema 1 → 2")

    # v2 → v3: add custom_commands + crash breaker config + log_level
    if schema < 3:
        data.setdefault("custom_commands",   [])
        data.setdefault("crash_limit",       3)
        data.setdefault("crash_window_secs", 600)
        data.setdefault("log_level",         "DEBUG")
        LOG.info("Migrated settings schema 2 → 3")

    # v3 → v4: promote custom_commands to per-profile
    if schema < 4:
        global_cmds = data.get("custom_commands") or []
        profiles = data.setdefault("profiles", {})
        for name, profile in profiles.items():
            if isinstance(profile, dict) and "custom_commands" not in profile:
                # Seed every existing profile with the previous global list.
                profile["custom_commands"] = list(global_cmds)
        LOG.info("Migrated settings schema 3 → 4 "
                 "(custom_commands are now per-profile)")

    # v4 → v5: add player_count_poll_secs (default 30s; 0 disables)
    if schema < 5:
        data.setdefault("player_count_poll_secs", 30)
        LOG.info("Migrated settings schema 4 → 5 "
                 "(added player_count_poll_secs)")

    # v5 → v6: each profile gets its own autorun_rules slot
    if schema < 6:
        profiles = data.setdefault("profiles", {})
        for name, profile in profiles.items():
            if isinstance(profile, dict):
                profile.setdefault("autorun_rules", [])
        LOG.info("Migrated settings schema 5 → 6 "
                 "(added autorun_rules per profile)")

    # v6 → v7: each profile gets its own player_totals dict
    # (player name → cumulative seconds played across all sessions)
    if schema < 7:
        profiles = data.setdefault("profiles", {})
        for name, profile in profiles.items():
            if isinstance(profile, dict):
                profile.setdefault("player_totals", {})
        LOG.info("Migrated settings schema 6 → 7 "
                 "(added player_totals per profile)")

    data["_schema_version"] = SETTINGS_SCHEMA_VERSION
    return _heal(data)


def _heal(data: dict) -> dict:
    if not isinstance(data.get("profiles"), dict):
        data["profiles"] = {"default": {"custom_commands": []}}
    if not isinstance(data.get("active_profile"), str):
        data["active_profile"] = "default"
    data["profiles"].setdefault(data["active_profile"], {})
    # Make sure the active profile has its own custom_commands slot.
    active = data["profiles"][data["active_profile"]]
    if isinstance(active, dict):
        active.setdefault("custom_commands", data.get("custom_commands") or [])
    return data


def _write_pre_migration_backup(path: str, old_schema: Any) -> None:
    """Copy the current settings file to a timestamped .bak before any
    migration mutates it. Best-effort; swallows IO errors."""
    if not os.path.exists(path):
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = f"{path}.v{old_schema}.{stamp}.bak"
    try:
        shutil.copy2(path, bak)
        LOG.info("Wrote pre-migration backup: %s", os.path.basename(bak))
    except OSError as e:
        LOG.warning("Could not write pre-migration backup: %s", e)
