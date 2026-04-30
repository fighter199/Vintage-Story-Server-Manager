"""
core/chat_log.py — Chat-log model with per-group separation.

Vintage Story 1.20+ servers tag every chat line with a group ID:

    [Server Chat] 0  | DerelictDawn: Oh
    [Server Chat] 10 | Fighter199: testing chat long

Group `0` is general (everyone). Other IDs are private/named groups
that the server creates when players form a chat circle.

This module gives VSSM:

  - `parse_chat_with_group(line)` — extends parse_chat_message to also
    return the group ID. Pure function, no state.

  - `ChatLogStore` — owns the in-memory model: per-group ring buffers
    of chat entries plus a dict of user-assigned group names. Loads
    and saves history to disk via injected I/O callbacks (so the unit
    tests can hand it a dict-as-disk and check what was written).

The store is intentionally I/O-agnostic: the host (`ServerManagerApp`)
provides paths and atomic-write helpers via the constructor. This
keeps the store unit-testable without any filesystem touching.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

# Per-group ring-buffer cap. 500 lines × maybe 100 chars × ~20 groups
# = ~1 MB worst-case in memory. Disk write is much smaller in practice
# because most groups have far fewer entries than this.
DEFAULT_MAX_PER_GROUP = 500

# -----------------------------------------------------------------------
# Chat line parsing — extended to also capture the group ID
# -----------------------------------------------------------------------
# Mirrors the colon-form regex in core/parsers.py but captures the
# leading group ID number too. Anchored to the colon-form because the
# group ID only appears in that shape; the older angle-bracket form
# never had one.
_RE_CHAT_GROUPED = re.compile(
    r"^\s*(\d+)\s*\|\s*"                       # required "<digits> | "
    r"([A-Za-z0-9_\-\.]+)\s*:\s*(.*)$",        # name : message
    re.I,
)


def parse_chat_with_group(line: str, strip_fn: Callable[[str], str] | None = None):
    """Return (group_id: str, player: str, message: str) or
    (None, None, None) if the line isn't a chat line in the
    grouped colon format.

    Parameters
    ----------
    line : str
        The raw log line (with timestamps and tags still attached).
    strip_fn : optional callable
        The host's `strip_log_prefix` function. Injected to avoid an
        import cycle and to let tests substitute a no-op.

    Notes
    -----
    The angle-bracket form ("[Server Chat] <Alice> hello") doesn't
    carry a group ID, so it's not handled here — only the modern
    colon form. Callers that need to handle both formats should fall
    back to the existing `parse_chat_message` for angle-bracket lines.
    """
    if not line:
        return (None, None, None)
    # Confirm it's a chat line — gate on the [Server Chat] / [Chat] tag
    # so plain "1 | Foo: bar" lines don't false-positive.
    if "[Chat]" not in line and "[CHAT]" not in line and "Server Chat" not in line:
        return (None, None, None)
    s = strip_fn(line) if strip_fn else line
    m = _RE_CHAT_GROUPED.search(s)
    if not m:
        return (None, None, None)
    return m.group(1), m.group(2), m.group(3).strip()


# -----------------------------------------------------------------------
# Entry + store
# -----------------------------------------------------------------------
@dataclass
class ChatEntry:
    """A single line of chat. Stored in per-group ring buffers."""
    timestamp: float
    player:    str
    message:   str

    def to_dict(self) -> dict:
        return {"ts": self.timestamp,
                "p":  self.player,
                "m":  self.message}

    @classmethod
    def from_dict(cls, d: dict) -> "ChatEntry":
        return cls(
            timestamp=float(d.get("ts", 0)),
            player=str(d.get("p", "")),
            message=str(d.get("m", "")),
        )


class ChatLogStore:
    """In-memory chat log with per-group separation.

    Parameters
    ----------
    load_history : callable() -> dict
        Returns the persisted blob (the wire format below). Called once
        on construction. Returning None or {} is treated as "no prior
        history".
    save_history : callable(dict) -> None
        Persists the wire format. The store calls this from `flush()`,
        not on every append (to avoid I/O storms during chat bursts).
    max_per_group : int
        Ring-buffer cap per group. When a group hits this, the oldest
        entries are evicted. Default 500.

    Wire format (passed to save_history / received from load_history)::

        {
          "version": 1,
          "groups":  { "<gid>": [ {"ts":…, "p":…, "m":…}, … ], … },
          "names":   { "<gid>": "<user-assigned label>", … }
        }
    """

    WIRE_VERSION = 1

    def __init__(
        self,
        load_history: Callable[[], dict] | None = None,
        save_history: Callable[[dict], None] | None = None,
        max_per_group: int = DEFAULT_MAX_PER_GROUP,
    ) -> None:
        self._save = save_history
        self._max  = max(10, int(max_per_group))
        # gid (str) -> list[ChatEntry], oldest first
        self._groups: dict[str, list[ChatEntry]] = {}
        # gid (str) -> user-assigned display name (e.g. "Builders chat")
        self._names: dict[str, str] = {}
        # True if there's anything not yet flushed to disk
        self._dirty: bool = False
        if load_history is not None:
            try:
                self._load_blob(load_history() or {})
            except Exception:
                # Corrupt history shouldn't kill the app; start clean.
                self._groups.clear()
                self._names.clear()

    # ------------------------------------------------------------------
    # Append + read
    # ------------------------------------------------------------------
    def append(self, group_id: str, player: str, message: str,
               now: float | None = None) -> None:
        """Append a chat entry to its group's ring buffer.
        Marks the store dirty so the next flush() persists it."""
        if not group_id or not player:
            return
        gid = str(group_id)
        if now is None:
            now = time.time()
        buf = self._groups.setdefault(gid, [])
        buf.append(ChatEntry(now, player, message or ""))
        # Evict oldest until under the cap
        if len(buf) > self._max:
            del buf[: len(buf) - self._max]
        self._dirty = True

    def entries(self, group_id: str) -> list[ChatEntry]:
        """Return a (live) reference to the group's buffer.
        Callers must not mutate; use copy() if they need to."""
        return self._groups.get(str(group_id), [])

    def all_entries_sorted(self) -> list[tuple[str, ChatEntry]]:
        """Return [(group_id, ChatEntry)] across every group, sorted
        chronologically. Used by the "All" subtab."""
        merged: list[tuple[str, ChatEntry]] = []
        for gid, buf in self._groups.items():
            for e in buf:
                merged.append((gid, e))
        merged.sort(key=lambda pair: pair[1].timestamp)
        return merged

    def known_group_ids(self) -> list[str]:
        """Return every group ID we've ever seen, OR have a name for,
        sorted with `0` (general) first and the rest by numeric ID."""
        ids = set(self._groups.keys()) | set(self._names.keys())
        def _key(g: str) -> tuple[int, int, str]:
            # "0" goes first, then numeric ascending, then non-numeric.
            if g == "0":
                return (0, 0, g)
            try:
                return (1, int(g), g)
            except (TypeError, ValueError):
                return (2, 0, g)
        return sorted(ids, key=_key)

    # ------------------------------------------------------------------
    # Group names
    # ------------------------------------------------------------------
    def display_name(self, group_id: str) -> str:
        """Return the user-assigned name if set, else a sensible default
        ("General" for group 0, "Group <id>" otherwise)."""
        gid = str(group_id)
        if gid in self._names and self._names[gid].strip():
            return self._names[gid]
        if gid == "0":
            return "General"
        return f"Group {gid}"

    def set_name(self, group_id: str, name: str) -> None:
        """Set or clear the user-assigned name for a group.
        Empty/whitespace clears, falling back to the default."""
        gid = str(group_id)
        clean = (name or "").strip()
        if clean:
            self._names[gid] = clean
        else:
            self._names.pop(gid, None)
        self._dirty = True

    def has_custom_name(self, group_id: str) -> bool:
        return str(group_id) in self._names and bool(
            self._names[str(group_id)].strip())

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def flush(self) -> bool:
        """Persist the in-memory state via the save callback. Returns
        True if a write was attempted, False if nothing was dirty
        (or no save callback was provided)."""
        if not self._dirty:
            return False
        if self._save is None:
            self._dirty = False
            return False
        blob = self._dump_blob()
        try:
            self._save(blob)
            self._dirty = False
            return True
        except Exception:
            # Keep dirty so a future flush retries
            return False

    def is_dirty(self) -> bool:
        return self._dirty

    def clear_group(self, group_id: str) -> None:
        """Drop all entries for a group (keeps the assigned name)."""
        gid = str(group_id)
        if gid in self._groups:
            del self._groups[gid]
            self._dirty = True

    def clear_all(self) -> None:
        """Drop every entry in every group (keeps assigned names)."""
        if self._groups:
            self._groups.clear()
            self._dirty = True

    # ------------------------------------------------------------------
    # Wire format helpers
    # ------------------------------------------------------------------
    def _dump_blob(self) -> dict:
        return {
            "version": self.WIRE_VERSION,
            "groups": {
                gid: [e.to_dict() for e in entries]
                for gid, entries in self._groups.items()
            },
            "names": dict(self._names),
        }

    def _load_blob(self, blob: dict) -> None:
        if not isinstance(blob, dict):
            return
        groups = blob.get("groups") or {}
        if isinstance(groups, dict):
            for gid, entries in groups.items():
                if not isinstance(entries, list):
                    continue
                buf: list[ChatEntry] = []
                for d in entries:
                    if not isinstance(d, dict):
                        continue
                    try:
                        buf.append(ChatEntry.from_dict(d))
                    except Exception:
                        continue
                # Honour the cap on load too — defensive against an
                # older save that used a higher cap.
                if len(buf) > self._max:
                    buf = buf[-self._max:]
                if buf:
                    self._groups[str(gid)] = buf
        names = blob.get("names") or {}
        if isinstance(names, dict):
            for gid, name in names.items():
                if isinstance(name, str) and name.strip():
                    self._names[str(gid)] = name.strip()
