"""
core/parsers.py — Pure parsing functions (all unit-testable, no UI deps).

Covers:
  - Server log-line classification (error / warn / chat / player / etc.)
  - Player join / leave / list event extraction
  - Role response parsing
  - JSON5-ish parser (comment + trailing-comma stripping)
  - Cron-style schedule parsing
  - Chat command detection (NEW — for the Custom Commands feature)
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

# -----------------------------------------------------------------------
# Log-line classification regexes
# -----------------------------------------------------------------------
_RE_LEVEL_ERROR = re.compile(r"\[(?:ERROR|FATAL|EXCEPTION)\]", re.I)
_RE_LEVEL_WARN  = re.compile(r"\[WARN(?:ING)?\]", re.I)
_RE_CHAT        = re.compile(r"\[(?:CHAT|Chat)\]|\[Server\s+Chat\]")
_RE_TIMESTAMP   = re.compile(
    r"^\s*\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\s+\d{1,2}:\d{2}:\d{2}\s*")
_RE_LOG_PREFIX  = re.compile(
    r"^\s*\[(?:Server\s+)?(?:Notification|Event|Warning|Info|Error|Chat|"
    r"Server Event|Debug|Audit)\]\s*",
    re.I)

# Chat line pattern — matches "<PlayerName> message text"
# Vintage Story chat lines look like: [Server Chat] <alice> hello world
_RE_CHAT_MESSAGE = re.compile(
    r"<([A-Za-z0-9_\-\.]+)>\s*(.*)",
    re.I)

def strip_log_prefix(line: str) -> str:
    s = _RE_TIMESTAMP.sub("", line)
    s = _RE_LOG_PREFIX.sub("", s)
    return s


def classify_line(line: str) -> str:
    if _RE_LEVEL_ERROR.search(line):
        return "error"
    if _RE_LEVEL_WARN.search(line):
        return "warn"
    if _RE_CHAT.search(line) or line.lstrip().startswith("<"):
        return "chat"
    low = line.lower()
    if ("exception" in low and ":" in low) or " error:" in low \
            or low.startswith("fatal"):
        return "error"
    if ("[server event]" in low or "[audit]" in low) \
            or "joined" in low or " joins" in low \
            or "left the game" in low \
            or re.search(r"player\s+\S+\s+left", low):
        if "joined" in low or "joins" in low or "left" in low:
            return "player"
    if "server ready" in low or " started" in low or "saved" in low:
        return "success"
    return "info"


# -----------------------------------------------------------------------
# Chat command detection (NEW)
# -----------------------------------------------------------------------
def parse_chat_message(line: str):
    """Extract (player_name, message_text) from a chat line, or (None, None).

    Vintage Story chat lines appear as:
        [Server Chat] <alice> !warp spawn
        [Chat] <Bob> hello
        <charlie> some text

    Returns (player_name: str, message: str) or (None, None).
    """
    stripped = strip_log_prefix(line)
    m = _RE_CHAT_MESSAGE.search(stripped)
    if m:
        return m.group(1), m.group(2).strip()
    return None, None


# -----------------------------------------------------------------------
# Player event parsing
# -----------------------------------------------------------------------
_RE_JOIN_PATTERNS = [
    re.compile(r"^\s*([A-Za-z0-9_\-\.]+)\s+\[[^\]]+\]:\d+\s+joins\.?\s*$"),
    re.compile(r"""Player ['"]([^'"]+)['"] has joined the game""", re.I),
    re.compile(r"\[Audit\]\s+([A-Za-z0-9_\-\.]+)\s+joined\b", re.I),
    re.compile(r"^\s*([A-Za-z0-9_\-\.]+)\s+joined\b"),
    re.compile(r"^\s*([A-Za-z0-9_\-\.]+)\s+has joined the game", re.I),
    re.compile(r"^\s*([A-Za-z0-9_\-\.]+)\s+joins\.?\s*$"),
]
_RE_LEAVE_PATTERNS = [
    re.compile(r"^\s*Player\s+([A-Za-z0-9_\-\.]+)\s+left\.?\s*$"),
    re.compile(r"""Player ['"]([^'"]+)['"] has left the game""", re.I),
    re.compile(r"\[Audit\]\s+Client\s+([A-Za-z0-9_\-\.]+)\s+disconnected", re.I),
    re.compile(r"^\s*Client\s+([A-Za-z][A-Za-z0-9_\-\.]*)\s+disconnected", re.I),
    re.compile(r"^\s*([A-Za-z0-9_\-\.]+)\s+left\.?\s*$"),
    re.compile(r"^\s*([A-Za-z0-9_\-\.]+)\s+has left the game", re.I),
]
_RE_LIST_CLIENTS = re.compile(
    r"(?:connected players|players online|online players|list of clients)[^:]*:\s*(.+)",
    re.I)
_RE_PLAYER_ROLE = re.compile(
    r"Player\s+has\s+role\s+([A-Za-z][A-Za-z0-9_\-]*)", re.I)


def parse_player_event(line: str):
    """Return (event, name) — event is 'join', 'leave', 'list', or None."""
    stripped = strip_log_prefix(line)
    for pat in _RE_JOIN_PATTERNS:
        m = pat.search(stripped)
        if m:
            return ("join", m.group(1))
    for pat in _RE_LEAVE_PATTERNS:
        m = pat.search(stripped)
        if m:
            return ("leave", m.group(1))
    m = _RE_LIST_CLIENTS.search(stripped)
    if m:
        return ("list", m.group(1).strip().rstrip("."))
    return (None, None)


def split_client_list(payload: str):
    if not payload or payload.lower() in ("none", "no one", "-"):
        return []
    names = [n.strip() for n in re.split(r"[,;]\s*|\s{2,}", payload)
             if n.strip() and not n.strip().startswith("(")]
    return [n for n in names if re.match(r"^[A-Za-z0-9_\-\.]{2,}$", n)]


def parse_role_response(line: str):
    """Parse 'Player has role X' → role string (lowercase), or None."""
    stripped = strip_log_prefix(line)
    m = _RE_PLAYER_ROLE.search(stripped)
    if m:
        return m.group(1).lower()
    return None


# -----------------------------------------------------------------------
# JSON5-ish parser (strips comments + trailing commas, single-quoted strings)
# -----------------------------------------------------------------------
def parse_json5_ish(text: str):
    out = []
    i = 0
    n = len(text)
    in_str = False
    str_quote = ''
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == '\\' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == str_quote:
                in_str = False
            i += 1
            continue
        if c in ('"', "'"):
            in_str = True
            str_quote = c
            out.append(c)
            i += 1
            continue
        if c == '/' and i + 1 < n:
            nxt = text[i + 1]
            if nxt == '/':
                j = text.find('\n', i + 2)
                i = n if j < 0 else j
                continue
            if nxt == '*':
                j = text.find('*/', i + 2)
                i = n if j < 0 else j + 2
                continue
        out.append(c)
        i += 1
    stripped = ''.join(out)
    stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    stripped = _convert_single_quoted_strings(stripped)
    return json.loads(stripped)


def _convert_single_quoted_strings(text: str) -> str:
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            out.append(c)
            i += 1
            while i < n:
                cc = text[i]
                out.append(cc)
                if cc == '\\' and i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                i += 1
                if cc == '"':
                    break
            continue
        if c == "'":
            out.append('"')
            i += 1
            while i < n:
                cc = text[i]
                if cc == '\\' and i + 1 < n:
                    out.append(cc)
                    out.append(text[i + 1])
                    i += 2
                    continue
                if cc == "'":
                    out.append('"')
                    i += 1
                    break
                if cc == '"':
                    out.append('\\"')
                    i += 1
                    continue
                out.append(cc)
                i += 1
            continue
        out.append(c)
        i += 1
    return ''.join(out)


# -----------------------------------------------------------------------
# Cron-style schedule parsing
# -----------------------------------------------------------------------
_WEEKDAY_MAP = {
    "sun": 6, "mon": 0, "tue": 1, "wed": 2,
    "thu": 3, "fri": 4, "sat": 5,
}


def parse_cron_expr(expr: str):
    """Parse 'HH:MM', 'DAY HH:MM', or comma/semicolon-separated list.
    Returns list of (weekday_or_None, hour, minute)."""
    entries = []
    for part in re.split(r"[,;]", expr):
        part = part.strip()
        if not part:
            continue
        tokens = part.split()
        weekday = None
        if len(tokens) == 2:
            day_tok = tokens[0].lower()
            if day_tok not in _WEEKDAY_MAP:
                raise ValueError(f"Unknown weekday: {tokens[0]!r}")
            weekday = _WEEKDAY_MAP[day_tok]
            time_tok = tokens[1]
        elif len(tokens) == 1:
            time_tok = tokens[0]
        else:
            raise ValueError(f"Cannot parse schedule entry: {part!r}")
        m = re.match(r"^(\d{1,2}):(\d{2})$", time_tok)
        if not m:
            raise ValueError(f"Cannot parse time: {time_tok!r}")
        h, mn = int(m.group(1)), int(m.group(2))
        if not (0 <= h <= 23 and 0 <= mn <= 59):
            raise ValueError(f"Out of range time: {time_tok!r}")
        entries.append((weekday, h, mn))
    if not entries:
        raise ValueError("no valid schedule entries")
    return entries


def seconds_until_next(entries, now=None) -> int:
    if now is None:
        now = datetime.now()
    best = None
    for (weekday, h, mn) in entries:
        for day_offset in range(0, 8):
            candidate = (now + timedelta(days=day_offset)).replace(
                hour=h, minute=mn, second=0, microsecond=0)
            if candidate <= now:
                continue
            if weekday is not None and candidate.weekday() != weekday:
                continue
            if best is None or candidate < best:
                best = candidate
            break
    if best is None:
        return 3600
    return max(1, int((best - now).total_seconds()))


# -----------------------------------------------------------------------
# Version comparison
# -----------------------------------------------------------------------
try:
    from packaging.version import Version as _PkgVersion   # type: ignore
    _HAVE_PKG_VERSION = True
except ImportError:
    _HAVE_PKG_VERSION = False


def _parse_version_fallback(s: str) -> tuple:
    """Parse a version string into a comparable tuple without `packaging`.

    Splits the string into a "release prefix" (numeric, dot-separated)
    and a "pre-release tail" (everything after the first hyphen, plus
    or non-numeric segment).

    Returns:
        (release_components: tuple[int, ...],
         is_release: int,         # 1 for clean release, 0 for pre-release
         prerelease_components: tuple)

    With this shape, `(0, 6, 0)` sorts above `(0, 6, 0)` + prerelease
    because the second element (`is_release`) breaks the tie in favour
    of the clean release. Among prereleases, the prerelease_components
    tuple provides the next ordering level.

    Examples:
        "0.6.0"        -> ((0, 6, 0), 1, ())
        "0.6.0-rc.2"   -> ((0, 6, 0), 0, ("rc", 2))
        "0.6.0-rc.1"   -> ((0, 6, 0), 0, ("rc", 1))
        "0.6.0-pre.1"  -> ((0, 6, 0), 0, ("pre", 1))
        "0.5.7"        -> ((0, 5, 7), 1, ())
    """
    # Split off the prerelease/build tail at the first non-numeric/dot.
    # We allow the leading 'v' some maintainers use.
    s = s.lstrip("vV")
    # Find the boundary: the first occurrence of '-', '+', or any
    # alphabetical character that isn't part of a numeric segment.
    m = re.match(r"^([0-9]+(?:\.[0-9]+)*)(.*)$", s)
    if not m:
        # Couldn't even parse a numeric prefix — treat as ((), 0, (s,))
        return ((), 0, (s,))
    head, tail = m.group(1), m.group(2)
    release = tuple(int(x) for x in head.split("."))
    if not tail:
        return (release, 1, ())  # clean release
    # Tail is something like "-rc.2", "-pre.1", "+build5", etc.
    # Strip leading separator and split on dots, hyphens, plus signs.
    tail_clean = tail.lstrip("-+.")
    pre_parts: list = []
    for part in re.split(r"[\.\-+]", tail_clean):
        if not part:
            continue
        # Try int; fall back to lowercase string. We compare strings
        # naturally (alphabetical), with "alpha" < "beta" < "rc" — which
        # happens to match how PEP 440 ranks them too. Mixed types in
        # tuples don't compare in Python 3, so wrap each in (kind, val).
        if part.isdigit():
            pre_parts.append((1, int(part)))
        else:
            pre_parts.append((0, part.lower()))
    return (release, 0, tuple(pre_parts))


def version_key(s: str) -> tuple:
    """Return a tuple suitable for `sorted(..., key=version_key, reverse=True)`.

    The tuple is comparable in the natural sense — i.e. for two version
    strings A and B, `version_key(A) > version_key(B)` iff A is strictly
    newer than B. Falls back to a (release, is_release, prerelease)
    triple when `packaging` isn't installed; that fallback handles
    pre-release semantics correctly (a clean `0.6.0` sorts above its
    `0.6.0-rc.2` and `0.6.0-pre.1` siblings).

    Empty / None / unparseable values sort *lowest* (oldest), so they
    end up at the bottom of a `reverse=True` sort.

    The returned shape is always `(tier, payload)`:
      tier 2 = parsed by `packaging`           (newest tier)
      tier 1 = component-triple fallback
      tier 0 = unparseable / empty             (oldest tier)
    Higher tier always sorts higher; within a tier the payload is
    self-consistent (`Version` vs `Version`, or tuple vs tuple).
    """
    if not s:
        return (0, ())
    if _HAVE_PKG_VERSION:
        try:
            return (2, _PkgVersion(s))
        except Exception:
            pass
    try:
        return (1, _parse_version_fallback(s))
    except Exception:
        return (0, ())


def version_is_newer(remote: str, local: str) -> bool:
    if not remote:
        return False
    if not local:
        return True
    if _HAVE_PKG_VERSION:
        try:
            return _PkgVersion(remote) > _PkgVersion(local)
        except Exception:
            pass
    try:
        return _parse_version_fallback(remote) > _parse_version_fallback(local)
    except Exception:
        return remote != local
