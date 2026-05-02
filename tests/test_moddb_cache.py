"""Tests for mods.moddb_cache."""
import json
import os
import tempfile

import pytest

from mods.moddb_cache import ModDbCache, DEFAULT_TTL_SECS, WIRE_VERSION


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t
    def __call__(self) -> float:
        return self.t
    def advance(self, dt: float) -> None:
        self.t += dt


# ----------------------------------------------------------------------
# Basics
# ----------------------------------------------------------------------
class TestBasics:
    def test_empty_cache_misses(self):
        c = ModDbCache(path=None, clock=FakeClock())
        assert c.get("foo") is None
        assert "foo" not in c
        assert len(c) == 0

    def test_put_then_get(self):
        c = ModDbCache(path=None, clock=FakeClock())
        c.put("foo", {"name": "Foo Mod"})
        got = c.get("foo")
        assert got == {"name": "Foo Mod"}
        assert "foo" in c
        assert len(c) == 1

    def test_put_marks_dirty(self):
        c = ModDbCache(path=None, clock=FakeClock())
        assert not c.is_dirty()
        c.put("foo", {"name": "Foo Mod"})
        assert c.is_dirty()

    def test_put_rejects_empty_modid(self):
        c = ModDbCache(path=None, clock=FakeClock())
        c.put("", {"name": "x"})
        assert len(c) == 0
        assert not c.is_dirty()

    def test_put_rejects_non_dict(self):
        c = ModDbCache(path=None, clock=FakeClock())
        c.put("foo", "not a dict")
        assert len(c) == 0


# ----------------------------------------------------------------------
# TTL
# ----------------------------------------------------------------------
class TestTtl:
    def test_within_ttl_hit(self):
        clock = FakeClock(1000)
        c = ModDbCache(path=None, ttl_secs=3600, clock=clock)
        c.put("foo", {"v": "1.0"})
        clock.advance(60)
        assert c.get("foo") == {"v": "1.0"}

    def test_just_inside_ttl_still_hits(self):
        clock = FakeClock(1000)
        c = ModDbCache(path=None, ttl_secs=3600, clock=clock)
        c.put("foo", {"v": "1.0"})
        clock.advance(3599)
        assert c.get("foo") == {"v": "1.0"}

    def test_after_ttl_misses(self):
        clock = FakeClock(1000)
        c = ModDbCache(path=None, ttl_secs=3600, clock=clock)
        c.put("foo", {"v": "1.0"})
        clock.advance(3601)
        assert c.get("foo") is None

    def test_zero_ttl_always_misses(self):
        # ttl=0 means "no caching" — every get is a miss.
        clock = FakeClock(1000)
        c = ModDbCache(path=None, ttl_secs=0, clock=clock)
        c.put("foo", {"v": "1.0"})
        assert c.get("foo") is None

    def test_negative_ttl_always_misses(self):
        clock = FakeClock(1000)
        c = ModDbCache(path=None, ttl_secs=-1, clock=clock)
        c.put("foo", {"v": "1.0"})
        assert c.get("foo") is None

    def test_default_ttl_six_hours(self):
        # Implementation choice pinned: documented user-visible default.
        assert DEFAULT_TTL_SECS == 6 * 3600


# ----------------------------------------------------------------------
# Age
# ----------------------------------------------------------------------
class TestAge:
    def test_age_secs(self):
        clock = FakeClock(1000)
        c = ModDbCache(path=None, ttl_secs=3600, clock=clock)
        c.put("foo", {})
        clock.advance(120)
        age = c.age_secs("foo")
        assert age is not None
        assert 119 <= age <= 121

    def test_age_secs_unknown_modid(self):
        c = ModDbCache(path=None, clock=FakeClock())
        assert c.age_secs("nope") is None

    def test_age_secs_returns_age_even_when_expired(self):
        # Useful for "(cached 7h ago, refreshing…)" UI hints.
        clock = FakeClock(1000)
        c = ModDbCache(path=None, ttl_secs=3600, clock=clock)
        c.put("foo", {})
        clock.advance(7 * 3600)
        age = c.age_secs("foo")
        assert age is not None and age >= 6 * 3600


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------
class TestPersistence:
    def test_save_to_disk(self, tmp_path):
        path = str(tmp_path / "cache.json")
        clock = FakeClock(1000)
        c = ModDbCache(path=path, clock=clock)
        c.put("foo", {"name": "Foo", "version": "1.0"})
        ok = c.save()
        assert ok is True
        assert os.path.isfile(path)
        with open(path) as f:
            blob = json.load(f)
        assert blob["version"] == WIRE_VERSION
        assert blob["entries"]["foo"]["data"] == {"name": "Foo", "version": "1.0"}

    def test_save_clears_dirty(self, tmp_path):
        path = str(tmp_path / "cache.json")
        c = ModDbCache(path=path, clock=FakeClock())
        c.put("foo", {})
        c.save()
        assert not c.is_dirty()

    def test_save_no_op_when_clean(self, tmp_path):
        path = str(tmp_path / "cache.json")
        c = ModDbCache(path=path, clock=FakeClock())
        assert c.save() is False
        assert not os.path.exists(path)

    def test_save_no_op_without_path(self):
        c = ModDbCache(path=None, clock=FakeClock())
        c.put("foo", {})
        assert c.save() is False

    def test_load_restores_state(self, tmp_path):
        path = str(tmp_path / "cache.json")
        clock = FakeClock(1000)
        c1 = ModDbCache(path=path, clock=clock)
        c1.put("foo", {"v": "1.0"})
        c1.put("bar", {"v": "2.0"})
        c1.save()
        # Reconstruct
        c2 = ModDbCache(path=path, clock=clock)
        assert c2.get("foo") == {"v": "1.0"}
        assert c2.get("bar") == {"v": "2.0"}
        # Loading shouldn't mark dirty
        assert not c2.is_dirty()

    def test_load_corrupt_starts_clean(self, tmp_path):
        path = str(tmp_path / "cache.json")
        with open(path, "w") as f:
            f.write("not valid json {{{")
        c = ModDbCache(path=path, clock=FakeClock())
        assert len(c) == 0

    def test_load_wrong_version_ignored(self, tmp_path):
        path = str(tmp_path / "cache.json")
        with open(path, "w") as f:
            json.dump({
                "version": 999,
                "entries": {"foo": {"fetched_at": 1, "data": {}}},
            }, f)
        c = ModDbCache(path=path, clock=FakeClock())
        # Future versions are discarded, not misinterpreted.
        assert len(c) == 0

    def test_load_missing_file_ok(self, tmp_path):
        path = str(tmp_path / "does_not_exist.json")
        c = ModDbCache(path=path, clock=FakeClock())
        assert len(c) == 0
        assert not c.is_dirty()

    def test_load_drops_malformed_entries(self, tmp_path):
        path = str(tmp_path / "cache.json")
        with open(path, "w") as f:
            json.dump({
                "version": 1,
                "entries": {
                    "good": {"fetched_at": 100.0, "data": {"v": "1"}},
                    "bad_data": {"fetched_at": 100.0, "data": "not a dict"},
                    "bad_ts": {"fetched_at": "not a number", "data": {}},
                    "not_a_dict": "garbage",
                },
            }, f)
        c = ModDbCache(path=path, clock=FakeClock())
        assert "good" in c
        assert "bad_data" not in c
        assert "bad_ts" not in c
        assert "not_a_dict" not in c

    def test_atomic_write_via_tmp(self, tmp_path):
        # Confirm the .tmp file is cleaned up after a successful save.
        path = str(tmp_path / "cache.json")
        c = ModDbCache(path=path, clock=FakeClock())
        c.put("foo", {})
        c.save()
        assert os.path.exists(path)
        assert not os.path.exists(path + ".tmp")

    def test_round_trip_preserves_age(self, tmp_path):
        path = str(tmp_path / "cache.json")
        clock = FakeClock(1000)
        c1 = ModDbCache(path=path, ttl_secs=3600, clock=clock)
        c1.put("foo", {})
        c1.save()
        # Advance the clock then load — the entry should appear ~1h old.
        clock.advance(1800)
        c2 = ModDbCache(path=path, ttl_secs=3600, clock=clock)
        age = c2.age_secs("foo")
        assert 1799 <= age <= 1801
        # And still fresh (under 1h TTL? no, over 1800 < 3600 — fresh)
        assert c2.get("foo") == {}


# ----------------------------------------------------------------------
# Maintenance
# ----------------------------------------------------------------------
class TestMaintenance:
    def test_clear(self):
        c = ModDbCache(path=None, clock=FakeClock())
        c.put("a", {})
        c.put("b", {})
        c.clear()
        assert len(c) == 0

    def test_clear_marks_dirty(self):
        c = ModDbCache(path=None, clock=FakeClock())
        c.put("a", {})
        c.save() if False else None  # stays dirty since path=None
        c._dirty = False  # simulate fresh-from-disk state
        c.clear()
        assert c.is_dirty()

    def test_expire_stale(self):
        clock = FakeClock(1000)
        c = ModDbCache(path=None, ttl_secs=3600, clock=clock)
        c.put("old", {})
        clock.advance(7200)   # "old" is now 2h old
        c.put("new", {})       # "new" is fresh
        removed = c.expire_stale()
        assert removed == 1
        assert "old" not in c
        assert "new" in c

    def test_expire_stale_zero_ttl_no_op(self):
        clock = FakeClock(1000)
        c = ModDbCache(path=None, ttl_secs=0, clock=clock)
        c.put("x", {})
        assert c.expire_stale() == 0


# ----------------------------------------------------------------------
# Realistic scenarios
# ----------------------------------------------------------------------
class TestScenarios:
    def test_consecutive_update_checks(self, tmp_path):
        # First check populates the cache, second check hits 100% from
        # cache and persists nothing new.
        path = str(tmp_path / "cache.json")
        clock = FakeClock(1000)
        modids = ["mod_a", "mod_b", "mod_c"]

        # First check: simulate three network calls
        c1 = ModDbCache(path=path, clock=clock)
        for m in modids:
            assert c1.get(m) is None  # miss
            c1.put(m, {"name": m})    # write to cache
        c1.save()

        # Second check, 30 minutes later — all three hit
        clock.advance(30 * 60)
        c2 = ModDbCache(path=path, clock=clock)
        hits = sum(1 for m in modids if c2.get(m) is not None)
        assert hits == 3
        # No new puts → not dirty
        assert not c2.is_dirty()

    def test_partial_cache_recheck(self, tmp_path):
        # First check covered only half the mods. Second check hits
        # the cached half and fetches the rest.
        path = str(tmp_path / "cache.json")
        clock = FakeClock(1000)
        c1 = ModDbCache(path=path, clock=clock)
        c1.put("mod_a", {"v": "1"})
        c1.put("mod_b", {"v": "1"})
        c1.save()

        c2 = ModDbCache(path=path, clock=clock)
        modids = ["mod_a", "mod_b", "mod_c", "mod_d"]
        hits   = [m for m in modids if c2.get(m) is not None]
        misses = [m for m in modids if c2.get(m) is None]
        assert hits == ["mod_a", "mod_b"]
        assert misses == ["mod_c", "mod_d"]
