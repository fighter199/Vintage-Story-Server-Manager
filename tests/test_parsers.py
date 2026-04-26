"""Tests for core.parsers."""
from datetime import datetime, timedelta

import pytest

from core.parsers import (
    classify_line,
    parse_chat_message,
    parse_player_event,
    parse_role_response,
    parse_json5_ish,
    parse_cron_expr,
    seconds_until_next,
    split_client_list,
    strip_log_prefix,
    version_is_newer,
)


class TestClassifyLine:
    def test_error_bracketed(self):
        assert classify_line("[ERROR] something broke") == "error"
        assert classify_line("[fatal] crash") == "error"
        assert classify_line("[Exception] boom") == "error"

    def test_warn_bracketed(self):
        assert classify_line("[Warning] mild") == "warn"
        assert classify_line("[warn] also") == "warn"

    def test_chat_bracketed(self):
        assert classify_line("[Server Chat] <Steve> hi") == "chat"
        assert classify_line("[CHAT] <bob>") == "chat"

    def test_chat_anchored_lt(self):
        assert classify_line("<alice> said hello") == "chat"

    def test_player_join_leave(self):
        assert classify_line("Steve joined the server") == "player"
        assert classify_line("Steve joins.") == "player"
        assert classify_line("Player Steve left.") == "player"

    def test_success_keywords(self):
        assert classify_line("Server ready.") == "success"
        assert classify_line("World saved.") == "success"

    def test_default_info(self):
        assert classify_line("Some random message") == "info"
        assert classify_line("") == "info"

    def test_bracketed_server_error_falls_to_info(self):
        # Documents current behavior — `[Server Error]` is NOT recognised
        # because the regex matches `[ERROR]` only. Pinned so any change
        # is intentional.
        assert classify_line("[Server Error] thing") == "info"

    def test_inline_error_keyword(self):
        assert classify_line("foo error: bar") == "error"
        # NOTE: "KeyError:" without a leading space doesn't match the
        # `" error:"` pattern (current behaviour) — but "Exception:" does.
        assert classify_line("SomeException: missing") == "error"


class TestParseChatMessage:
    def test_server_chat_prefix(self):
        assert parse_chat_message("[Server Chat] <Steve> !warp spawn") == \
            ("Steve", "!warp spawn")

    def test_chat_prefix(self):
        assert parse_chat_message("[Chat] <alice> hi") == ("alice", "hi")

    def test_bare_angle(self):
        assert parse_chat_message("<charlie> ok") == ("charlie", "ok")

    def test_no_chat_returns_none(self):
        assert parse_chat_message("Server started.") == (None, None)
        assert parse_chat_message("Steve [127.0.0.1]:42 joins.") == \
            (None, None)

    def test_empty_message(self):
        assert parse_chat_message("<bob>") == ("bob", "")

    def test_with_timestamp_prefix(self):
        line = "12.04.2026 11:23:45 [Server Chat] <Dave> howdy"
        assert parse_chat_message(line) == ("Dave", "howdy")

    def test_player_with_special_chars(self):
        assert parse_chat_message("<some-user.42> msg") == \
            ("some-user.42", "msg")


class TestParsePlayerEvent:
    def test_join_with_ip(self):
        assert parse_player_event("Steve [127.0.0.1]:42 joins.") == \
            ("join", "Steve")

    def test_join_quoted(self):
        line = "Player 'Steve' has joined the game"
        assert parse_player_event(line) == ("join", "Steve")

    def test_audit_join(self):
        assert parse_player_event("[Audit] Steve joined") == \
            ("join", "Steve")

    def test_leave_player_x_left(self):
        assert parse_player_event("Player Steve left.") == \
            ("leave", "Steve")

    def test_leave_quoted(self):
        line = "Player 'Steve' has left the game"
        assert parse_player_event(line) == ("leave", "Steve")

    def test_audit_disconnect(self):
        line = "[Audit] Client Steve disconnected"
        assert parse_player_event(line) == ("leave", "Steve")

    def test_no_match(self):
        assert parse_player_event("Server started.") == (None, None)

    def test_list_clients(self):
        assert parse_player_event("Connected players: alice, bob, charlie") \
            == ("list", "alice, bob, charlie")


class TestSplitClientList:
    def test_comma_separated(self):
        assert split_client_list("alice, bob, charlie") == \
            ["alice", "bob", "charlie"]

    def test_semicolon_separated(self):
        assert split_client_list("alice; bob; charlie") == \
            ["alice", "bob", "charlie"]

    def test_double_space_separated(self):
        assert split_client_list("alice  bob  charlie") == \
            ["alice", "bob", "charlie"]

    def test_empty_or_none_marker(self):
        assert split_client_list("") == []
        assert split_client_list("none") == []
        assert split_client_list("no one") == []
        assert split_client_list("-") == []

    def test_filters_short_garbage(self):
        # Single-letter "names" are filtered out.
        assert split_client_list("a, ok, b") == ["ok"]

    def test_filters_paren_groups(self):
        assert split_client_list("alice, (offline)") == ["alice"]


class TestParseRoleResponse:
    def test_basic(self):
        assert parse_role_response("Player has role admin") == "admin"

    def test_lowercase_normalisation(self):
        assert parse_role_response("Player has role SuPlayer") == "suplayer"

    def test_no_match(self):
        assert parse_role_response("Steve joined") is None


class TestParseJson5Ish:
    def test_plain_json(self):
        assert parse_json5_ish('{"a": 1, "b": 2}') == {"a": 1, "b": 2}

    def test_strips_line_comments(self):
        text = '''{"a": 1, // a comment
        "b": 2}'''
        assert parse_json5_ish(text) == {"a": 1, "b": 2}

    def test_strips_block_comments(self):
        text = '{"a": 1, /* block */ "b": 2}'
        assert parse_json5_ish(text) == {"a": 1, "b": 2}

    def test_trailing_commas_allowed(self):
        assert parse_json5_ish('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}
        assert parse_json5_ish('[1, 2, 3,]') == [1, 2, 3]

    def test_single_quoted_strings(self):
        assert parse_json5_ish("{'a': 'hello'}") == {"a": "hello"}


class TestParseCronExpr:
    def test_simple_time(self):
        assert parse_cron_expr("06:00") == [(None, 6, 0)]

    def test_with_weekday(self):
        assert parse_cron_expr("mon 18:30") == [(0, 18, 30)]

    def test_comma_separated(self):
        result = parse_cron_expr("06:00, 18:00")
        assert result == [(None, 6, 0), (None, 18, 0)]

    def test_mixed_with_weekday(self):
        result = parse_cron_expr("mon 06:00; fri 18:30")
        assert result == [(0, 6, 0), (4, 18, 30)]

    def test_invalid_time(self):
        with pytest.raises(ValueError):
            parse_cron_expr("not a time")

    def test_invalid_weekday(self):
        with pytest.raises(ValueError):
            parse_cron_expr("xyz 06:00")

    def test_out_of_range(self):
        with pytest.raises(ValueError):
            parse_cron_expr("25:00")
        with pytest.raises(ValueError):
            parse_cron_expr("06:99")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_cron_expr("")
        with pytest.raises(ValueError):
            parse_cron_expr(",,")


class TestSecondsUntilNext:
    def test_later_today(self):
        now = datetime(2026, 4, 25, 5, 0)  # 5 AM
        entries = [(None, 6, 0)]            # 6 AM today
        assert seconds_until_next(entries, now=now) == 3600

    def test_tomorrow(self):
        now = datetime(2026, 4, 25, 7, 0)
        entries = [(None, 6, 0)]
        # Should be ~23 hours away (tomorrow's 6 AM)
        assert 23 * 3600 - 1 <= seconds_until_next(entries, now=now) \
            <= 23 * 3600 + 1

    def test_specific_weekday(self):
        # 25 Apr 2026 is a Saturday (weekday 5).
        now = datetime(2026, 4, 25, 12, 0)  # noon Sat
        # Next Monday at 9:00 AM is 45 hours away.
        entries = [(0, 9, 0)]
        secs = seconds_until_next(entries, now=now)
        expected = 45 * 3600
        assert abs(secs - expected) < 60


class TestStripLogPrefix:
    def test_timestamp_strip(self):
        assert strip_log_prefix("12.04.2026 11:23:45 hello") == "hello"

    def test_log_prefix_strip(self):
        assert strip_log_prefix("[Server Notification] hello") == "hello"

    def test_combined(self):
        assert strip_log_prefix(
            "12.04.2026 11:23:45 [Server Notification] hello") == "hello"

    def test_unaffected(self):
        assert strip_log_prefix("hello world") == "hello world"


class TestVersionIsNewer:
    def test_strictly_newer_major(self):
        assert version_is_newer("2.0.0", "1.9.9") is True

    def test_strictly_newer_minor(self):
        assert version_is_newer("1.2.0", "1.1.9") is True

    def test_strictly_newer_patch(self):
        assert version_is_newer("1.0.1", "1.0.0") is True

    def test_equal_is_false(self):
        assert version_is_newer("1.0.0", "1.0.0") is False

    def test_older_is_false(self):
        assert version_is_newer("1.0.0", "1.0.1") is False

    def test_missing_local_means_yes(self):
        assert version_is_newer("1.0.0", "") is True

    def test_missing_remote_means_no(self):
        assert version_is_newer("", "1.0.0") is False

    def test_short_version(self):
        assert version_is_newer("2.0", "1.9") is True

    def test_uneven_lengths(self):
        # 1.0.0.1 should be newer than 1.0.0
        assert version_is_newer("1.0.0.1", "1.0.0") is True


class TestVersionKeySort:
    """Real-world scenario: ModDB returns releases interleaved by upload
    date when a maintainer alternates between branches. Sorting by
    version_key (descending) must group them by version branch."""

    def _vk(self, s):
        from core.parsers import version_key
        return version_key(s)

    def test_basic_descending(self):
        from core.parsers import version_key
        versions = ["1.0.0", "1.0.2", "1.0.1", "0.9.9"]
        out = sorted(versions, key=version_key, reverse=True)
        assert out == ["1.0.2", "1.0.1", "1.0.0", "0.9.9"]

    def test_double_digit_patch_sorts_above_single_digit(self):
        # The classic "1.0.10 < 1.0.2" lexicographic trap.
        from core.parsers import version_key
        versions = ["1.0.2", "1.0.10", "1.0.9"]
        out = sorted(versions, key=version_key, reverse=True)
        assert out == ["1.0.10", "1.0.9", "1.0.2"]

    def test_amulet_bed_spawn_screenshot_scenario(self):
        # Pinned regression for the user's ModDB screenshot:
        # the file list was interleaved like v0.5.7, v1.0.7, v0.5.6,
        # v1.0.6 ... after sorting it should be 1.0.x then 0.5.x.
        from core.parsers import version_key
        interleaved = [
            "0.5.7", "1.0.7", "0.5.6", "1.0.6",
            "0.5.5", "1.0.5", "0.5.4", "1.0.4",
            "1.0.3", "1.0.2", "1.0.1", "0.5.3", "0.5.2",
        ]
        out = sorted(interleaved, key=version_key, reverse=True)
        # All 1.0.x come first, then all 0.5.x.
        expected = [
            "1.0.7", "1.0.6", "1.0.5", "1.0.4",
            "1.0.3", "1.0.2", "1.0.1",
            "0.5.7", "0.5.6", "0.5.5", "0.5.4", "0.5.3", "0.5.2",
        ]
        assert out == expected

    def test_empty_string_sorts_last(self):
        from core.parsers import version_key
        versions = ["1.0.0", "", "0.5.0", None]
        out = sorted(
            (v for v in versions if v is not None),
            key=version_key, reverse=True,
        )
        assert out == ["1.0.0", "0.5.0", ""]

    def test_unparseable_doesnt_crash(self):
        from core.parsers import version_key
        # Some maintainers do weird things — make sure we don't raise.
        version_key("garbage")
        version_key("v?")
        version_key("1.x.0")

    def test_prerelease_betterruins_scenario(self):
        # Pinned regression for the user-reported BetterRuins screenshot:
        # the file list showed pre-release versions ABOVE their clean
        # release. After the fallback fix, 0.6.0 sorts above all its
        # 0.6.0-rc.* and 0.6.0-pre.* siblings.
        from core.parsers import version_key
        versions = [
            "0.6.0", "0.6.0-rc.2", "0.6.0-rc.1", "0.6.0-pre.1",
            "0.5.7", "0.5.6",
        ]
        out = sorted(versions, key=version_key, reverse=True)
        assert out == [
            "0.6.0",
            "0.6.0-rc.2",
            "0.6.0-rc.1",
            "0.6.0-pre.1",
            "0.5.7",
            "0.5.6",
        ]

    def test_prerelease_with_fallback(self):
        # Force the no-`packaging` path so we exercise the fallback
        # comparator regardless of what's installed locally.
        import core.parsers as p
        original = p._HAVE_PKG_VERSION
        p._HAVE_PKG_VERSION = False
        try:
            versions = ["0.6.0", "0.6.0-rc.2", "0.6.0-pre.1", "0.5.7"]
            out = sorted(versions, key=p.version_key, reverse=True)
            assert out[0] == "0.6.0", \
                f"Expected 0.6.0 first, got {out}"
            assert out[1] == "0.6.0-rc.2"
            assert out[2] == "0.6.0-pre.1"
            assert out[3] == "0.5.7"
        finally:
            p._HAVE_PKG_VERSION = original

    def test_prerelease_rc_above_pre(self):
        # rc and pre are equivalent under PEP 440 normalisation (the
        # `packaging` library treats `1.0.0-pre.1` and `1.0.0-rc.1` as
        # the same version). So in the with-packaging path we just
        # ensure neither raises and they sort consistently.
        # In the fallback path, `rc` > `pre` alphabetically.
        import core.parsers as p
        original = p._HAVE_PKG_VERSION
        p._HAVE_PKG_VERSION = False
        try:
            versions = ["1.0.0-pre.1", "1.0.0-rc.1"]
            out = sorted(versions, key=p.version_key, reverse=True)
            assert out == ["1.0.0-rc.1", "1.0.0-pre.1"]
        finally:
            p._HAVE_PKG_VERSION = original

    def test_v_prefix_stripped(self):
        # Some maintainers prefix with 'v' — make sure that doesn't
        # break the comparator.
        import core.parsers as p
        original = p._HAVE_PKG_VERSION
        p._HAVE_PKG_VERSION = False
        try:
            versions = ["v1.0.0", "v0.9.9"]
            out = sorted(versions, key=p.version_key, reverse=True)
            assert out == ["v1.0.0", "v0.9.9"]
        finally:
            p._HAVE_PKG_VERSION = original
