"""
core/command_files.py — The COMMANDS tab's reference data, from two files.

    vs_commands_builtin.json   shipped with VSSM; replaced on every update
    vs_commands_user.json      yours; never shipped, so updates can't touch it

Both have the same shape — {category: {command name: entry}} — and are
merged at load time. A user entry with the same name as a built-in one
replaces it (e.g. to fix a description); a user entry of `null` hides
the built-in command. User entries are tagged with "_source": "user".

Older VSSM versions shipped the list as vs_commands.json, which people
edited in place. That name is no longer shipped, so unzipping an update
leaves it alone; on first load its additions and edits are moved into
vs_commands_user.json and the old file is renamed to vs_commands.json.old.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from .constants import LOG
from .parsers import parse_json5_ish

BUILTIN_FILE = "vs_commands_builtin.json"
USER_FILE = "vs_commands_user.json"
LEGACY_FILE = "vs_commands.json"

# Commands earlier releases shipped and 3.4 removed because they need a
# player caller (the console has no position/inventory/claims). Never
# migrate these out of a legacy file as if they were user additions.
RETIRED_COMMANDS = frozenset({
    "/serverconfig setspawnhere", "/clearinv", "/kill", "/gamemode (self)",
    "/tp (to coords)", "/tp (to absolute coords)", "/tp (relative offset)",
    "/tp (to player)", "/tpwp", "/waypoint list", "/waypoint add",
    "/waypoint remove", "/land list", "/land info", "/land free",
    "/land adminfree", "/land claim new", "/land claim save",
    "/land claim cancel", "/land claim grant", "/land claim revoke",
    "/land claim grow", "/land claim shrink", "/debug rift spawn",
    "/whenwillitstopraining",
})

USER_TEMPLATE = """{
  "_note": "Your own commands for the COMMANDS tab. This file is never overwritten by VSSM updates. Same format as vs_commands_builtin.json: categories containing commands. A command with the same name as a built-in one replaces it; set a built-in command to null to hide it. Press Reload in the COMMANDS tab after editing.",

  "My commands": {
    "/example": {
      "template": "/announce {message}",
      "description": "Example — edit or delete me.",
      "args": [
        {"name": "message", "type": "text", "hint": "Text to broadcast"}
      ]
    }
  }
}
"""


def normalize_entry(name: str, raw) -> Optional[dict]:
    """Fill in defaults for one command entry (a bare string is taken as
    its description). None for anything unusable."""
    if isinstance(raw, str):
        return {"description": raw, "template": name, "args": []}
    if not isinstance(raw, dict):
        return None
    entry = dict(raw)
    entry.setdefault("description", "")
    entry.setdefault("template", name)
    entry.setdefault("args", [])
    return entry


def read_command_file(path: str) -> dict:
    """{category: {name: raw entry}} from a JSON / JSON5-ish file; {} if
    the file is missing. Raises ValueError for an unreadable file."""
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = parse_json5_ish(f.read())
    if not isinstance(data, dict):
        raise ValueError(f"{os.path.basename(path)}: expected an object of categories")
    return {cat: cmds for cat, cmds in data.items()
            if not cat.startswith("_") and isinstance(cmds, dict)}


def merge_commands(builtin: dict, user: dict) -> dict:
    """Built-in categories/commands with the user's added, replaced (same
    name) or hidden (null) on top. Category order: built-in first."""
    out: dict = {}
    for cat, cmds in builtin.items():
        entries = {n: e for n, e in ((n, normalize_entry(n, r)) for n, r in cmds.items())
                   if e is not None}
        if entries:
            out[cat] = entries
    for cat, cmds in user.items():
        for name, raw in cmds.items():
            if raw is None:                               # hide a built-in
                for entries in out.values():
                    entries.pop(name, None)
                continue
            entry = normalize_entry(name, raw)
            if entry is None:
                continue
            entry["_source"] = "user"
            for entries in out.values():                  # replace in place
                if name in entries:
                    entries[name] = entry
                    break
            else:
                out.setdefault(cat, {})[name] = entry
    return {cat: entries for cat, entries in out.items() if entries}


def extract_user_entries(legacy: dict, builtin: dict) -> dict:
    """What someone added to or changed in an old vs_commands.json,
    relative to the current built-in list (retired commands excluded)."""
    known = {name: raw for cmds in builtin.values() for name, raw in cmds.items()}
    out: dict = {}
    for cat, cmds in legacy.items():
        for name, raw in cmds.items():
            if name in RETIRED_COMMANDS:
                continue
            if name in known and normalize_entry(name, raw) == normalize_entry(name, known[name]):
                continue
            out.setdefault(cat, {})[name] = raw
    return out


def migrate_legacy_file(folder: str) -> int:
    """Move additions/edits from a legacy vs_commands.json into the user
    file (without overwriting anything already there) and rename the
    legacy file. Returns how many commands were moved."""
    legacy_path = os.path.join(folder, LEGACY_FILE)
    if not os.path.isfile(legacy_path):
        return 0
    try:
        legacy = read_command_file(legacy_path)
        builtin = read_command_file(os.path.join(folder, BUILTIN_FILE))
    except (OSError, ValueError) as e:
        LOG.warning("could not read legacy %s: %s", LEGACY_FILE, e)
        return 0
    moved = extract_user_entries(legacy, builtin)
    count = 0
    if moved:
        user_path = os.path.join(folder, USER_FILE)
        try:
            user = read_command_file(user_path)
        except (OSError, ValueError) as e:
            # Don't clobber a user file we can't parse — leave the legacy
            # file in place and try again next launch.
            LOG.warning("not migrating: %s is unreadable (%s)", USER_FILE, e)
            return 0
        for cat, cmds in moved.items():
            for name, raw in cmds.items():
                if not any(name in c for c in user.values()):
                    user.setdefault(cat, {})[name] = raw
                    count += 1
        payload = {"_note": json.loads(USER_TEMPLATE)["_note"], **user}
        tmp = user_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, user_path)
    try:
        os.replace(legacy_path, legacy_path + ".old")
    except OSError as e:
        LOG.warning("could not rename %s: %s", LEGACY_FILE, e)
    if count:
        LOG.info("moved %d custom command(s) from %s to %s",
                 count, LEGACY_FILE, USER_FILE)
    return count


def load_commands(folder: str) -> tuple[dict, list[str]]:
    """Merged command reference for the app folder, plus any problems to
    show the user (e.g. a typo in their file). Never raises."""
    problems: list[str] = []
    try:
        migrate_legacy_file(folder)
    except OSError as e:
        problems.append(f"Could not migrate {LEGACY_FILE}: {e}")
    try:
        builtin = read_command_file(os.path.join(folder, BUILTIN_FILE))
    except (OSError, ValueError) as e:
        builtin = {}
        problems.append(f"{BUILTIN_FILE}: {e}")
    try:
        user = read_command_file(os.path.join(folder, USER_FILE))
    except (OSError, ValueError) as e:
        user = {}
        problems.append(f"{USER_FILE} has an error and was skipped: {e}")
    return merge_commands(builtin, user), problems


def ensure_user_file(folder: str) -> str:
    """Path of the user file, creating it from the template if missing."""
    path = os.path.join(folder, USER_FILE)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(USER_TEMPLATE)
    return path
