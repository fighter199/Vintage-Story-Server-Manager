"""
mods/inspector.py — Read modinfo.json from installed VS mods.

Supports .zip, directory, .cs, .dll, .disabled variants.
Uses the JSON5-aware parser so real-world modinfo files with // comments
don't cause false-negative "can't read" results.
"""
from __future__ import annotations

import os
import zipfile

from core.parsers import parse_json5_ish


class LocalModInspector:

    @classmethod
    def read_mod_file(cls, path: str) -> dict:
        """Return dict: modid, name, version, side, path, dependencies, error?"""
        result = {
            "modid":        None,
            "name":         os.path.basename(path),
            "version":      None,
            "side":         None,
            "path":         path,
            "dependencies": {},
            "error":        None,
        }
        try:
            real_path = path
            if real_path.lower().endswith(".disabled"):
                real_path = real_path[:-9]

            info = None
            lower = real_path.lower()
            if os.path.isdir(path):
                info = cls._read_from_dir(path)
            elif lower.endswith(".zip") or lower.endswith(".jar"):
                info = cls._read_from_zip(path)
            elif lower.endswith(".cs"):
                sibling = os.path.join(os.path.dirname(path), "modinfo.json")
                if os.path.isfile(sibling):
                    with open(sibling, "r", encoding="utf-8", errors="replace") as f:
                        info = parse_json5_ish(f.read())
            elif lower.endswith(".dll"):
                sibling = os.path.join(os.path.dirname(path), "modinfo.json")
                if os.path.isfile(sibling):
                    with open(sibling, "r", encoding="utf-8", errors="replace") as f:
                        info = parse_json5_ish(f.read())
                else:
                    result["error"] = "compiled (.dll) — metadata unreadable"
            else:
                result["error"] = "unsupported file type"

            if info:
                lk = {str(k).lower(): v for k, v in info.items()}
                result["modid"]   = lk.get("modid")
                result["name"]    = lk.get("name") or result["name"]
                result["version"] = lk.get("version")
                deps = lk.get("dependencies") or {}
                if isinstance(deps, dict):
                    result["dependencies"] = {str(k): str(v) for k, v in deps.items()}
                side = lk.get("side")
                result["side"] = str(side).strip().lower() if side else "universal"
        except Exception as e:
            result["error"] = f"read failed: {e}"
        return result

    @classmethod
    def _read_from_dir(cls, folder: str):
        candidate = os.path.join(folder, "modinfo.json")
        if not os.path.isfile(candidate):
            return None
        with open(candidate, "r", encoding="utf-8", errors="replace") as f:
            return parse_json5_ish(f.read())

    @classmethod
    def _read_from_zip(cls, zip_path: str):
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                candidates = [n for n in zf.namelist()
                              if n.lower().endswith("modinfo.json")]
                if not candidates:
                    return None
                candidates.sort(key=lambda n: n.count("/"))
                with zf.open(candidates[0]) as f:
                    text = f.read().decode("utf-8", errors="replace")
                return parse_json5_ish(text)
        except (zipfile.BadZipFile, OSError):
            return None
