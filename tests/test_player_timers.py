"""Tests for core.player_timers."""
import pytest

from core.player_timers import PlayerTimers, fmt_duration


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t
    def __call__(self) -> float:
        return self.t
    def advance(self, dt: float) -> None:
        self.t += dt


class TestSessionTracking:
    def test_session_zero_when_not_online(self):
        clock = FakeClock(1000)
        totals: dict = {}
        pt = PlayerTimers(lambda: totals, clock=clock)
        assert pt.session_secs("alice") == 0
        assert pt.has_active_session("alice") is False

    def test_join_starts_session(self):
        clock = FakeClock(1000)
        totals: dict = {}
        pt = PlayerTimers(lambda: totals, clock=clock)
        pt.record_join("alice")
        clock.advance(45)
        assert pt.session_secs("alice") == 45
        assert pt.has_active_session("alice") is True

    def test_leave_ends_session(self):
        clock = FakeClock(1000)
        totals: dict = {}
        pt = PlayerTimers(lambda: totals, clock=clock)
        pt.record_join("alice")
        clock.advance(60)
        pt.record_leave("alice")
        assert pt.session_secs("alice") == 0
        assert pt.has_active_session("alice") is False

    def test_rejoin_resets_session(self):
        clock = FakeClock(1000)
        totals: dict = {}
        pt = PlayerTimers(lambda: totals, clock=clock)
        pt.record_join("alice")
        clock.advance(120)
        pt.record_leave("alice")
        clock.advance(300)  # 5 min offline
        pt.record_join("alice")
        clock.advance(10)
        # Session timer is fresh
        assert pt.session_secs("alice") == 10

    def test_double_join_no_op(self):
        # If two join events fire (e.g. duplicated by /list clients sync)
        # the second one should not reset the session.
        clock = FakeClock(1000)
        totals: dict = {}
        pt = PlayerTimers(lambda: totals, clock=clock)
        pt.record_join("alice")
        clock.advance(30)
        pt.record_join("alice")  # spurious second join
        clock.advance(15)
        # Total session should be 45s, not 15s
        assert pt.session_secs("alice") == 45

    def test_leave_for_offline_player_no_op(self):
        clock = FakeClock(1000)
        totals: dict = {}
        pt = PlayerTimers(lambda: totals, clock=clock)
        # Should not crash, and totals should remain at 0
        pt.record_leave("nobody")
        assert totals == {}


class TestTotalAccumulation:
    def test_total_is_persisted_after_leave(self):
        clock = FakeClock(1000)
        totals: dict = {}
        pt = PlayerTimers(lambda: totals, clock=clock)
        pt.record_join("alice")
        clock.advance(120)
        pt.record_leave("alice")
        assert totals.get("alice") == 120

    def test_total_includes_active_session(self):
        clock = FakeClock(1000)
        totals: dict = {"alice": 60}  # 1 min of prior playtime
        pt = PlayerTimers(lambda: totals, clock=clock)
        pt.record_join("alice")
        clock.advance(30)
        # Total = 60 (prior) + 30 (current) = 90
        assert pt.total_secs("alice") == 90

    def test_multiple_sessions_accumulate(self):
        clock = FakeClock(1000)
        totals: dict = {}
        pt = PlayerTimers(lambda: totals, clock=clock)
        # Session 1: 100s
        pt.record_join("alice"); clock.advance(100); pt.record_leave("alice")
        # Session 2: 200s
        pt.record_join("alice"); clock.advance(200); pt.record_leave("alice")
        # Session 3: 50s
        pt.record_join("alice"); clock.advance(50);  pt.record_leave("alice")
        assert totals.get("alice") == 350

    def test_total_for_unknown_player(self):
        clock = FakeClock(1000)
        totals: dict = {}
        pt = PlayerTimers(lambda: totals, clock=clock)
        assert pt.total_secs("nobody") == 0

    def test_independent_players(self):
        clock = FakeClock(1000)
        totals: dict = {}
        pt = PlayerTimers(lambda: totals, clock=clock)
        pt.record_join("alice"); clock.advance(60)
        pt.record_join("bob");   clock.advance(30)
        assert pt.session_secs("alice") == 90
        assert pt.session_secs("bob") == 30


class TestFlush:
    def test_flush_with_no_active_sessions(self):
        clock = FakeClock(1000)
        totals: dict = {}
        pt = PlayerTimers(lambda: totals, clock=clock)
        assert pt.flush() == 0

    def test_flush_accumulates_active(self):
        clock = FakeClock(1000)
        totals: dict = {}
        pt = PlayerTimers(lambda: totals, clock=clock)
        pt.record_join("alice")
        clock.advance(60)
        flushed = pt.flush()
        assert flushed == 60
        assert totals.get("alice") == 60
        # Session is still active — they didn't leave
        assert pt.has_active_session("alice")

    def test_flush_does_not_double_count(self):
        # Critical: if flush runs every minute, we mustn't double-add
        # the accumulated time on each call.
        clock = FakeClock(1000)
        totals: dict = {}
        pt = PlayerTimers(lambda: totals, clock=clock)
        pt.record_join("alice")
        clock.advance(60)
        pt.flush()
        assert totals.get("alice") == 60
        clock.advance(60)
        pt.flush()
        assert totals.get("alice") == 120
        clock.advance(60)
        pt.flush()
        assert totals.get("alice") == 180

    def test_flush_then_leave_no_double_count(self):
        # Flush halfway through a session, then leave: total should
        # equal the full session length.
        clock = FakeClock(1000)
        totals: dict = {}
        pt = PlayerTimers(lambda: totals, clock=clock)
        pt.record_join("alice")
        clock.advance(60)
        pt.flush()
        clock.advance(60)
        pt.record_leave("alice")
        assert totals.get("alice") == 120

    def test_total_accurate_during_flush_cycle(self):
        # total_secs() must read correctly between flushes — i.e.
        # base + unflushed-so-far without overlap.
        clock = FakeClock(1000)
        totals: dict = {}
        pt = PlayerTimers(lambda: totals, clock=clock)
        pt.record_join("alice")
        clock.advance(60)
        pt.flush()              # 60 in totals, 0 unflushed
        clock.advance(45)
        # Should report 60 (flushed) + 45 (unflushed) = 105
        assert pt.total_secs("alice") == 105

    def test_reset_all_flushes_everything(self):
        clock = FakeClock(1000)
        totals: dict = {}
        pt = PlayerTimers(lambda: totals, clock=clock)
        pt.record_join("alice"); clock.advance(60)
        pt.record_join("bob");   clock.advance(30)
        pt.reset_all()
        assert totals.get("alice") == 90
        assert totals.get("bob") == 30
        assert pt.active_players() == []


class TestForgetPlayer:
    def test_forget_drops_active_and_total(self):
        clock = FakeClock(1000)
        totals: dict = {"alice": 500}
        pt = PlayerTimers(lambda: totals, clock=clock)
        pt.record_join("alice")
        clock.advance(60)
        pt.forget_player("alice")
        assert pt.session_secs("alice") == 0
        assert pt.total_secs("alice") == 0
        assert "alice" not in totals


class TestFmtDuration:
    def test_under_a_minute(self):
        assert fmt_duration(45) == "0:00:45"

    def test_minutes(self):
        assert fmt_duration(125) == "0:02:05"

    def test_hours(self):
        assert fmt_duration(3725) == "1:02:05"

    def test_just_under_a_day(self):
        assert fmt_duration(86399) == "23:59:59"

    def test_a_day_exactly_uses_day_format(self):
        assert fmt_duration(86400) == "1d 00:00"

    def test_multiple_days(self):
        assert fmt_duration(90061) == "1d 01:01"

    def test_a_week(self):
        assert fmt_duration(604800) == "7d 00:00"

    def test_negative_clamped(self):
        assert fmt_duration(-50) == "0:00:00"

    def test_zero(self):
        assert fmt_duration(0) == "0:00:00"
