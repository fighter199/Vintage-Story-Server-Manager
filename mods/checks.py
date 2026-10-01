"""
mods/checks.py — Problems in the mods folder that would bite at start.

Reads each mod's modinfo.json (via LocalModInspector) and reports:
  * error — a dependency that's missing or disabled; a mod that needs a
            newer Vintage Story than the server runs; the same mod
            installed twice;
  * warn  — a dependency installed in an older version than required;
            a mod whose modinfo couldn't be read;
  * info  — client-only mods (the server doesn't load them).

Mods built for an older game version are not flagged: Vintage Story
lists a minimum version, and most such mods still work.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from core.parsers import version_is_newer

from .inspector import LocalModInspector

# Mods that are part of the game itself.
BUILTIN_MODS = {"game", "survival", "creative"}
MOD_SUFFIXES = (".zip", ".jar", ".dll", ".cs")


@dataclass
class ModProblem:
    level: str           # "error" | "warn" | "info"
    mod: str             # file name
    message: str

    def __str__(self) -> str:
        return f"{self.mod}: {self.message}"


def mod_id(info: dict) -> str:
    """The modid, or the one the game derives from the name when a mod
    doesn't declare it (lower case, letters and digits only)."""
    mid = info.get("modid")
    if mid:
        return str(mid).strip().lower()
    name = os.path.splitext(str(info.get("name") or ""))[0]
    return re.sub(r"[^a-z0-9]", "", name.lower())


def scan_mods_folder(folder: str) -> dict:
    """{file name: inspector info} for every mod file or folder."""
    out = {}
    if not folder or not os.path.isdir(folder):
        return out
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        base = name[:-9] if name.lower().endswith(".disabled") else name
        if os.path.isdir(path) or base.lower().endswith(MOD_SUFFIXES):
            try:
                out[name] = LocalModInspector.read_mod_file(path)
            except Exception as e:                       # pragma: no cover
                out[name] = {"name": name, "error": str(e), "dependencies": {}}
    return out


def _wanted(req: str) -> str:
    req = (req or "").strip()
    return "" if req in ("", "*") else req


def check_mods(mods: dict, game_version: str | None = None) -> list[ModProblem]:
    """Problems with the mods in `mods` ({file name: inspector info}),
    errors first."""
    problems: list[ModProblem] = []
    enabled = {f: i for f, i in mods.items() if not f.lower().endswith(".disabled")}
    disabled_ids = {mod_id(i) for f, i in mods.items() if f not in enabled}

    by_id: dict = {}
    for f, info in enabled.items():
        if info.get("error") and not info.get("modid"):
            problems.append(ModProblem("warn", f, "couldn't read its modinfo "
                                       f"({info['error']}); not checked"))
            continue
        by_id.setdefault(mod_id(info), []).append(f)

    for mid, files in by_id.items():
        if len(files) > 1:
            problems.append(ModProblem(
                "error", files[0], f"installed {len(files)} times "
                f"({', '.join(files)}) — remove all but one"))

    for f, info in enabled.items():
        if mod_id(info) not in by_id:
            continue
        for dep, req in (info.get("dependencies") or {}).items():
            dep_id, req = dep.strip().lower(), _wanted(req)
            if dep_id in BUILTIN_MODS:
                if dep_id == "game" and req and game_version and \
                        version_is_newer(req, game_version):
                    problems.append(ModProblem(
                        "error", f, f"needs Vintage Story {req} or newer; "
                        f"the server is {game_version}"))
                continue
            if dep_id not in by_id:
                why = "is disabled" if dep_id in disabled_ids else "isn't installed"
                problems.append(ModProblem(
                    "error", f, f"needs the mod \"{dep}\"{' ' + req if req else ''}, "
                    f"which {why}"))
                continue
            have = enabled[by_id[dep_id][0]].get("version") or ""
            if req and have and version_is_newer(req, str(have)):
                problems.append(ModProblem(
                    "warn", f, f"needs \"{dep}\" {req} or newer; "
                    f"{have} is installed"))

    for f, info in enabled.items():
        if str(info.get("side") or "").lower() == "client":
            problems.append(ModProblem("info", f, "client-side only — the "
                                       "server doesn't load it"))

    order = {"error": 0, "warn": 1, "info": 2}
    problems.sort(key=lambda p: (order[p.level], p.mod.lower()))
    return problems
