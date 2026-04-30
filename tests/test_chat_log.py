"""Tests for core.chat_log."""
import pytest

from core.chat_log import (
    ChatEntry,
    ChatLogStore,
    parse_chat_with_group,
)


# Trivial strip helper for tests — peels the timestamps + tag prefix
# the same way the real strip_log_prefix does for the user's samples.
def _strip(line: str) -> str:
    import re
    s = line
    s = re.sub(r"^\s*\[\d{1,2}:\d{2}:\d{2}\]\s+", "", s)
    s = re.sub(r"^\s*\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\s+\d{1,2}:\d{2}:\d{2}(?:[,.]\d+)?\s+",
               "", s)
    s = re.sub(r"^\s*\[(?:Server\s+)?Chat\]\s*", "", s, flags=re.I)
    return s


class TestParseChatWithGroup:
    def test_general_chat_group_zero(self):
        line = "[23:39:03] 29.4.2026 23:39:03 [Server Chat] 0 | DerelictDawn: Oh"
        assert parse_chat_with_group(line, _strip) == \
            ("0", "DerelictDawn", "Oh")

    def test_named_group_two_digits(self):
        line = ("[00:34:32] 30.4.2026 00:34:32 [Server Chat] "
                "10 | Fighter199: testing chat long")
        assert parse_chat_with_group(line, _strip) == \
            ("10", "Fighter199", "testing chat long")

    def test_message_with_punctuation(self):
        line = "[Server Chat] 0 | DerelictDawn: K we can't quench until iron"
        assert parse_chat_with_group(line, _strip) == \
            ("0", "DerelictDawn", "K we can't quench until iron")

    def test_message_containing_colon(self):
        # The split is on the FIRST colon — anything after stays in
        # the message.
        line = "[Server Chat] 0 | Alice: ratio is 1:2:3"
        assert parse_chat_with_group(line, _strip) == \
            ("0", "Alice", "ratio is 1:2:3")

    def test_non_chat_line_returns_none(self):
        # No Chat tag → not chat
        line = "[Server Notification] List of online Players"
        assert parse_chat_with_group(line, _strip) == (None, None, None)

    def test_angle_bracket_form_returns_none(self):
        # The angle-bracket form has no group ID; this function only
        # handles the colon form. (Fall back to parse_chat_message
        # for the angle form.)
        line = "[Server Chat] <Alice> hello"
        assert parse_chat_with_group(line, _strip) == (None, None, None)

    def test_empty_input(self):
        assert parse_chat_with_group("", _strip) == (None, None, None)
        assert parse_chat_with_group(None, _strip) == (None, None, None)

    def test_empty_message(self):
        line = "[Server Chat] 0 | Alice: "
        gid, p, m = parse_chat_with_group(line, _strip)
        assert gid == "0" and p == "Alice" and m == ""

    def test_no_chat_tag_no_match(self):
        # Even if the line LOOKS like the chat shape, without the tag
        # we don't claim it (defends against e.g. notification lines
        # that have a "0 | foo: bar" suffix in some unforeseen format).
        line = "29.4.2026 12:00:00 [Server Notification] 0 | Alice: hi"
        assert parse_chat_with_group(line, _strip) == (None, None, None)


class TestChatLogStoreBasics:
    def test_append_creates_group(self):
        s = ChatLogStore()
        s.append("0", "Alice", "hi")
        assert len(s.entries("0")) == 1
        e = s.entries("0")[0]
        assert e.player == "Alice"
        assert e.message == "hi"

    def test_append_to_unknown_group_creates_it(self):
        s = ChatLogStore()
        s.append("42", "Bob", "secret")
        assert "42" in s.known_group_ids()
        assert s.entries("42")[0].message == "secret"

    def test_known_group_ids_sorts_zero_first(self):
        s = ChatLogStore()
        s.append("10", "Alice", "x")
        s.append("0",  "Bob",   "y")
        s.append("5",  "Carol", "z")
        # 0, 5, 10 (numeric ascending after pinning 0)
        assert s.known_group_ids() == ["0", "5", "10"]

    def test_known_group_ids_includes_named_only(self):
        # Even if no entries yet, a group with a custom name is
        # discoverable. Useful for "I just renamed group 7 — show me a tab".
        s = ChatLogStore()
        s.set_name("7", "Builders")
        assert "7" in s.known_group_ids()

    def test_empty_inputs_no_op(self):
        s = ChatLogStore()
        s.append("", "Alice", "x")
        s.append("0", "", "x")
        assert s.known_group_ids() == []
        assert not s.is_dirty()


class TestRingBuffer:
    def test_caps_at_max(self):
        # Use 50 (well above the 10-min clamp) so we actually exercise
        # the cap rather than tripping over the lower bound.
        s = ChatLogStore(max_per_group=50)
        for i in range(80):
            s.append("0", "Alice", f"msg {i}")
        buf = s.entries("0")
        assert len(buf) == 50
        # Newest 50 are kept; oldest evicted
        assert buf[0].message  == "msg 30"
        assert buf[-1].message == "msg 79"

    def test_min_cap_clamped(self):
        # max < 10 is clamped to 10 (defensive — too low is unusable)
        s = ChatLogStore(max_per_group=2)
        for i in range(15):
            s.append("0", "Alice", f"msg {i}")
        assert len(s.entries("0")) == 10


class TestAllEntriesSorted:
    def test_merges_by_timestamp(self):
        s = ChatLogStore()
        s.append("0",  "Alice", "first",  now=100.0)
        s.append("10", "Bob",   "second", now=200.0)
        s.append("0",  "Carol", "third",  now=300.0)
        merged = s.all_entries_sorted()
        assert [e.message for _, e in merged] == \
            ["first", "second", "third"]

    def test_includes_group_id(self):
        s = ChatLogStore()
        s.append("0",  "Alice", "a", now=1.0)
        s.append("10", "Bob",   "b", now=2.0)
        merged = s.all_entries_sorted()
        assert [gid for gid, _ in merged] == ["0", "10"]


class TestGroupNames:
    def test_default_name_for_zero(self):
        s = ChatLogStore()
        assert s.display_name("0") == "General"

    def test_default_name_for_other(self):
        s = ChatLogStore()
        assert s.display_name("10") == "Group 10"

    def test_custom_name(self):
        s = ChatLogStore()
        s.set_name("10", "Builders")
        assert s.display_name("10") == "Builders"
        assert s.has_custom_name("10")

    def test_custom_name_for_general(self):
        # User CAN override "General" — useful if they prefer "Public"
        # or some other label.
        s = ChatLogStore()
        s.set_name("0", "Public")
        assert s.display_name("0") == "Public"

    def test_clearing_name_falls_back(self):
        s = ChatLogStore()
        s.set_name("10", "Builders")
        s.set_name("10", "")
        assert s.display_name("10") == "Group 10"
        assert not s.has_custom_name("10")

    def test_whitespace_only_name_treated_as_clear(self):
        s = ChatLogStore()
        s.set_name("10", "Builders")
        s.set_name("10", "   ")
        assert s.display_name("10") == "Group 10"

    def test_setting_name_marks_dirty(self):
        s = ChatLogStore()
        assert not s.is_dirty()
        s.set_name("10", "Builders")
        assert s.is_dirty()


class TestPersistence:
    def test_flush_writes_via_callback(self):
        written: list[dict] = []
        s = ChatLogStore(save_history=lambda b: written.append(b))
        s.append("0",  "Alice", "hi", now=100.0)
        s.set_name("10", "Builders")
        ok = s.flush()
        assert ok is True
        assert len(written) == 1
        blob = written[0]
        assert blob["version"] == 1
        assert blob["groups"]["0"][0]["p"] == "Alice"
        assert blob["names"]["10"] == "Builders"

    def test_flush_clears_dirty(self):
        written: list[dict] = []
        s = ChatLogStore(save_history=lambda b: written.append(b))
        s.append("0", "Alice", "x")
        assert s.is_dirty()
        s.flush()
        assert not s.is_dirty()

    def test_flush_no_op_when_clean(self):
        written: list[dict] = []
        s = ChatLogStore(save_history=lambda b: written.append(b))
        ok = s.flush()
        assert ok is False
        assert written == []

    def test_flush_no_op_without_save_callback(self):
        s = ChatLogStore()  # no save callback
        s.append("0", "Alice", "x")
        assert s.flush() is False

    def test_load_restores_state(self):
        blob = {
            "version": 1,
            "groups": {
                "0":  [{"ts": 100.0, "p": "Alice", "m": "hi"}],
                "10": [{"ts": 200.0, "p": "Bob",   "m": "secret"}],
            },
            "names": {"10": "Builders"},
        }
        s = ChatLogStore(load_history=lambda: blob)
        assert len(s.entries("0")) == 1
        assert s.entries("0")[0].player == "Alice"
        assert s.display_name("10") == "Builders"
        # Loading shouldn't mark dirty (nothing changed since load)
        assert not s.is_dirty()

    def test_load_corrupt_blob_starts_clean(self):
        # A garbage blob should not crash construction.
        s = ChatLogStore(load_history=lambda: "not a dict")
        assert s.known_group_ids() == []

    def test_load_partial_blob_is_resilient(self):
        # A blob missing 'names' or 'groups' is fine.
        s = ChatLogStore(load_history=lambda: {"version": 1})
        assert s.known_group_ids() == []
        s.append("0", "Alice", "x")
        assert len(s.entries("0")) == 1

    def test_load_drops_malformed_entries(self):
        blob = {
            "version": 1,
            "groups": {
                "0": [
                    {"ts": 100.0, "p": "Alice", "m": "ok"},
                    "not a dict",                          # garbage
                    {"ts": "bad", "p": "Bob",   "m": "x"}, # bad ts but ChatEntry coerces
                ],
            },
        }
        s = ChatLogStore(load_history=lambda: blob)
        # Only "ok" and the bad-ts entry survive (the latter coerces to 0)
        msgs = [e.message for e in s.entries("0")]
        assert "ok" in msgs

    def test_load_honours_max_per_group(self):
        # Disk has 100 entries but max is 50 — only the last 50 survive.
        # (We use 50 not 5 so we're above the 10-entry minimum clamp.)
        blob = {
            "version": 1,
            "groups": {
                "0": [
                    {"ts": float(i), "p": "Alice", "m": str(i)}
                    for i in range(100)
                ]
            },
        }
        s = ChatLogStore(load_history=lambda: blob, max_per_group=50)
        msgs = [e.message for e in s.entries("0")]
        # Last 50: messages 50..99
        assert msgs[0] == "50"
        assert msgs[-1] == "99"
        assert len(msgs) == 50


class TestClear:
    def test_clear_group(self):
        s = ChatLogStore()
        s.append("0", "Alice", "x")
        s.append("10", "Bob",  "y")
        s.clear_group("0")
        assert s.entries("0") == []
        # Other groups untouched
        assert len(s.entries("10")) == 1

    def test_clear_group_keeps_name(self):
        s = ChatLogStore()
        s.set_name("10", "Builders")
        s.append("10", "Bob", "x")
        s.clear_group("10")
        # Name survives so the tab keeps its label
        assert s.display_name("10") == "Builders"

    def test_clear_all(self):
        s = ChatLogStore()
        s.append("0",  "Alice", "x")
        s.append("10", "Bob",   "y")
        s.set_name("10", "Builders")
        s.clear_all()
        assert s.entries("0") == []
        assert s.entries("10") == []
        # Names survive clear_all (intentional)
        assert s.display_name("10") == "Builders"


class TestRoundTrip:
    def test_save_then_load_preserves_state(self):
        captured: dict = {}
        def save(b):
            captured.clear()
            captured.update(b)
        s = ChatLogStore(save_history=save)
        s.append("0",  "Alice", "hello", now=100.0)
        s.append("10", "Bob",   "secret", now=200.0)
        s.set_name("10", "Builders")
        s.flush()

        # Reconstruct from the captured blob
        s2 = ChatLogStore(load_history=lambda: dict(captured))
        assert len(s2.entries("0")) == 1
        assert s2.entries("0")[0].message == "hello"
        assert s2.display_name("10") == "Builders"
