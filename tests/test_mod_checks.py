"""Tests for mods.checks."""
import json
import os
import zipfile

from mods.checks import check_mods, mod_id, scan_mods_folder


def mod(modid=None, version="1.0.0", deps=None, side="universal", name=None,
        error=None):
    return {"modid": modid, "name": name or (modid or "x"), "version": version,
            "side": side, "dependencies": deps or {}, "error": error}


def _levels(problems):
    return [(p.level, p.mod) for p in problems]


class TestChecks:
    def test_clean_set(self):
        mods = {"a.zip": mod("a", deps={"game": "1.20.0", "b": "1.0.0"}),
                "b.zip": mod("b", version="1.2.0")}
        assert check_mods(mods, "1.22.2") == []

    def test_missing_and_disabled_dependency(self):
        mods = {"a.zip": mod("a", deps={"b": "", "c": "*"}),
                "c.zip.disabled": mod("c")}
        problems = check_mods(mods)
        assert _levels(problems) == [("error", "a.zip"), ("error", "a.zip")]
        text = " | ".join(p.message for p in problems)
        assert '"b", which isn\'t installed' in text
        assert '"c", which is disabled' in text

    def test_dependency_too_old_is_a_warning(self):
        mods = {"a.zip": mod("a", deps={"lib": "2.0.0"}),
                "lib.zip": mod("lib", version="1.5.0")}
        (p,) = check_mods(mods)
        assert p.level == "warn" and "2.0.0 or newer; 1.5.0" in p.message

    def test_game_version(self):
        mods = {"new.zip": mod("new", deps={"game": "1.23.0"}),
                "old.zip": mod("old", deps={"game": "1.18.0"})}
        (p,) = check_mods(mods, "1.22.2")
        assert p.mod == "new.zip" and "1.23.0 or newer; the server is 1.22.2" in p.message
        assert check_mods(mods, None) == []          # unknown version: skip

    def test_duplicates(self):
        mods = {"a_1.0.zip": mod("a"), "a_1.1.zip": mod("A", version="1.1.0"),
                "a_0.9.zip.disabled": mod("a")}
        (p,) = check_mods(mods)
        assert p.level == "error" and "installed 2 times" in p.message

    def test_unreadable_and_client_side(self):
        mods = {"broken.dll": mod(None, error="compiled (.dll) — metadata unreadable",
                                  name="broken.dll"),
                "hud.zip": mod("hud", side="client")}
        assert _levels(check_mods(mods)) == [("warn", "broken.dll"),
                                             ("info", "hud.zip")]

    def test_modid_derived_from_name(self):
        assert mod_id({"name": "Better Ruins!"}) == "betterruins"
        mods = {"a.zip": mod("a", deps={"betterruins": ""}),
                "br.zip": mod(None, name="Better Ruins")}
        assert check_mods(mods) == []


class TestScan:
    def test_scan_folder(self, tmp_path):
        folder = str(tmp_path)
        with zipfile.ZipFile(os.path.join(folder, "a.zip"), "w") as z:
            z.writestr("modinfo.json", json.dumps(
                {"modid": "a", "version": "1.0.0", "dependencies": {"b": "1.0.0"}}))
        with zipfile.ZipFile(os.path.join(folder, "b.zip.disabled"), "w") as z:
            z.writestr("modinfo.json", json.dumps({"modid": "b", "version": "1.0.0"}))
        open(os.path.join(folder, "readme.txt"), "w").close()
        mods = scan_mods_folder(folder)
        assert sorted(mods) == ["a.zip", "b.zip.disabled"]
        (p,) = check_mods(mods)
        assert "which is disabled" in p.message
        assert scan_mods_folder(os.path.join(folder, "nope")) == {}
