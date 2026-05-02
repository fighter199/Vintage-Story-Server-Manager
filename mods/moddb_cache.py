"""
mods/moddb_cache.py — On-disk TTL cache for ModDB API responses.

The mod-update check fires `GET /api/mod/<modid>` once per local mod.
Each call costs an HTTPS round-trip; with 40+ mods that's noticeable
even on a fast connection, and worse when ModDB is sluggish or one
modid times out.

This cache stores each `get_mod` response on disk for a configurable
TTL (default 6 hours). Re-running the update check inside the TTL
window costs zero network bandwidth. After the TTL expires, entries
are silently re-fetched on next access.

Design notes:
  - **Pure logic.** No HTTP, no Tk, no settings module. The host
    constructs it with a path and a clock callable; tests use
    in-memory state with a fake clock.
  - **Per-modid entries.** Cache key is the modid string; value is
    the full `mod` dict from /api/mod/<modid>, plus a fetched-at
    timestamp.
  - **Atomic writes.** Save goes via a tmp file + os.replace, so a
    crashed VSSM mid-flush doesn't leave a half-written cache.
  - **Resilient load.** Corrupt or partial cache files are treated
    as "no cache" — no migration code, no version juggling, just
    start fresh.
  - **Bounded.** No automatic size cap; the cache only grows when
    you check updates for new mods, and entries naturally roll over
    via the TTL. A `clear()` method is provided.

Wire format::

    {
      "version":  1,
      "entries":  {
        "<modid>": {
          "fetched_at": <epoch seconds>,
          "data":       <the full /api/mod/<modid> 'mod' dict>,
        },
        ...
      }
    }
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Optional


WIRE_VERSION = 1
DEFAULT_TTL_SECS = 6 * 3600   # 6 hours


class ModDbCache:
    """On-disk TTL cache for ModDB get_mod responses.

    Parameters
    ----------
    path : str
        Where to read/write the cache file. None disables persistence
        (the cache becomes purely in-memory — useful for tests).
    ttl_secs : int
        How long an entry is considered fresh. Negative or zero
        disables freshness (every call misses).
    clock : optional callable() -> float
        Defaults to time.time. Override for tests.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        ttl_secs: int = DEFAULT_TTL_SECS,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._path  = path
        self._ttl   = int(ttl_secs)
        self._clock = clock or time.time
        # In-memory mirror: modid (str) -> {"fetched_at": float, "data": dict}
        self._entries: dict[str, dict] = {}
        self._dirty: bool = False
        self._load()

    # ------------------------------------------------------------------
    # Lookup + store
    # ------------------------------------------------------------------
    def get(self, modid: str) -> Optional[dict]:
        """Return the cached mod-dict if present AND still fresh,
        else None. None covers all of: missing entry, expired entry,
        corrupted entry, malformed modid argument."""
        if not modid:
            return None
        key = str(modid)
        entry = self._entries.get(key)
        if not entry:
            return None
        try:
            fetched_at = float(entry.get("fetched_at", 0))
        except (TypeError, ValueError):
            return None
        if self._ttl <= 0:
            return None
        if (self._clock() - fetched_at) > self._ttl:
            return None
        data = entry.get("data")
        return data if isinstance(data, dict) else None

    def put(self, modid: str, data: dict) -> None:
        """Store a freshly-fetched mod-dict, with timestamp = now.
        Marks the cache dirty so the next save() persists it."""
        if not modid or not isinstance(data, dict):
            return
        key = str(modid)
        self._entries[key] = {
            "fetched_at": self._clock(),
            "data":       data,
        }
        self._dirty = True

    def has_fresh(self, modid: str) -> bool:
        """True iff a fresh entry exists for this modid. Equivalent to
        `get(modid) is not None` but avoids returning the dict."""
        return self.get(modid) is not None

    def age_secs(self, modid: str) -> Optional[float]:
        """Return how old the cached entry for `modid` is, in seconds.
        None if no entry exists. Returns the age even if expired —
        useful for UI hints like "(cached 7h ago, refreshing…)"."""
        if not modid:
            return None
        entry = self._entries.get(str(modid))
        if not entry:
            return None
        try:
            return max(0.0, self._clock() - float(entry.get("fetched_at", 0)))
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save(self) -> bool:
        """Write the cache to disk if dirty. Returns True on a write
        attempt, False on no-op (clean) or no-path."""
        if not self._dirty:
            return False
        if not self._path:
            self._dirty = False
            return False
        blob = {
            "version": WIRE_VERSION,
            "entries": dict(self._entries),
        }
        tmp = self._path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(blob, f, ensure_ascii=False)
            os.replace(tmp, self._path)
            self._dirty = False
            return True
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return False

    def is_dirty(self) -> bool:
        return self._dirty

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def clear(self) -> None:
        """Drop every entry. Marks dirty so the next save() persists
        the empty state."""
        if self._entries:
            self._entries.clear()
            self._dirty = True

    def expire_stale(self) -> int:
        """Remove entries older than the TTL. Returns the count removed.
        The on-disk cache only shrinks after the next save()."""
        if self._ttl <= 0:
            return 0
        now = self._clock()
        stale = [k for k, e in self._entries.items()
                 if (now - float(e.get("fetched_at", 0))) > self._ttl]
        for k in stale:
            del self._entries[k]
        if stale:
            self._dirty = True
        return len(stale)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, modid: object) -> bool:
        if not isinstance(modid, str):
            return False
        return modid in self._entries

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self._path or not os.path.isfile(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                blob = json.load(f)
        except (OSError, ValueError):
            # Corrupt cache → start clean. Don't raise — the worst
            # case is we re-fetch everything once.
            return
        if not isinstance(blob, dict):
            return
        # Tolerate a missing or unknown version field — we only have
        # one wire format right now, but if a future version changes
        # the schema we'd want to refuse rather than misinterpret.
        ver = blob.get("version")
        if ver is not None and ver != WIRE_VERSION:
            return
        entries = blob.get("entries")
        if not isinstance(entries, dict):
            return
        for k, v in entries.items():
            if not isinstance(v, dict):
                continue
            if not isinstance(v.get("data"), dict):
                continue
            try:
                float(v.get("fetched_at", 0))
            except (TypeError, ValueError):
                continue
            self._entries[str(k)] = {
                "fetched_at": float(v["fetched_at"]),
                "data":       v["data"],
            }
