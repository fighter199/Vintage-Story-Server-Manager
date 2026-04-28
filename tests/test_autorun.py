"""Tests for core.autorun (pure-logic scheduler)."""
import pytest

from core.autorun import (
    AutorunAudit,
    AutorunScheduler,
    expand_commands,
    make_empty_rule,
    normalize_rule,
    validate_rule,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
class FakeClock:
    """Manually-advanced clock for deterministic tests."""
    def __init__(self, t: float = 1000.0):
        self.t = t
    def __call__(self) -> float:
        return self.t
    def advance(self, dt: float) -> None:
        self.t += dt


class Sender:
    def __init__(self):
        self.sent: list[str] = []
    def __call__(self, cmd: str) -> None:
        self.sent.append(cmd)


# ----------------------------------------------------------------------
# Rule helpers
# ----------------------------------------------------------------------
class TestMakeEmptyRule:
    def test_returns_dict_with_required_fields(self):
        r = make_empty_rule()
        assert isinstance(r, dict)
        assert r["enabled"] is True
        assert r["interval_secs"] >= 1
        assert "commands" in r and "name" in r


class TestNormalizeRule:
    def test_drops_unknown_keys(self):
        r = {"name": "x", "interval_secs": 60, "commands": "/save",
             "evil_payload": "rm -rf /"}
        normalize_rule(r)
        assert "evil_payload" not in r

    def test_clamps_zero_interval(self):
        r = {"name": "x", "interval_secs": 0, "commands": "/save"}
        normalize_rule(r)
        assert r["interval_secs"] == 1

    def test_clamps_negative_interval(self):
        r = {"name": "x", "interval_secs": -100, "commands": "/save"}
        normalize_rule(r)
        assert r["interval_secs"] == 1

    def test_coerces_bool_fields(self):
        r = {"name": "x", "interval_secs": 60, "commands": "/save",
             "enabled": 1, "run_on_start": "yes", "pause_when_empty": 0}
        normalize_rule(r)
        assert r["enabled"] is True
        assert r["run_on_start"] is True
        assert r["pause_when_empty"] is False

    def test_handles_garbage_interval(self):
        r = {"name": "x", "interval_secs": "notanumber",
             "commands": "/save"}
        normalize_rule(r)
        assert r["interval_secs"] == 300  # default

    def test_strips_name_whitespace(self):
        r = {"name": "  hourly  ", "interval_secs": 60, "commands": "/save"}
        normalize_rule(r)
        assert r["name"] == "hourly"

    def test_seeds_missing_fields(self):
        r = {"name": "x", "interval_secs": 60, "commands": "/save"}
        normalize_rule(r)
        # Missing booleans get sensible defaults
        assert "enabled" in r and r["enabled"] is True
        assert "run_on_start" in r
        assert "pause_when_empty" in r

    def test_rejects_non_dict(self):
        with pytest.raises(TypeError):
            normalize_rule("notadict")


class TestValidateRule:
    def test_ok_rule(self):
        r = make_empty_rule()
        r["name"] = "save"
        r["commands"] = "/save"
        ok, reason = validate_rule(r)
        assert ok is True
        assert reason == ""

    def test_missing_name(self):
        r = make_empty_rule()
        r["commands"] = "/save"
        ok, reason = validate_rule(r)
        assert ok is False and "name" in reason

    def test_zero_interval(self):
        r = make_empty_rule()
        r["name"] = "x"
        r["commands"] = "/save"
        r["interval_secs"] = 0
        ok, reason = validate_rule(r)
        assert ok is False and "interval" in reason

    def test_no_commands(self):
        r = make_empty_rule()
        r["name"] = "x"
        r["commands"] = ""
        ok, reason = validate_rule(r)
        assert ok is False and "command" in reason

    def test_only_comments(self):
        r = make_empty_rule()
        r["name"] = "x"
        r["commands"] = "# just a comment\n# another"
        ok, reason = validate_rule(r)
        assert ok is False


class TestExpandCommands:
    def test_single_line(self):
        assert expand_commands("/save") == ["/save"]

    def test_multi_line(self):
        out = expand_commands("/save\n/announce ok")
        assert out == ["/save", "/announce ok"]

    def test_drops_blank_and_comments(self):
        out = expand_commands("\n/save\n# a comment\n  \n/announce ok\n")
        assert out == ["/save", "/announce ok"]

    def test_handles_none(self):
        assert expand_commands("") == []


# ----------------------------------------------------------------------
# Scheduler
# ----------------------------------------------------------------------
class TestSchedulerLifecycle:
    def test_starts_armed_only_after_start(self):
        clock = FakeClock(1000)
        send = Sender()
        rules = [{"name": "save", "interval_secs": 60,
                  "commands": "/save", "enabled": True}]
        sch = AutorunScheduler(lambda: rules, send, lambda: 1,
                                clock=clock)
        # Tick before start does nothing
        assert sch.tick() == 0
        assert send.sent == []
        sch.start()
        assert sch.running is True

    def test_stop_clears_state(self):
        clock = FakeClock(1000)
        send = Sender()
        rules = [{"name": "save", "interval_secs": 60,
                  "commands": "/save", "enabled": True}]
        sch = AutorunScheduler(lambda: rules, send, lambda: 1,
                                clock=clock)
        sch.start()
        sch.stop()
        assert sch.running is False
        clock.advance(120)
        assert sch.tick() == 0


class TestSchedulerFiring:
    def test_fires_at_interval(self):
        clock = FakeClock(1000)
        send = Sender()
        rules = [{"name": "save", "interval_secs": 60,
                  "commands": "/save", "enabled": True}]
        sch = AutorunScheduler(lambda: rules, send, lambda: 1,
                                clock=clock)
        sch.start()
        # No fire yet
        clock.advance(30)
        sch.tick()
        assert send.sent == []
        # Fire at 60s exactly
        clock.advance(31)
        sch.tick()
        assert send.sent == ["/save"]

    def test_run_on_start_fires_immediately(self):
        clock = FakeClock(1000)
        send = Sender()
        rules = [{"name": "save", "interval_secs": 600,
                  "commands": "/save", "enabled": True,
                  "run_on_start": True}]
        sch = AutorunScheduler(lambda: rules, send, lambda: 1,
                                clock=clock)
        sch.start()
        # Fired during start, not by a tick
        assert send.sent == ["/save"]

    def test_run_on_start_then_interval(self):
        clock = FakeClock(1000)
        send = Sender()
        rules = [{"name": "save", "interval_secs": 60,
                  "commands": "/save", "enabled": True,
                  "run_on_start": True}]
        sch = AutorunScheduler(lambda: rules, send, lambda: 1,
                                clock=clock)
        sch.start()
        assert send.sent == ["/save"]  # immediate
        clock.advance(60)
        sch.tick()
        assert send.sent == ["/save", "/save"]  # then again after interval

    def test_multi_line_commands(self):
        clock = FakeClock(1000)
        send = Sender()
        rules = [{"name": "x", "interval_secs": 60,
                  "commands": "/save\n/announce backup done",
                  "enabled": True}]
        sch = AutorunScheduler(lambda: rules, send, lambda: 1,
                                clock=clock)
        sch.start()
        clock.advance(60)
        sch.tick()
        assert send.sent == ["/save", "/announce backup done"]

    def test_disabled_rule_does_not_fire(self):
        clock = FakeClock(1000)
        send = Sender()
        rules = [{"name": "save", "interval_secs": 60,
                  "commands": "/save", "enabled": False}]
        sch = AutorunScheduler(lambda: rules, send, lambda: 1,
                                clock=clock)
        sch.start()
        clock.advance(120)
        sch.tick()
        assert send.sent == []

    def test_pause_when_empty_skips_when_no_players(self):
        clock = FakeClock(1000)
        send = Sender()
        rules = [{"name": "save", "interval_secs": 60,
                  "commands": "/save", "enabled": True,
                  "pause_when_empty": True}]
        # 0 players online
        sch = AutorunScheduler(lambda: rules, send, lambda: 0,
                                clock=clock)
        sch.start()
        clock.advance(60)
        sch.tick()
        assert send.sent == []

    def test_pause_when_empty_fires_with_players(self):
        clock = FakeClock(1000)
        send = Sender()
        rules = [{"name": "save", "interval_secs": 60,
                  "commands": "/save", "enabled": True,
                  "pause_when_empty": True}]
        sch = AutorunScheduler(lambda: rules, send, lambda: 5,
                                clock=clock)
        sch.start()
        clock.advance(60)
        sch.tick()
        assert send.sent == ["/save"]

    def test_pause_skip_still_advances_schedule(self):
        # If we paused for one window, we shouldn't fire 4 times in a
        # row when players come back — the missed window is dropped.
        clock = FakeClock(1000)
        send = Sender()
        players = [0]
        rules = [{"name": "save", "interval_secs": 60,
                  "commands": "/save", "enabled": True,
                  "pause_when_empty": True}]
        sch = AutorunScheduler(lambda: rules, send, lambda: players[0],
                                clock=clock)
        sch.start()
        # 5 minutes of empty server — would've been 5 fires
        for _ in range(5):
            clock.advance(60)
            sch.tick()
        assert send.sent == []
        # Player joins
        players[0] = 1
        clock.advance(60)
        sch.tick()
        # Only the next scheduled tick fires, not 5 backlogged ones
        assert send.sent == ["/save"]


class TestSchedulerLiveEdits:
    def test_new_rule_added_mid_session_is_armed(self):
        clock = FakeClock(1000)
        send = Sender()
        rules: list = []
        sch = AutorunScheduler(lambda: rules, send, lambda: 1,
                                clock=clock)
        sch.start()
        # Add a rule after scheduler is running
        rules.append({"name": "save", "interval_secs": 60,
                      "commands": "/save", "enabled": True})
        clock.advance(30)
        sch.tick()  # arm it
        assert send.sent == []
        clock.advance(60)
        sch.tick()  # fires
        assert send.sent == ["/save"]

    def test_removed_rule_stops_firing(self):
        clock = FakeClock(1000)
        send = Sender()
        rules = [{"name": "save", "interval_secs": 60,
                  "commands": "/save", "enabled": True}]
        sch = AutorunScheduler(lambda: rules, send, lambda: 1,
                                clock=clock)
        sch.start()
        rules.clear()
        clock.advance(120)
        sch.tick()
        assert send.sent == []

    def test_independent_intervals(self):
        clock = FakeClock(1000)
        send = Sender()
        rules = [
            {"name": "fast",  "interval_secs": 30,
             "commands": "/a", "enabled": True},
            {"name": "slow",  "interval_secs": 90,
             "commands": "/b", "enabled": True},
        ]
        sch = AutorunScheduler(lambda: rules, send, lambda: 1,
                                clock=clock)
        sch.start()
        # 90s = 3 fast + 1 slow
        for _ in range(3):
            clock.advance(30)
            sch.tick()
        assert send.sent.count("/a") == 3
        assert send.sent.count("/b") == 1


class TestSchedulerAudit:
    def test_audit_called_for_fire(self):
        clock = FakeClock(1000)
        send = Sender()
        events: list[AutorunAudit] = []
        rules = [{"name": "save", "interval_secs": 60,
                  "commands": "/save", "enabled": True}]
        sch = AutorunScheduler(lambda: rules, send, lambda: 1,
                                audit=events.append, clock=clock)
        sch.start()
        clock.advance(60)
        sch.tick()
        assert len(events) == 1
        assert events[0].fired is True
        assert events[0].rule_name == "save"
        assert events[0].commands == ["/save"]

    def test_audit_called_for_skip(self):
        clock = FakeClock(1000)
        send = Sender()
        events: list[AutorunAudit] = []
        rules = [{"name": "save", "interval_secs": 60,
                  "commands": "/save", "enabled": False}]
        sch = AutorunScheduler(lambda: rules, send, lambda: 1,
                                audit=events.append, clock=clock)
        sch.start()
        clock.advance(60)
        sch.tick()
        assert len(events) == 1
        assert events[0].fired is False
        assert events[0].skipped_reason == "disabled"

    def test_broken_send_does_not_break_other_rules(self):
        # If a send raises, the next command in the same tick still fires.
        clock = FakeClock(1000)
        send_good = Sender()
        def send(cmd):
            if cmd == "/bad":
                raise RuntimeError("boom")
            send_good(cmd)
        rules = [{"name": "x", "interval_secs": 60,
                  "commands": "/bad\n/good", "enabled": True}]
        sch = AutorunScheduler(lambda: rules, send, lambda: 1,
                                clock=clock)
        sch.start()
        clock.advance(60)
        sch.tick()
        assert send_good.sent == ["/good"]


class TestSchedulerInspection:
    def test_seconds_to_next(self):
        clock = FakeClock(1000)
        send = Sender()
        rules = [{"name": "save", "interval_secs": 60,
                  "commands": "/save", "enabled": True}]
        sch = AutorunScheduler(lambda: rules, send, lambda: 1,
                                clock=clock)
        sch.start()
        clock.advance(20)
        s = sch.seconds_to_next("save")
        assert 39.9 <= s <= 40.1

    def test_seconds_to_next_unknown_returns_none(self):
        clock = FakeClock(1000)
        sch = AutorunScheduler(lambda: [], lambda c: None, lambda: 1,
                                clock=clock)
        sch.start()
        assert sch.seconds_to_next("nope") is None

    def test_seconds_to_next_when_stopped_returns_none(self):
        clock = FakeClock(1000)
        rules = [{"name": "save", "interval_secs": 60,
                  "commands": "/save", "enabled": True}]
        sch = AutorunScheduler(lambda: rules, lambda c: None, lambda: 1,
                                clock=clock)
        # never started
        assert sch.seconds_to_next("save") is None
