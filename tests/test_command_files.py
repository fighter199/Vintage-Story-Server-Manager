"""Tests for core.command_files — built-in + user command lists."""
import json
import os

from core.command_files import (BUILTIN_FILE, LEGACY_FILE, RETIRED_COMMANDS,
                                USER_FILE, ensure_user_file,
                                extract_user_entries, load_commands,
                                merge_commands, migrate_legacy_file,
                                read_command_file)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

BUILTIN = {
    "Server": {"/stop": {"template": "/stop", "description": "Stop."},
               "/stats": "Show stats."},
    "World": {"/time set": {"template": "/time set {h}", "description": "Set.",
                            "args": [{"name": "h", "type": "float"}]}},
}


def write(folder, name, data):
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))
    return path


class TestMerge:
    def test_builtin_only(self):
        merged = merge_commands(BUILTIN, {})
        assert list(merged) == ["Server", "World"]
        assert merged["Server"]["/stats"] == {
            "description": "Show stats.", "template": "/stats", "args": []}
        assert "_source" not in merged["Server"]["/stop"]

    def test_user_adds_replaces_and_hides(self):
        user = {"Mine": {"/hello": {"template": "/announce hi"}},
                "Anything": {"/stop": {"template": "/stop", "description": "Mine!"},
                             "/stats": None}}
        merged = merge_commands(BUILTIN, user)
        assert list(merged) == ["Server", "World", "Mine"]
        assert merged["Mine"]["/hello"]["_source"] == "user"
        # Replaced in its original category, not moved.
        assert merged["Server"]["/stop"]["description"] == "Mine!"
        assert merged["Server"]["/stop"]["_source"] == "user"
        assert "/stats" not in merged["Server"]

    def test_empty_categories_dropped(self):
        merged = merge_commands({"A": {"/x": "x"}}, {"U": {"/x": None}})
        assert merged == {}


class TestExtract:
    def test_additions_and_edits_only(self):
        legacy = {
            "Server": {"/stop": {"template": "/stop", "description": "Stop."},  # same
                       "/stats": "Different text",                              # edited
                       "/mycmd": {"template": "/mycmd"}},                       # added
            "Land Claiming": {"/land list": {"template": "/land list"}},        # retired
        }
        assert extract_user_entries(legacy, BUILTIN) == {
            "Server": {"/stats": "Different text", "/mycmd": {"template": "/mycmd"}}}

    def test_retired_list_matches_what_was_removed(self):
        builtin = read_command_file(os.path.join(ROOT, BUILTIN_FILE))
        shipped = {n for cmds in builtin.values() for n in cmds}
        assert not shipped & RETIRED_COMMANDS
        assert len(RETIRED_COMMANDS) == 25


class TestMigration:
    def test_moves_custom_commands_and_renames_legacy(self, tmp_path):
        d = str(tmp_path)
        write(d, BUILTIN_FILE, BUILTIN)
        legacy = dict(BUILTIN)
        legacy["Mine"] = {"/warp": {"template": "/tp {player} 0 120 0"}}
        legacy["Waypoints"] = {"/waypoint add": {"template": "/waypoint add"}}
        write(d, LEGACY_FILE, legacy)
        assert migrate_legacy_file(d) == 1
        assert not os.path.exists(os.path.join(d, LEGACY_FILE))
        assert os.path.exists(os.path.join(d, LEGACY_FILE + ".old"))
        user = read_command_file(os.path.join(d, USER_FILE))
        assert user == {"Mine": {"/warp": {"template": "/tp {player} 0 120 0"}}}
        merged, problems = load_commands(d)
        assert problems == []
        assert merged["Mine"]["/warp"]["_source"] == "user"

    def test_never_overwrites_existing_user_entries(self, tmp_path):
        d = str(tmp_path)
        write(d, BUILTIN_FILE, BUILTIN)
        write(d, USER_FILE, {"Mine": {"/warp": {"template": "/NEWER"}}})
        write(d, LEGACY_FILE, {"Mine": {"/warp": {"template": "/older"},
                                        "/home": {"template": "/home"}}})
        assert migrate_legacy_file(d) == 1
        user = read_command_file(os.path.join(d, USER_FILE))
        assert user["Mine"] == {"/warp": {"template": "/NEWER"},
                                "/home": {"template": "/home"}}

    def test_unreadable_user_file_is_left_alone(self, tmp_path):
        d = str(tmp_path)
        write(d, BUILTIN_FILE, BUILTIN)
        bad = write(d, USER_FILE, "{ this is not json")
        write(d, LEGACY_FILE, {"Mine": {"/x": "x"}})
        assert migrate_legacy_file(d) == 0
        assert open(bad).read() == "{ this is not json"
        assert os.path.exists(os.path.join(d, LEGACY_FILE))   # retry later

    def test_unmodified_legacy_just_renamed(self, tmp_path):
        d = str(tmp_path)
        write(d, BUILTIN_FILE, BUILTIN)
        write(d, LEGACY_FILE, BUILTIN)
        assert migrate_legacy_file(d) == 0
        assert not os.path.exists(os.path.join(d, USER_FILE))
        assert os.path.exists(os.path.join(d, LEGACY_FILE + ".old"))


class TestLoad:
    def test_user_file_error_reported_not_fatal(self, tmp_path):
        d = str(tmp_path)
        write(d, BUILTIN_FILE, BUILTIN)
        write(d, USER_FILE, '{"Mine": {"/x": }}')
        merged, problems = load_commands(d)
        assert "/stop" in merged["Server"]
        assert len(problems) == 1 and USER_FILE in problems[0]

    def test_json5_comments_and_trailing_commas(self, tmp_path):
        d = str(tmp_path)
        write(d, BUILTIN_FILE, BUILTIN)
        write(d, USER_FILE, '{\n // my stuff\n "Mine": {"/x": "An x",},\n}')
        merged, problems = load_commands(d)
        assert problems == [] and merged["Mine"]["/x"]["description"] == "An x"

    def test_template_file_loads(self, tmp_path):
        d = str(tmp_path)
        write(d, BUILTIN_FILE, BUILTIN)
        path = ensure_user_file(d)
        assert ensure_user_file(d) == path          # doesn't overwrite
        merged, problems = load_commands(d)
        assert problems == []
        assert merged["My commands"]["/example"]["_source"] == "user"

    def test_shipped_builtin_file_is_valid(self):
        builtin = read_command_file(os.path.join(ROOT, BUILTIN_FILE))
        merged = merge_commands(builtin, {})
        assert sum(len(v) for v in merged.values()) == 84
        tp = next(c["/tp (player to player)"] for c in merged.values()
                  if "/tp (player to player)" in c)
        assert tp["template"] == "/tp {player} {destination}"
