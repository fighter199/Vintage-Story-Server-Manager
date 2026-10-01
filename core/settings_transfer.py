"""
core/settings_transfer.py — Move VSSM's settings to another install.

An export is one JSON file holding every profile (server and folders,
schedules, custom commands, autorun rules, playtime), the app-wide
preferences (theme, text size, crash-loop limits, …) and the entries of
vs_commands_user.json. Window layout and chat logs stay behind: the
first belongs to the screen, the second can be large and is per install.

Importing merges into the current settings; nothing is removed.
"""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime

from .constants import APP_VERSION
from .profiles import create_profile, profile_names, validate_name, ProfileError

EXPORT_FORMAT = "vssm-settings"
EXPORT_VERSION = 1

# App-wide preferences carried by an export.
PREFERENCE_KEYS = ("theme_preset", "custom_theme_colors", "ui_scale_override",
                   "crash_limit", "crash_window_secs", "log_level",
                   "player_count_poll_secs", "auto_check_updates")


class TransferError(ValueError):
    """The file can't be imported; the message is user-facing."""


def export_bundle(settings: dict, user_commands: dict | None = None) -> dict:
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "app_version": APP_VERSION,
        "exported": datetime.now().isoformat(timespec="seconds"),
        "active_profile": settings.get("active_profile") or "default",
        "profiles": copy.deepcopy(settings.get("profiles") or {}),
        "preferences": {k: copy.deepcopy(settings[k])
                        for k in PREFERENCE_KEYS if k in settings},
        "user_commands": copy.deepcopy(user_commands or {}),
    }


def write_bundle(path: str, bundle: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def read_bundle(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise TransferError(f"Couldn't read the file: {e}") from e
    except ValueError as e:
        raise TransferError("This isn't a VSSM settings export (not valid "
                            "JSON).") from e
    if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT:
        raise TransferError("This isn't a VSSM settings export.")
    if int(data.get("version") or 0) > EXPORT_VERSION:
        raise TransferError("This export was made by a newer VSSM "
                            f"({data.get('app_version', '?')}); update VSSM "
                            "to import it.")
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not all(
            isinstance(k, str) and isinstance(v, dict) for k, v in profiles.items()):
        raise TransferError("The export's profile list is damaged.")
    if not isinstance(data.get("preferences", {}), dict) or \
            not isinstance(data.get("user_commands", {}), dict):
        raise TransferError("The export is damaged.")
    return data


def clashing_profiles(settings: dict, bundle: dict) -> list[str]:
    """Imported profile names that already exist (ignoring case)."""
    mine = {n.casefold() for n in profile_names(settings)}
    return [n for n in bundle["profiles"] if n.casefold() in mine]


def _free_name(settings: dict, name: str) -> str:
    base = f"{name} (imported)"
    for n in range(1, 1000):
        candidate = base if n == 1 else f"{name} (imported {n})"
        try:
            return validate_name(settings, candidate[:40])
        except ProfileError:
            continue
    raise TransferError(f'No free name for the imported profile "{name}".')


def import_profiles(settings: dict, bundle: dict, replace: bool) -> list[tuple]:
    """Add the bundle's profiles. A name that already exists is replaced
    (replace=True) or imported under a new name. Returns
    [(imported name, name in settings, "added"|"replaced"|"renamed")]."""
    done = []
    profiles = settings.setdefault("profiles", {})
    for name, data in bundle["profiles"].items():
        data = copy.deepcopy(data)
        existing = next((n for n in profiles if n.casefold() == name.casefold()), None)
        if existing is not None and replace:
            profiles[existing] = data
            done.append((name, existing, "replaced"))
            continue
        try:
            target = validate_name(settings, name) if existing is None \
                else _free_name(settings, name)
        except ProfileError:
            target = _free_name(settings, name)
        create_profile(settings, target)
        profiles[target] = data
        done.append((name, target, "renamed" if existing is not None
                     or target != name else "added"))
    return done


def import_preferences(settings: dict, bundle: dict) -> list[str]:
    applied = []
    for key, value in bundle.get("preferences", {}).items():
        if key in PREFERENCE_KEYS:
            settings[key] = copy.deepcopy(value)
            applied.append(key)
    return applied


def merge_user_commands(mine: dict, theirs: dict) -> tuple[dict, int]:
    """Theirs added to mine, category by category (same name → theirs).
    Returns (merged, number of commands taken from theirs)."""
    merged = copy.deepcopy(mine or {})
    count = 0
    for cat, cmds in (theirs or {}).items():
        if cat.startswith("_") or not isinstance(cmds, dict):
            if cat.startswith("_") and cat not in merged:
                merged[cat] = copy.deepcopy(cmds)
            continue
        target = merged.setdefault(cat, {})
        if not isinstance(target, dict):
            continue
        for name, entry in cmds.items():
            target[name] = copy.deepcopy(entry)
            count += 1
    return merged, count
