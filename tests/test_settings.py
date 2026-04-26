"""Tests for core.settings — load/save round-trip, migration, import/export."""
import json
import os

import pytest


@pytest.fixture
def tmp_script_dir(tmp_path, monkeypatch):
    """Patch core.constants.script_dir to point at a fresh tmp folder.

    We do this fixture-style so each test gets its own isolated settings
    file and we can re-use it across all tests.
    """
    import core.constants as cst
    # Need to also reload settings so its `from .constants import script_dir`
    # picks up the fresh function.
    monkeypatch.setattr(cst, "script_dir", lambda: str(tmp_path))
    import importlib
    import core.settings as cs
    importlib.reload(cs)
    return tmp_path, cs


class TestLoadSaveRoundtrip:
    def test_default_settings_when_file_missing(self, tmp_script_dir):
        _, cs = tmp_script_dir
        s = cs.load_settings()
        assert s["active_profile"] == "default"
        assert "default" in s["profiles"]
        assert s["custom_commands"] == []
        assert s["crash_limit"] == 3
        assert s["log_level"] == "DEBUG"
        # Schema should be at the latest version
        from core.constants import SETTINGS_SCHEMA_VERSION
        assert s["_schema_version"] == SETTINGS_SCHEMA_VERSION

    def test_save_then_reload(self, tmp_script_dir):
        _, cs = tmp_script_dir
        s = cs.load_settings()
        s["custom_commands"] = [
            {"trigger": "!hi", "response": "/say hi {player}",
             "roles": [], "enabled": True}
        ]
        s["theme_preset"] = "green"
        assert cs.save_settings(s) is True

        reloaded = cs.load_settings()
        assert reloaded["theme_preset"] == "green"
        assert reloaded["custom_commands"][0]["trigger"] == "!hi"

    def test_atomic_write_no_tmp_left_behind(self, tmp_script_dir):
        tmp_path, cs = tmp_script_dir
        s = cs.load_settings()
        cs.save_settings(s)
        assert os.path.exists(cs.settings_path())
        assert not os.path.exists(cs.settings_path() + ".tmp")


class TestMigration:
    def test_v1_to_current(self, tmp_script_dir):
        tmp_path, cs = tmp_script_dir
        # Write a flat (v1) settings file
        v1 = {
            "server_path": "/srv/vs",
            "world_folder": "/srv/vs/world",
            "ui_scale_override": 1.2,
        }
        with open(cs.settings_path(), "w") as f:
            json.dump(v1, f)
        loaded = cs.load_settings()
        assert loaded["_schema_version"] >= 4
        # Old keys should be promoted into the default profile
        assert loaded["profiles"]["default"]["server_path"] == "/srv/vs"
        # ui_scale_override stayed at top level
        assert loaded["ui_scale_override"] == 1.2

    def test_v3_to_v4_promotes_custom_commands_to_profile(self, tmp_script_dir):
        tmp_path, cs = tmp_script_dir
        v3 = {
            "_schema_version": 3,
            "active_profile": "default",
            "profiles": {"default": {}, "creative": {}},
            "custom_commands": [
                {"trigger": "!warp", "response": "/tp",
                 "roles": [], "enabled": True}
            ],
        }
        with open(cs.settings_path(), "w") as f:
            json.dump(v3, f)
        loaded = cs.load_settings()
        # Migration always lands at the latest schema, regardless of
        # how many version bumps it walks through.
        from core.constants import SETTINGS_SCHEMA_VERSION
        assert loaded["_schema_version"] == SETTINGS_SCHEMA_VERSION
        # Both profiles seeded with the original global list
        assert (loaded["profiles"]["default"]["custom_commands"][0]["trigger"]
                == "!warp")
        assert (loaded["profiles"]["creative"]["custom_commands"][0]["trigger"]
                == "!warp")

    def test_v4_to_v5_adds_player_count_poll_secs(self, tmp_script_dir):
        tmp_path, cs = tmp_script_dir
        v4 = {
            "_schema_version": 4,
            "active_profile": "default",
            "profiles": {"default": {"custom_commands": []}},
            "ui_scale_override": 1.0,
            "theme_preset": "amber",
        }
        with open(cs.settings_path(), "w") as f:
            json.dump(v4, f)
        loaded = cs.load_settings()
        # New key seeded with default (30s).
        assert loaded.get("player_count_poll_secs") == 30

    def test_pre_migration_backup_written(self, tmp_script_dir):
        tmp_path, cs = tmp_script_dir
        # Write a v3 file, load, check for backup file.
        v3 = {"_schema_version": 3, "active_profile": "default",
              "profiles": {"default": {}}}
        with open(cs.settings_path(), "w") as f:
            json.dump(v3, f)
        cs.load_settings()
        backups = [f for f in os.listdir(tmp_path) if f.endswith(".bak")]
        assert len(backups) == 1
        assert backups[0].startswith("vserverman_settings.json.v3.")

    def test_already_current_no_backup(self, tmp_script_dir):
        tmp_path, cs = tmp_script_dir
        # Write file already at the latest schema; no migration → no backup.
        from core.constants import SETTINGS_SCHEMA_VERSION
        s = {"_schema_version": SETTINGS_SCHEMA_VERSION,
             "active_profile": "default",
             "profiles": {"default": {"custom_commands": []}}}
        with open(cs.settings_path(), "w") as f:
            json.dump(s, f)
        cs.load_settings()
        backups = [f for f in os.listdir(tmp_path) if f.endswith(".bak")]
        assert backups == []

    def test_corrupt_file_falls_back_to_defaults(self, tmp_script_dir):
        tmp_path, cs = tmp_script_dir
        with open(cs.settings_path(), "w") as f:
            f.write("not valid json {{{ ")
        loaded = cs.load_settings()
        # Should NOT raise; should return defaults.
        assert loaded["active_profile"] == "default"


class TestProfiles:
    def test_get_active_profile(self, tmp_script_dir):
        _, cs = tmp_script_dir
        s = cs.load_settings()
        p = cs.get_active_profile(s)
        assert isinstance(p, dict)

    def test_get_active_profile_creates_if_missing(self, tmp_script_dir):
        _, cs = tmp_script_dir
        s = {"active_profile": "creative", "profiles": {}}
        p = cs.get_active_profile(s)
        # Creates the profile on access.
        assert "creative" in s["profiles"]

    def test_per_profile_custom_commands(self, tmp_script_dir):
        _, cs = tmp_script_dir
        s = cs.load_settings()
        s["profiles"]["creative"] = {"custom_commands": [
            {"trigger": "!gm", "response": "/gamemode 1",
             "roles": [], "enabled": True}
        ]}
        s["active_profile"] = "creative"
        assert cs.load_custom_commands(s)[0]["trigger"] == "!gm"
        s["active_profile"] = "default"
        assert cs.load_custom_commands(s) == []

    def test_save_custom_commands_writes_to_active_profile(self, tmp_script_dir):
        _, cs = tmp_script_dir
        s = cs.load_settings()
        new_rules = [
            {"trigger": "!a", "response": "/a", "roles": [],
             "enabled": True}
        ]
        cs.save_custom_commands(s, new_rules)
        assert (s["profiles"][s["active_profile"]]["custom_commands"]
                == new_rules)


# Note: TestImportExport for rules has moved to test_custom_commands.py
# since the import/export functions now live in core.custom_commands.
# The shims in core.settings are tested below to verify the back-compat
# wrappers still forward correctly.

class TestSettingsImportExportShims:
    def test_settings_shim_exports_via_engine(self, tmp_script_dir):
        _, cs = tmp_script_dir
        rules = [
            {"trigger": "!hi", "response": "/say hi",
             "roles": [], "enabled": True, "cooldown_secs": 0,
             "confirmed_destructive": False},
        ]
        # The shim should produce the same payload as the engine's
        # function would directly.
        from core.custom_commands import export_rules_to_json as direct
        assert cs.export_rules_to_json(rules) == direct(rules)

    def test_settings_shim_imports_via_engine(self, tmp_script_dir):
        _, cs = tmp_script_dir
        payload = json.dumps([{"trigger": "!hi", "response": "/say"}])
        out = cs.import_rules_from_json(payload)
        assert out[0]["trigger"] == "!hi"
