"""
core/crash_report.py — A report file for each unexpected server exit.

When the server process dies without being asked to, VSSM writes a
plain-text report to logs/crash-reports/: when and how it exited, how
long it had run, who was online, a one-line guess at the cause, any
crash log the game itself wrote since the start, and the last few
hundred console lines. The newest MAX_REPORTS are kept.
"""
from __future__ import annotations

import glob
import os
import re
import time
from datetime import datetime

MAX_REPORTS = 20
CONSOLE_LINES = 300
MAX_GAME_LOG_BYTES = 200_000

# A .NET exception line, e.g. "System.IO.IOException: Disk full" or
# "Unhandled exception. System.NullReferenceException: …".
_EXCEPTION_RE = re.compile(r"\b((?:[A-Z]\w*\.)+\w*Exception)\b(?::\s*(.*))?")
_FATAL_HINTS = ("crash", "fatal", "unhandled", "out of memory",
                "stack overflow", "could not", "failed to")


def reports_dir(log_folder: str) -> str:
    return os.path.join(log_folder, "crash-reports")


def summarize(lines: list[str]) -> str:
    """A one-line guess at what went wrong: the last .NET exception in
    the output, else the last line that sounds fatal, else the last
    line. Empty if there was no output."""
    for line in reversed(lines):
        m = _EXCEPTION_RE.search(line)
        if m:
            message = (m.group(2) or "").strip()
            return f"{m.group(1)}: {message}" if message else m.group(1)
    for line in reversed(lines):
        low = line.lower()
        if any(h in low for h in _FATAL_HINTS):
            return line.strip()
    for line in reversed(lines):
        if line.strip():
            return line.strip()
    return ""


def find_game_crash_logs(folders: list[str], since: float) -> list[str]:
    """Crash logs the game wrote (files with "crash" in the name in a
    Logs folder) modified at or after `since`, newest first."""
    found = set()
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        for pattern in ("*crash*", os.path.join("*", "*crash*")):
            for path in glob.glob(os.path.join(folder, pattern)):
                try:
                    if os.path.isfile(path) and os.path.getmtime(path) >= since:
                        found.add(os.path.abspath(path))
                except OSError:
                    continue
    return sorted(found, key=os.path.getmtime, reverse=True)


def _read_tail(path: str, limit: int = MAX_GAME_LOG_BYTES) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        return ("[…earlier part cut…]\n" + text) if size > limit else text
    except OSError as e:
        return f"(could not read: {e})"


def build_report(*, exit_code, started: float | None, ended: float,
                 console: list[str], players: list[str], game_logs: list[str],
                 app_version: str, server_path: str = "") -> str:
    uptime = ""
    if started:
        secs = int(ended - started)
        uptime = f"{secs // 3600}h {secs % 3600 // 60}m {secs % 60}s"
    cause = summarize(console) or "(no output)"
    out = [
        "VSSM crash report",
        "=================",
        f"Time:        {datetime.fromtimestamp(ended):%Y-%m-%d %H:%M:%S}",
        f"Exit code:   {exit_code if exit_code is not None else 'unknown'}",
        f"Uptime:      {uptime or 'unknown'}",
        f"Server:      {server_path or 'unknown'}",
        f"VSSM:        {app_version}",
        f"Online:      {', '.join(players) if players else 'nobody'}",
        f"Likely cause: {cause}",
        "",
    ]
    for path in game_logs:
        out += [f"--- Game crash log: {path} ---", _read_tail(path).rstrip(), ""]
    tail = console[-CONSOLE_LINES:]
    out += [f"--- Last {len(tail)} console lines ---", *tail, ""]
    return "\n".join(out)


def write_report(log_folder: str, text: str, now: float | None = None) -> str:
    """Save a report and prune old ones. Returns its path."""
    folder = reports_dir(log_folder)
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.fromtimestamp(now or time.time()).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(folder, f"crash-{stamp}.txt")
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"crash-{stamp}-{n}.txt")
        n += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    reports = sorted(glob.glob(os.path.join(folder, "crash-*.txt")),
                     key=os.path.getmtime)
    for old in reports[:-MAX_REPORTS]:
        try:
            os.remove(old)
        except OSError:
            pass
    return path
