"""Tests for core.settings_transfer — settings export/import."""
import json
import os

import pytest

from core import settings_transfer as st


def _settings():
    return {
        "active_profile": "Main",
        "profiles": {
            "Main": {"server_path": "/srv/vs", "custom_commands": [{"trigger": "!a"}],
                     "autorun_rules": [], "player_totals": {"Alice": 60}},
            "Test": {"server_path": "/srv/test"},
        },
        "theme_preset": "green", "ui_scale_override": 1.2, "crash_limit": 5,
        "window_layout": {"geometry": "800x600+0+0"},
    }


class TestExport:
    def test_bundle_contents(self):
        b = st.export_bundle(_settings(), {"My commands": {"/x": {"template": "/x"}}})
        assert b["format"] == st.EXPORT_FORMAT and b["version"] == 1
        assert set(b["profiles"]) == {"Main", "Test"}
        assert b["profiles"]["Main"]["player_totals"] == {"Alice": 60}
        assert b["preferences"] == {"theme_preset": "green",
                                    "ui_scale_override": 1.2, "crash_limit": 5}
        assert "window_layout" not in json.dumps(b)
        assert b["user_commands"]["My commands"]["/x"]["template"] == "/x"

    def test_round_trip_through_file(self, tmp_path):
        path = os.path.join(str(tmp_path), "vssm.json")
        st.write_bundle(path, st.export_bundle(_settings()))
        assert st.read_bundle(path)["profiles"]["Test"]["server_path"] == "/srv/test"
        assert not os.path.exists(path + ".tmp")


class TestRead:
    def _write(self, tmp_path, data):
        path = os.path.join(str(tmp_path), "f.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def test_rejects_other_files(self, tmp_path):
        for data in ("not json", {"profiles": {}}, [1, 2],
                     {"format": "vssm-settings", "version": 1, "profiles": []}):
            with pytest.raises(st.TransferError):
                st.read_bundle(self._write(tmp_path, data))

    def test_rejects_newer_format(self, tmp_path):
        path = self._write(tmp_path, {"format": "vssm-settings", "version": 99,
                                      "app_version": "9.0", "profiles": {}})
        with pytest.raises(st.TransferError, match="newer VSSM"):
            st.read_bundle(path)


class TestImport:
    def _bundle(self):
        return st.export_bundle(_settings())

    def test_into_empty_settings(self):
        mine = {"active_profile": "default", "profiles": {"default": {}}}
        done = st.import_profiles(mine, self._bundle(), replace=False)
        assert [d[2] for d in done] == ["added", "added"]
        assert list(mine["profiles"]) == ["default", "Main", "Test"]
        assert mine["profiles"]["Main"]["server_path"] == "/srv/vs"

    def test_clash_keep_both(self):
        mine = {"active_profile": "main", "profiles": {"main": {"server_path": "mine"}}}
        assert st.clashing_profiles(mine, self._bundle()) == ["Main"]
        done = st.import_profiles(mine, self._bundle(), replace=False)
        assert done[0] == ("Main", "Main (imported)", "renamed")
        assert mine["profiles"]["main"]["server_path"] == "mine"
        assert mine["profiles"]["Main (imported)"]["server_path"] == "/srv/vs"
        st.import_profiles(mine, self._bundle(), replace=False)
        assert "Main (imported 2)" in mine["profiles"]

    def test_clash_replace_keeps_local_name(self):
        mine = {"active_profile": "main", "profiles": {"main": {"server_path": "mine"}}}
        done = st.import_profiles(mine, self._bundle(), replace=True)
        assert done[0] == ("Main", "main", "replaced")
        assert mine["profiles"]["main"]["server_path"] == "/srv/vs"
        assert list(mine["profiles"]) == ["main", "Test"]

    def test_imported_data_is_a_copy(self):
        bundle = self._bundle()
        mine = {"profiles": {}}
        st.import_profiles(mine, bundle, replace=False)
        mine["profiles"]["Main"]["custom_commands"].append({"trigger": "!b"})
        assert len(bundle["profiles"]["Main"]["custom_commands"]) == 1

    def test_preferences(self):
        mine = {"theme_preset": "amber"}
        bundle = self._bundle()
        bundle["preferences"]["window_layout"] = {"evil": 1}
        applied = st.import_preferences(mine, bundle)
        assert mine["theme_preset"] == "green" and "window_layout" not in mine
        assert set(applied) == {"theme_preset", "ui_scale_override", "crash_limit"}

    def test_merge_user_commands(self):
        mine = {"_note": "mine", "A": {"/a": {"template": "/a"}, "/b": {"template": "/b"}}}
        theirs = {"_note": "theirs", "A": {"/b": {"template": "/b2"}},
                  "B": {"/c": {"template": "/c"}}}
        merged, n = st.merge_user_commands(mine, theirs)
        assert n == 2 and merged["_note"] == "mine"
        assert merged["A"]["/a"]["template"] == "/a"
        assert merged["A"]["/b"]["template"] == "/b2"
        assert merged["B"]["/c"]["template"] == "/c"
