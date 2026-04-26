"""Tests for core.custom_commands — engine, validation, and helpers."""
import pytest

from core.custom_commands import (
    AuditRecord,
    ChatCommandDispatcher,
    DESTRUCTIVE_KEYWORDS,
    _contains_destructive,
    _expand_response,
    _extract_args,
    _matches_trigger,
    make_empty_rule,
    normalize_rule,
    validate_rule,
)


# ----------------------------------------------------------------------
# Trigger matching + argument extraction
# ----------------------------------------------------------------------
class TestExtractArgs:
    def test_match_no_args(self):
        assert _extract_args("!warp", "!warp") == []

    def test_match_with_args(self):
        assert _extract_args("!warp spawn", "!warp") == ["spawn"]
        assert _extract_args("!give Bob flint 5", "!give") == \
            ["Bob", "flint", "5"]

    def test_case_insensitive(self):
        assert _extract_args("!WARP spawn", "!warp") == ["spawn"]
        assert _extract_args("!warp SPAWN", "!WARP") == ["SPAWN"]

    def test_no_match_substring(self):
        assert _extract_args("!warpzone", "!warp") is None

    def test_no_match_not_at_start(self):
        assert _extract_args("hello !warp", "!warp") is None

    def test_no_match_completely_different(self):
        assert _extract_args("hello world", "!warp") is None

    def test_extra_whitespace(self):
        assert _extract_args("!warp    spawn  bar", "!warp") == \
            ["spawn", "bar"]

    def test_trigger_with_special_chars(self):
        # The trigger is regex-escaped, so special chars are literal.
        assert _extract_args("!a.b foo", "!a.b") == ["foo"]
        assert _extract_args("!axb foo", "!a.b") is None


class TestExpandResponse:
    def test_player_substitution(self):
        out = _expand_response("/say hi {player}", "Steve", "guest", [])
        assert out == ["/say hi Steve"]

    def test_role_substitution(self):
        out = _expand_response("/grant {role}", "Steve", "admin", [])
        assert out == ["/grant admin"]

    def test_positional_substitution(self):
        out = _expand_response("/give {1} {2}", "Steve", "admin",
                                ["Bob", "flint"])
        assert out == ["/give Bob flint"]

    def test_target_alias(self):
        out = _expand_response("/tp {target}", "Steve", "admin",
                                ["Alice"])
        assert out == ["/tp Alice"]

    def test_args_join(self):
        out = _expand_response("/say {args}", "Steve", "admin",
                                ["hello", "world"])
        assert out == ["/say hello world"]

    def test_missing_arg_drops_line(self):
        # A line that references {2} when only {1} is provided is dropped
        # entirely — but other lines stay.
        out = _expand_response(
            "/give {1}\n/give {1} {2}",
            "Steve", "admin", ["Bob"])
        assert out == ["/give Bob"]

    def test_blank_lines_skipped(self):
        out = _expand_response("/a\n\n/b\n   \n/c", "Steve", "admin", [])
        assert out == ["/a", "/b", "/c"]

    def test_multiple_substitutions_one_line(self):
        out = _expand_response("/tell {player} you are {role}",
                                "Steve", "admin", [])
        assert out == ["/tell Steve you are admin"]


# ----------------------------------------------------------------------
# Dispatcher behaviour
# ----------------------------------------------------------------------
class TestChatCommandDispatcher:
    def _make(self, rules, **kwargs):
        return ChatCommandDispatcher(lambda: rules, **kwargs)

    def test_anyone_can_trigger_blank_roles(self):
        rules = [{"trigger": "!hi", "response": "/say hi {player}",
                  "roles": [], "enabled": True, "cooldown_secs": 0}]
        d = self._make(rules)
        assert d.dispatch("Steve", "guest", "!hi") == ["/say hi Steve"]

    def test_role_gating(self):
        rules = [{"trigger": "!day", "response": "/time set day",
                  "roles": ["admin"], "enabled": True, "cooldown_secs": 0}]
        d = self._make(rules)
        assert d.dispatch("Carol", "admin", "!day") == ["/time set day"]
        assert d.dispatch("Bob", "guest", "!day") == []

    def test_disabled_rule_skipped(self):
        rules = [{"trigger": "!hi", "response": "/say hi",
                  "roles": [], "enabled": False, "cooldown_secs": 0}]
        d = self._make(rules)
        assert d.dispatch("Steve", "guest", "!hi") == []

    def test_no_match(self):
        rules = [{"trigger": "!hi", "response": "/say hi",
                  "roles": [], "enabled": True, "cooldown_secs": 0}]
        d = self._make(rules)
        assert d.dispatch("Steve", "guest", "hello world") == []

    def test_multi_line_response(self):
        rules = [{"trigger": "!combo",
                  "response": "/say one\n/say two\n/say three",
                  "roles": [], "enabled": True, "cooldown_secs": 0}]
        d = self._make(rules)
        assert d.dispatch("Steve", "guest", "!combo") == \
            ["/say one", "/say two", "/say three"]

    def test_argument_capture_via_dispatch(self):
        rules = [{"trigger": "!give", "response": "/give {1} {2}",
                  "roles": [], "enabled": True, "cooldown_secs": 0}]
        d = self._make(rules)
        assert d.dispatch("Steve", "admin", "!give flint 5") == \
            ["/give flint 5"]

    def test_multiple_rules_can_fire(self):
        rules = [
            {"trigger": "!hi", "response": "/say a",
             "roles": [], "enabled": True, "cooldown_secs": 0},
            {"trigger": "!hi", "response": "/say b",
             "roles": [], "enabled": True, "cooldown_secs": 0},
        ]
        d = self._make(rules)
        assert d.dispatch("Steve", "guest", "!hi") == ["/say a", "/say b"]


class TestCooldown:
    def _setup(self):
        clock_value = [1000.0]
        clock = lambda: clock_value[0]
        rules = [{"trigger": "!warp", "response": "/tp {player}",
                  "roles": [], "enabled": True, "cooldown_secs": 30}]
        d = ChatCommandDispatcher(lambda: rules, clock=clock)
        return d, clock_value

    def test_first_call_fires(self):
        d, _ = self._setup()
        assert d.dispatch("Steve", "guest", "!warp") == ["/tp Steve"]

    def test_second_call_blocked_within_window(self):
        d, _ = self._setup()
        d.dispatch("Steve", "guest", "!warp")
        assert d.dispatch("Steve", "guest", "!warp") == []

    def test_call_after_window_fires(self):
        d, t = self._setup()
        d.dispatch("Steve", "guest", "!warp")
        t[0] += 31
        assert d.dispatch("Steve", "guest", "!warp") == ["/tp Steve"]

    def test_cooldown_is_per_player(self):
        d, _ = self._setup()
        d.dispatch("Steve", "guest", "!warp")
        # Different player can fire immediately.
        assert d.dispatch("Alice", "guest", "!warp") == ["/tp Alice"]

    def test_cooldown_is_per_rule(self):
        clock_value = [1000.0]
        rules = [
            {"trigger": "!warp", "response": "/tp",
             "roles": [], "enabled": True, "cooldown_secs": 30},
            {"trigger": "!home", "response": "/home",
             "roles": [], "enabled": True, "cooldown_secs": 30},
        ]
        d = ChatCommandDispatcher(lambda: rules,
                                    clock=lambda: clock_value[0])
        d.dispatch("Steve", "guest", "!warp")
        assert d.dispatch("Steve", "guest", "!home") == ["/home"]

    def test_reset_cooldowns(self):
        d, _ = self._setup()
        d.dispatch("Steve", "guest", "!warp")
        d.reset_cooldowns()
        assert d.dispatch("Steve", "guest", "!warp") == ["/tp Steve"]


class TestDestructiveGuard:
    def test_unconfirmed_destructive_blocked(self):
        rules = [{"trigger": "!off", "response": "/stop",
                  "roles": ["admin"], "enabled": True,
                  "cooldown_secs": 0,
                  "confirmed_destructive": False}]
        d = ChatCommandDispatcher(lambda: rules)
        assert d.dispatch("Carol", "admin", "!off") == []

    def test_confirmed_destructive_fires(self):
        rules = [{"trigger": "!off", "response": "/stop",
                  "roles": ["admin"], "enabled": True,
                  "cooldown_secs": 0,
                  "confirmed_destructive": True}]
        d = ChatCommandDispatcher(lambda: rules)
        assert d.dispatch("Carol", "admin", "!off") == ["/stop"]

    def test_non_destructive_does_not_require_flag(self):
        rules = [{"trigger": "!hi", "response": "/say hi",
                  "roles": [], "enabled": True,
                  "cooldown_secs": 0,
                  "confirmed_destructive": False}]
        d = ChatCommandDispatcher(lambda: rules)
        assert d.dispatch("Steve", "guest", "!hi") == ["/say hi"]

    def test_destructive_keywords_are_substring(self):
        # We do a substring check, so /role with trailing space is needed
        # to avoid /role-something matching when intended.
        assert _contains_destructive("/stop now")
        assert _contains_destructive("/op {player}")
        assert not _contains_destructive("/say role admin")  # "role " with space


class TestAuditListener:
    def test_listener_called_on_fire(self):
        records = []
        rules = [{"trigger": "!hi", "response": "/say hi",
                  "roles": [], "enabled": True, "cooldown_secs": 0}]
        d = ChatCommandDispatcher(lambda: rules,
                                    audit_listener=records.append)
        d.dispatch("Steve", "guest", "!hi")
        assert len(records) == 1
        assert records[0].player == "Steve"
        assert records[0].skipped_reason is None
        assert records[0].commands == ["/say hi"]

    def test_listener_called_on_role_skip(self):
        records = []
        rules = [{"trigger": "!day", "response": "/time set day",
                  "roles": ["admin"], "enabled": True, "cooldown_secs": 0}]
        d = ChatCommandDispatcher(lambda: rules,
                                    audit_listener=records.append)
        d.dispatch("Bob", "guest", "!day")
        assert records[0].skipped_reason == "role"

    def test_listener_called_on_cooldown(self):
        records = []
        clock_value = [1000.0]
        rules = [{"trigger": "!warp", "response": "/tp",
                  "roles": [], "enabled": True, "cooldown_secs": 30}]
        d = ChatCommandDispatcher(
            lambda: rules,
            audit_listener=records.append,
            clock=lambda: clock_value[0],
        )
        d.dispatch("Steve", "guest", "!warp")
        d.dispatch("Steve", "guest", "!warp")
        assert records[1].skipped_reason == "cooldown"
        assert records[1].cooldown_remaining > 0

    def test_listener_called_on_destructive(self):
        records = []
        rules = [{"trigger": "!off", "response": "/stop",
                  "roles": [], "enabled": True, "cooldown_secs": 0}]
        d = ChatCommandDispatcher(lambda: rules,
                                    audit_listener=records.append)
        d.dispatch("Carol", "admin", "!off")
        assert records[0].skipped_reason == "unconfirmed_destructive"

    def test_listener_exception_doesnt_break_dispatch(self):
        def bad(_):
            raise RuntimeError("boom")
        rules = [{"trigger": "!hi", "response": "/say hi",
                  "roles": [], "enabled": True, "cooldown_secs": 0}]
        d = ChatCommandDispatcher(lambda: rules, audit_listener=bad)
        # Should NOT raise.
        assert d.dispatch("Steve", "guest", "!hi") == ["/say hi"]

    def test_set_audit_listener_late(self):
        records = []
        rules = [{"trigger": "!hi", "response": "/say hi",
                  "roles": [], "enabled": True, "cooldown_secs": 0}]
        d = ChatCommandDispatcher(lambda: rules)
        d.set_audit_listener(records.append)
        d.dispatch("Steve", "guest", "!hi")
        assert len(records) == 1


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
class TestValidateRule:
    def test_valid_rule(self):
        rule = {"trigger": "!hi", "response": "/say hi",
                "roles": [], "enabled": True, "cooldown_secs": 0}
        assert validate_rule(rule) == []

    def test_empty_trigger(self):
        errs = validate_rule({"trigger": "", "response": "/say"})
        assert any("Trigger cannot be empty" in e for e in errs)

    def test_empty_response(self):
        errs = validate_rule({"trigger": "!hi", "response": ""})
        assert any("Response" in e for e in errs)

    def test_trigger_with_spaces(self):
        errs = validate_rule({"trigger": "!has spaces", "response": "/x"})
        assert any("spaces" in e for e in errs)

    def test_destructive_unconfirmed_fails(self):
        errs = validate_rule({"trigger": "!off", "response": "/stop"})
        assert any("destructive" in e for e in errs)

    def test_destructive_confirmed_passes(self):
        errs = validate_rule({"trigger": "!off", "response": "/stop",
                              "confirmed_destructive": True})
        assert errs == []

    def test_negative_cooldown(self):
        errs = validate_rule({"trigger": "!hi", "response": "/say",
                              "cooldown_secs": -5})
        assert any("Cooldown" in e for e in errs)

    def test_non_numeric_cooldown(self):
        errs = validate_rule({"trigger": "!hi", "response": "/say",
                              "cooldown_secs": "abc"})
        assert any("Cooldown" in e for e in errs)

    def test_roles_must_be_list(self):
        errs = validate_rule({"trigger": "!hi", "response": "/say",
                              "roles": "admin"})
        assert any("roles" in e for e in errs)


class TestNormalizeRule:
    def test_fills_missing_fields(self):
        rule = {"trigger": "!hi", "response": "/say"}
        normalize_rule(rule)
        assert rule["roles"] == []
        assert rule["enabled"] is True
        assert rule["cooldown_secs"] == 0
        assert rule["confirmed_destructive"] is False

    def test_does_not_overwrite_existing(self):
        rule = {"trigger": "!hi", "response": "/say",
                "cooldown_secs": 30, "enabled": False}
        normalize_rule(rule)
        assert rule["cooldown_secs"] == 30
        assert rule["enabled"] is False


class TestMakeEmptyRule:
    def test_has_all_fields(self):
        rule = make_empty_rule()
        assert "trigger" in rule
        assert "response" in rule
        assert "roles" in rule
        assert "enabled" in rule
        assert "cooldown_secs" in rule
        assert "confirmed_destructive" in rule

    def test_is_valid_after_filling(self):
        rule = make_empty_rule()
        rule["trigger"] = "!hi"
        rule["response"] = "/say hi"
        assert validate_rule(rule) == []


# ----------------------------------------------------------------------
# Import / export
# ----------------------------------------------------------------------
class TestExportRulesToJson:
    def test_basic_payload_shape(self):
        from core.custom_commands import (
            export_rules_to_json, EXPORT_WRAPPER_KEY, EXPORT_WRAPPER_VERSION)
        import json as _json
        rules = [{"trigger": "!hi", "response": "/say",
                  "roles": [], "enabled": True}]
        payload = export_rules_to_json(rules)
        data = _json.loads(payload)
        assert data[EXPORT_WRAPPER_KEY] == EXPORT_WRAPPER_VERSION
        assert isinstance(data["rules"], list)
        assert data["rules"][0]["trigger"] == "!hi"

    def test_pretty_printed(self):
        from core.custom_commands import export_rules_to_json
        payload = export_rules_to_json([])
        assert "\n" in payload  # indented JSON has newlines

    def test_export_only_contains_rules_no_settings(self):
        # The whole point of moving this to a separate module: the export
        # MUST NOT include any settings fields, even by accident.
        from core.custom_commands import export_rules_to_json
        import json as _json
        rules = [{"trigger": "!hi", "response": "/say",
                  "roles": [], "enabled": True}]
        data = _json.loads(export_rules_to_json(rules))
        # Anything that smells like settings is forbidden.
        forbidden = {"active_profile", "profiles", "ui_scale_override",
                     "theme_preset", "crash_limit", "crash_window_secs",
                     "log_level", "player_count_poll_secs",
                     "_schema_version", "server_path", "world_folder",
                     "mods_folder", "backup_dir"}
        leaked = set(data.keys()) & forbidden
        assert not leaked, f"Settings keys leaked into export: {leaked}"

    def test_export_normalises_partial_rules(self):
        # An exported file should always have all fields populated, even
        # if the in-memory rule was missing some.
        from core.custom_commands import export_rules_to_json
        import json as _json
        partial = [{"trigger": "!hi", "response": "/say"}]
        data = _json.loads(export_rules_to_json(partial))
        rule = data["rules"][0]
        assert rule["roles"] == []
        assert rule["enabled"] is True
        assert rule["cooldown_secs"] == 0
        assert rule["confirmed_destructive"] is False

    def test_export_filters_non_dict_entries(self):
        from core.custom_commands import export_rules_to_json
        import json as _json
        # If junk creeps into the rules list, drop it instead of crashing.
        bad = [None, "string", 42, {"trigger": "!hi", "response": "/say"}]
        data = _json.loads(export_rules_to_json(bad))
        assert len(data["rules"]) == 1
        assert data["rules"][0]["trigger"] == "!hi"


class TestImportRulesFromJson:
    def test_round_trip(self):
        from core.custom_commands import (
            export_rules_to_json, import_rules_from_json)
        rules = [
            {"trigger": "!hi", "response": "/say hi {player}",
             "roles": [], "enabled": True, "cooldown_secs": 5,
             "confirmed_destructive": False},
        ]
        out = import_rules_from_json(export_rules_to_json(rules))
        assert out[0]["trigger"] == "!hi"
        assert out[0]["cooldown_secs"] == 5

    def test_accepts_bare_list(self):
        from core.custom_commands import import_rules_from_json
        import json as _json
        out = import_rules_from_json(
            _json.dumps([{"trigger": "!hi", "response": "/say"}]))
        assert out[0]["trigger"] == "!hi"
        # normalize_rule fills defaults
        assert out[0]["enabled"] is True
        assert out[0]["roles"] == []

    def test_accepts_legacy_wrapper_key(self):
        # Files exported by v3.1 used `vserverman_rules_version`; we
        # still accept those on import for back-compat.
        from core.custom_commands import import_rules_from_json
        import json as _json
        legacy = _json.dumps({
            "vserverman_rules_version": 1,
            "rules": [{"trigger": "!hi", "response": "/say"}],
        })
        out = import_rules_from_json(legacy)
        assert out[0]["trigger"] == "!hi"

    def test_rejects_settings_dump(self):
        # If the user accidentally points the import dialog at their
        # full settings file, it should NOT silently strip out any
        # rules field that happens to be there. Bail with a clear
        # error instead.
        from core.custom_commands import import_rules_from_json
        import json as _json
        settings_dump = _json.dumps({
            "_schema_version": 5,
            "active_profile": "default",
            "profiles": {"default": {}},
            "crash_limit": 3,
            "rules": [{"trigger": "!hi", "response": "/say"}],
        })
        try:
            import_rules_from_json(settings_dump)
        except ValueError as e:
            assert "settings" in str(e).lower()
        else:
            raise AssertionError(
                "Expected ValueError for a settings-shaped payload")

    def test_rejects_invalid_rules(self):
        from core.custom_commands import import_rules_from_json
        import json as _json
        try:
            import_rules_from_json(
                _json.dumps([{"trigger": "", "response": ""}]))
        except ValueError:
            return
        raise AssertionError("Expected ValueError for empty trigger")

    def test_rejects_destructive_unconfirmed(self):
        from core.custom_commands import import_rules_from_json
        import json as _json
        try:
            import_rules_from_json(
                _json.dumps([{"trigger": "!off", "response": "/stop"}]))
        except ValueError as e:
            assert "destructive" in str(e).lower()
        else:
            raise AssertionError("Expected ValueError for unconfirmed /stop")

    def test_rejects_malformed_json(self):
        from core.custom_commands import import_rules_from_json
        try:
            import_rules_from_json("not valid json {{{")
        except ValueError:
            return
        raise AssertionError("Expected ValueError for malformed JSON")

    def test_rejects_wrong_shape(self):
        from core.custom_commands import import_rules_from_json
        for bad in ('"just a string"', "42", "null", "true"):
            try:
                import_rules_from_json(bad)
            except ValueError:
                continue
            raise AssertionError(f"Expected ValueError for {bad!r}")

    def test_rejects_non_dict_rule(self):
        from core.custom_commands import import_rules_from_json
        import json as _json
        try:
            import_rules_from_json(_json.dumps(["not a dict"]))
        except ValueError as e:
            assert "Rule 0" in str(e)
        else:
            raise AssertionError("Expected ValueError for non-dict rule")
