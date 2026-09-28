"""
core/profiles.py — Create, duplicate, rename, delete and switch profiles.

A profile is one server set-up: its executable, mods / world / backup
folders, schedules, custom commands, autorun rules and playtime totals.
Profiles live in settings["profiles"][name] and settings["active_profile"]
names the one in use. Each also has its own chat history file
(chat_log_<name>.json, see core.settings.chat_log_path), which is moved
or removed along with it.

These functions change the settings dict in place; the caller saves it.
"""
from __future__ import annotations

import copy
import os

from .constants import LOG
from .settings import chat_log_path

MAX_NAME_LENGTH = 40

# The settings each profile stores, with their defaults. The app shows
# each one in a Tk variable named <key>_var.
PROFILE_FIELDS: dict = {
    "server_path":         "",
    "mods_folder":         "",
    "world_folder":        "",
    "backup_dir":          "",
    "max_backups":         "10",
    "max_start_backups":   "5",
    "max_stop_backups":    "5",
    "autorestart":         False,
    "autosave_enabled":    False,
    "autosave_interval":   "30",
    "autosave_cmd":        True,
    "cron_expr":           "",
    "shutdown_timeout":    "30",
    "backup_before_start": False,
    "backup_before_stop":  False,
    "check_players_before_restart":           False,
    "check_players_before_scheduled_restart": False,
    "check_players_before_shutdown":          False,
}


class ProfileError(ValueError):
    """A profile action that can't be done; the message is user-facing."""


def _profiles(settings: dict) -> dict:
    profiles = settings.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
        settings["profiles"] = profiles
    return profiles


def profile_names(settings: dict) -> list[str]:
    """Every profile name, in the order they were created."""
    return list(_profiles(settings))


def active_profile_name(settings: dict) -> str:
    name = settings.get("active_profile")
    return name if isinstance(name, str) and name else "default"


def validate_name(settings: dict, name: str, ignore: str | None = None) -> str:
    """Return `name` tidied up (surrounding and repeated spaces removed),
    or raise ProfileError. `ignore` is the profile being renamed, which
    may keep its own name in a different case."""
    clean = " ".join(str(name or "").split())
    if not clean:
        raise ProfileError("Enter a name for the profile.")
    if len(clean) > MAX_NAME_LENGTH:
        raise ProfileError(f"Keep the name to {MAX_NAME_LENGTH} characters or fewer.")
    for existing in profile_names(settings):
        if existing == ignore:
            continue
        if existing.casefold() == clean.casefold():
            raise ProfileError(f'There is already a profile called "{existing}".')
        if chat_log_path(existing).casefold() == chat_log_path(clean).casefold():
            # Names that differ only in punctuation would share one
            # chat history file.
            raise ProfileError(f'"{clean}" is too similar to the profile '
                               f'"{existing}". Try another name.')
    return clean


def create_profile(settings: dict, name: str, copy_from: str | None = None) -> str:
    """Add a profile and return its (tidied) name.

    A new profile starts empty: no folders, custom commands or autorun
    rules. (The top-level custom_commands list only mirrors whichever
    profile saved last, so it's no template.) With `copy_from` it gets a
    copy of that profile's folders, schedules, custom commands and
    autorun rules — but not its playtime totals or chat history, which
    belong to the original server."""
    clean = validate_name(settings, name)
    profiles = _profiles(settings)
    if copy_from is not None:
        if copy_from not in profiles:
            raise ProfileError(f'There is no profile called "{copy_from}".')
        new = copy.deepcopy(profiles[copy_from])
        new["player_totals"] = {}
    else:
        new = {"custom_commands": [], "autorun_rules": [], "player_totals": {}}
    profiles[clean] = new
    return clean


def rename_profile(settings: dict, old: str, new: str) -> str:
    """Rename a profile (keeping its place in the list) and its chat
    history file. Returns the new name."""
    profiles = _profiles(settings)
    if old not in profiles:
        raise ProfileError(f'There is no profile called "{old}".')
    clean = validate_name(settings, new, ignore=old)
    if clean == old:
        return old
    settings["profiles"] = {(clean if k == old else k): v
                            for k, v in profiles.items()}
    if active_profile_name(settings) == old:
        settings["active_profile"] = clean
    _move_chat_log(old, clean)
    return clean


def delete_profile(settings: dict, name: str) -> None:
    """Remove a profile and its chat history. The active profile and
    the last remaining one can't be deleted."""
    profiles = _profiles(settings)
    if name not in profiles:
        raise ProfileError(f'There is no profile called "{name}".')
    if len(profiles) <= 1:
        raise ProfileError("This is the only profile, so it can't be deleted.")
    if name == active_profile_name(settings):
        raise ProfileError("Switch to another profile before deleting this one.")
    del profiles[name]
    path = chat_log_path(name)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as e:
        LOG.warning("could not remove chat log %s: %s", path, e)


def unsaved_fields(profile: dict, current: dict) -> list[str]:
    """Keys of PROFILE_FIELDS whose value in `current` (what the UI
    shows) differs from what `profile` has saved."""
    changed = []
    for key, default in PROFILE_FIELDS.items():
        if key not in current:
            continue
        saved = profile.get(key, default)
        if isinstance(default, bool):
            same = bool(current[key]) == bool(saved)
        else:
            same = str(current[key]).strip() == str(saved).strip()
        if not same:
            changed.append(key)
    return changed


def set_active_profile(settings: dict, name: str) -> None:
    if name not in _profiles(settings):
        raise ProfileError(f'There is no profile called "{name}".')
    settings["active_profile"] = name


def _move_chat_log(old: str, new: str) -> None:
    src, dst = chat_log_path(old), chat_log_path(new)
    if src == dst or not os.path.isfile(src):
        return
    try:
        if os.path.exists(dst) and not os.path.samefile(src, dst):
            # Left behind by a profile deleted outside VSSM; keep it.
            os.replace(dst, dst + ".old")
        os.replace(src, dst)
    except OSError as e:
        LOG.warning("could not rename chat log %s -> %s: %s", src, dst, e)
