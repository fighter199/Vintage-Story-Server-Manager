"""
core/processes.py — Find Vintage Story servers VSSM didn't start.

VSSM only knows about the server it launched itself. One started some
other way (a Windows service, a shortcut, a second VSSM) may be writing
the same savegame, so destructive actions — deleting chunks, restoring
a backup — check for one first.

A server matches when its command line mentions VintagestoryServer
(VintagestoryServer.exe, the Linux binary, or `dotnet
VintagestoryServer.dll`). If its --dataPath is visible and the world
folder isn't inside it, it's another server on the same machine and is
ignored; without a visible command line (Windows without psutil lists
names only) it's reported as "maybe".
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from typing import Callable, Iterable, Optional

from .constants import LOG

_SERVER_RE = re.compile(r"vintagestoryserver", re.I)


def default_data_path() -> str:
    """Where a server keeps its data when started without --dataPath."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "VintagestoryData")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/VintagestoryData")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "VintagestoryData")


def list_processes() -> list[tuple[int, str, bool]]:
    """(pid, command line or name, has_full_cmdline) for running
    processes. psutil when installed, else the platform's tools."""
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        out = []
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmd = p.info.get("cmdline") or []
                if cmd:
                    out.append((p.info["pid"], subprocess.list2cmdline(cmd), True))
                else:
                    out.append((p.info["pid"], p.info.get("name") or "", False))
            except Exception:
                continue
        return out
    try:
        if sys.platform.startswith("win"):
            res = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=10,
                                 creationflags=0x08000000)
            out = []
            for line in res.stdout.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 2 and parts[1].isdigit():
                    out.append((int(parts[1]), parts[0], False))
            return out
        res = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                             text=True, timeout=10)
        out = []
        for line in res.stdout.splitlines():
            pid, _, args = line.strip().partition(" ")
            if pid.isdigit():
                out.append((int(pid), args.strip(), True))
        return out
    except Exception as e:
        LOG.debug("process listing failed: %s", e)
        return []


def _data_path_from_cmdline(cmdline: str) -> Optional[str]:
    try:
        args = shlex.split(cmdline, posix=not sys.platform.startswith("win"))
    except ValueError:
        args = cmdline.split()
    for i, arg in enumerate(args):
        low = arg.lower()
        if low.startswith("--datapath="):
            return arg.split("=", 1)[1].strip('"')
        if low == "--datapath" and i + 1 < len(args):
            return args[i + 1].strip('"')
    return None


def _inside(path: str, folder: str) -> bool:
    try:
        path = os.path.normcase(os.path.abspath(path))
        folder = os.path.normcase(os.path.abspath(folder))
        return os.path.commonpath([path, folder]) == folder
    except ValueError:                       # different drives on Windows
        return False


def find_external_servers(world_folder: str, own_pids: Iterable[int] = (),
                          processes: Optional[Callable[[], list]] = None,
                          default_data: Optional[str] = None) -> list[dict]:
    """Servers that might be using `world_folder`, excluding VSSM's own.

    Each is {"pid", "cmdline", "certain"}: certain=True when its data
    path contains the world folder, False when that can't be told (no
    command line visible). Servers clearly using another data folder
    are left out."""
    skip = {os.getpid(), *own_pids}
    default_data = default_data or default_data_path()
    found = []
    for pid, cmdline, full in (processes or list_processes)():
        if pid in skip or not _SERVER_RE.search(cmdline or ""):
            continue
        if not full:
            found.append({"pid": pid, "cmdline": cmdline, "certain": False})
            continue
        data = _data_path_from_cmdline(cmdline) or default_data
        if world_folder and not _inside(world_folder, data):
            continue                          # a different server's data
        found.append({"pid": pid, "cmdline": cmdline, "certain": True})
    return found


def describe(servers: list[dict]) -> str:
    """One line per server for a warning dialog."""
    lines = []
    for s in servers[:5]:
        cmd = s["cmdline"] if len(s["cmdline"]) <= 90 else s["cmdline"][:87] + "…"
        lines.append(f"• PID {s['pid']}: {cmd}")
    if len(servers) > 5:
        lines.append(f"• …and {len(servers) - 5} more")
    return "\n".join(lines)
