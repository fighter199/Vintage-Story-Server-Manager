"""Tests for core.profiles — create / duplicate / rename / delete / switch."""
import importlib
import os

import pytest


@pytest.fixture
def tmp_script_dir(tmp_path, monkeypatch):
    """Settings and chat logs in a fresh tmp folder (run_tests.py
    supplies its own fixture of the same name)."""
    import core.constants as cst
    monkeypatch.setattr(cst, "script_dir", lambda: str(tmp_path))
    import core.settings as cs
    importlib.reload(cs)
    return tmp_path, cs


def _env(tmp_script_dir):
    tmp_path, cs = tmp_script_dir
    import core.profiles as cp
    importlib.reload(cp)                 # picks up the reloaded chat_log_path
    return tmp_path, cs, cp, cs.load_settings()


def _write_chat(cs, name, text="{}"):
    path = cs.chat_log_path(name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class TestCreate:
    def test_new_profile_starts_blank(self, tmp_script_dir):
        _, _, cp, s = _env(tmp_script_dir)
        # The top-level list mirrors the last-saved profile; not a template.
        s["custom_commands"] = [{"trigger": "!hi"}]
        name = cp.create_profile(s, "  Creative   world ")
        assert name == "Creative world"
        assert cp.profile_names(s) == ["default", "Creative world"]
        prof = s["profiles"][name]
        assert prof == {"custom_commands": [], "autorun_rules": [],
                        "player_totals": {}}

    def test_duplicate_copies_everything_but_playtime(self, tmp_script_dir):
        _, _, cp, s = _env(tmp_script_dir)
        s["profiles"]["default"].update(
            server_path="/srv/vs/VintagestoryServer", world_folder="/srv/data",
            autorun_rules=[{"command": "/autosavenow"}],
            player_totals={"Alice": 3600})
        name = cp.create_profile(s, "Test copy", copy_from="default")
        prof = s["profiles"][name]
        assert prof["server_path"] == "/srv/vs/VintagestoryServer"
        assert prof["autorun_rules"] == [{"command": "/autosavenow"}]
        assert prof["player_totals"] == {}
        assert s["profiles"]["default"]["player_totals"] == {"Alice": 3600}
        prof["autorun_rules"].append({"command": "/x"})
        assert len(s["profiles"]["default"]["autorun_rules"]) == 1

    def test_rejects_empty_or_long_names(self, tmp_script_dir):
        _, _, cp, s = _env(tmp_script_dir)
        for bad in ("", "   ", "x" * 41):
            with pytest.raises(cp.ProfileError):
                cp.create_profile(s, bad)
        assert cp.profile_names(s) == ["default"]

    def test_rejects_duplicates_ignoring_case(self, tmp_script_dir):
        _, _, cp, s = _env(tmp_script_dir)
        with pytest.raises(cp.ProfileError, match="already a profile"):
            cp.create_profile(s, "DEFAULT")

    def test_rejects_names_sharing_a_chat_log(self, tmp_script_dir):
        _, _, cp, s = _env(tmp_script_dir)
        cp.create_profile(s, "my server")
        with pytest.raises(cp.ProfileError, match="too similar"):
            cp.create_profile(s, "my_server")

    def test_copy_from_missing_profile(self, tmp_script_dir):
        _, _, cp, s = _env(tmp_script_dir)
        with pytest.raises(cp.ProfileError):
            cp.create_profile(s, "b", copy_from="nope")


class TestRename:
    def test_rename_keeps_order_active_and_chat_log(self, tmp_script_dir):
        _, cs, cp, s = _env(tmp_script_dir)
        cp.create_profile(s, "second")
        old_log = _write_chat(cs, "default", '{"v": 1}')
        new = cp.rename_profile(s, "default", "Survival")
        assert new == "Survival"
        assert cp.profile_names(s) == ["Survival", "second"]
        assert cp.active_profile_name(s) == "Survival"
        assert not os.path.exists(old_log)
        with open(cs.chat_log_path("Survival"), encoding="utf-8") as f:
            assert f.read() == '{"v": 1}'

    def test_rename_inactive_profile_leaves_active_alone(self, tmp_script_dir):
        _, _, cp, s = _env(tmp_script_dir)
        cp.create_profile(s, "second")
        cp.rename_profile(s, "second", "third")
        assert cp.active_profile_name(s) == "default"
        assert cp.profile_names(s) == ["default", "third"]

    def test_case_only_rename_allowed(self, tmp_script_dir):
        _, cs, cp, s = _env(tmp_script_dir)
        _write_chat(cs, "default")
        assert cp.rename_profile(s, "default", "Default") == "Default"
        assert os.path.isfile(cs.chat_log_path("Default"))

    def test_rename_onto_another_profile_refused(self, tmp_script_dir):
        _, _, cp, s = _env(tmp_script_dir)
        cp.create_profile(s, "second")
        with pytest.raises(cp.ProfileError):
            cp.rename_profile(s, "second", "Default")

    def test_stray_chat_log_is_kept_not_overwritten(self, tmp_script_dir):
        _, cs, cp, s = _env(tmp_script_dir)
        cp.create_profile(s, "second")
        _write_chat(cs, "second", "mine")
        stray = _write_chat(cs, "third", "stray")
        cp.rename_profile(s, "second", "third")
        with open(stray, encoding="utf-8") as f:
            assert f.read() == "mine"
        with open(stray + ".old", encoding="utf-8") as f:
            assert f.read() == "stray"


class TestDeleteAndSwitch:
    def test_delete_removes_profile_and_chat_log(self, tmp_script_dir):
        _, cs, cp, s = _env(tmp_script_dir)
        cp.create_profile(s, "second")
        log = _write_chat(cs, "second")
        cp.delete_profile(s, "second")
        assert cp.profile_names(s) == ["default"]
        assert not os.path.exists(log)

    def test_cannot_delete_active_or_last(self, tmp_script_dir):
        _, _, cp, s = _env(tmp_script_dir)
        with pytest.raises(cp.ProfileError, match="only profile"):
            cp.delete_profile(s, "default")
        cp.create_profile(s, "second")
        with pytest.raises(cp.ProfileError, match="Switch to another"):
            cp.delete_profile(s, "default")

    def test_switch(self, tmp_script_dir):
        _, cs, cp, s = _env(tmp_script_dir)
        cp.create_profile(s, "second")
        cp.set_active_profile(s, "second")
        assert cs.get_active_profile(s) is s["profiles"]["second"]
        with pytest.raises(cp.ProfileError):
            cp.set_active_profile(s, "missing")

    def test_survives_save_and_load(self, tmp_script_dir):
        _, cs, cp, s = _env(tmp_script_dir)
        cp.create_profile(s, "second", copy_from="default")
        cp.set_active_profile(s, "second")
        cs.save_settings(s)
        again = cs.load_settings()
        assert cp.profile_names(again) == ["default", "second"]
        assert cp.active_profile_name(again) == "second"


class TestUnsavedFields:
    def test_nothing_changed_against_defaults(self):
        from core.profiles import PROFILE_FIELDS, unsaved_fields
        assert unsaved_fields({}, dict(PROFILE_FIELDS)) == []

    def test_reports_changed_keys(self):
        from core.profiles import PROFILE_FIELDS, unsaved_fields
        profile = {"server_path": "/srv/vs", "autorestart": True,
                   "max_backups": 10}
        current = dict(PROFILE_FIELDS, server_path="/srv/vs ",
                       autorestart=1, max_backups="10")
        assert unsaved_fields(profile, current) == []
        current.update(world_folder="/data", autorestart=False)
        assert unsaved_fields(profile, current) == ["world_folder", "autorestart"]

    def test_ignores_keys_not_shown(self):
        from core.profiles import unsaved_fields
        assert unsaved_fields({"server_path": "/a"}, {}) == []
