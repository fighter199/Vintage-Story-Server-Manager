"""
core/utils.py — Miscellaneous utilities with no UI dependencies.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import zipfile
from datetime import datetime

from .constants import LOG
from .parsers import parse_json5_ish


# -----------------------------------------------------------------------
# Port availability check
# -----------------------------------------------------------------------
def is_port_free(port: int, host: str = "0.0.0.0") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def find_vs_port(server_dir: str) -> int:
    """Read VS port from serverconfig.json, default 42420."""
    candidate = os.path.join(server_dir, "serverconfig.json")
    try:
        with open(candidate, "r", encoding="utf-8", errors="replace") as f:
            data = parse_json5_ish(f.read())
        port = data.get("Port") or data.get("port")
        if isinstance(port, int) and 1 <= port <= 65535:
            return port
        if isinstance(port, str) and port.isdigit():
            return int(port)
    except (OSError, ValueError, Exception):
        pass
    return 42420


# -----------------------------------------------------------------------
# OS file manager
# -----------------------------------------------------------------------
def open_in_file_manager(path: str) -> bool:
    if not path:
        return False
    if not os.path.exists(path):
        return False
    is_file = os.path.isfile(path)
    try:
        if sys.platform.startswith("win"):
            if is_file:
                subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])
            else:
                os.startfile(os.path.normpath(path))   # type: ignore
            return True
        elif sys.platform == "darwin":
            folder = os.path.dirname(path) if is_file else path
            subprocess.Popen(["open", folder])
            return True
        else:
            folder = os.path.dirname(path) if is_file else path
            subprocess.Popen(["xdg-open", folder])
            return True
    except Exception as e:
        LOG.warning("open_in_file_manager(%r) failed: %s", path, e)
        return False


# -----------------------------------------------------------------------
# Mod filename sanitisation
# -----------------------------------------------------------------------
_RE_HASH_SUFFIX = re.compile(r"[_\-][A-Fa-f0-9]{32,}(?=\.[A-Za-z0-9]{1,5}$)")


def strip_hash_suffix(name: str) -> str:
    return _RE_HASH_SUFFIX.sub("", name)


def sanitize_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._\-+]", "_", name)


def _is_human_readable_modid(value) -> bool:
    if not value:
        return False
    s = str(value).strip()
    if not s or s.isdigit():
        return False
    return bool(re.search(r"[A-Za-z]", s))


def clean_mod_filename(url, declared=None, modid=None, version=None, name=None) -> str:
    """Pick the cleanest mod filename from available metadata."""
    ext = ".zip"
    if declared:
        base = os.path.basename(declared)
        _, e = os.path.splitext(base)
        if e.lower() in (".zip", ".cs", ".dll"):
            ext = e.lower()
        blob = strip_hash_suffix(base)
        if not re.search(r"[A-Fa-f0-9]{32,}", blob):
            return sanitize_filename(blob)
    if _is_human_readable_modid(modid) and version:
        return sanitize_filename(f"{modid}_{version}{ext}")
    if url:
        base = os.path.basename(url.split("?")[0])
        _, e = os.path.splitext(base)
        if e.lower() in (".zip", ".cs", ".dll"):
            ext = e.lower()
        return sanitize_filename(strip_hash_suffix(base)) or f"mod{ext}"
    if name:
        return sanitize_filename(name) + ext
    return f"mod_{datetime.now().strftime('%Y%m%d%H%M%S')}{ext}"


# -----------------------------------------------------------------------
# Human-readable size
# -----------------------------------------------------------------------
def fmt_size(n) -> str:
    """Render a byte count as a human-readable string.

    - Negative or zero → '0 B'
    - Non-numeric input → '?' (used when we don't know a file size yet)
    - B has no decimal; KB/MB/GB get one decimal.
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    v = float(n)
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    if i == 0:
        return f"{int(v)} {units[i]}"
    return f"{v:.1f} {units[i]}"


# -----------------------------------------------------------------------
# Backup: zip + verify + restore
# -----------------------------------------------------------------------
def backup_world_to_zip(src: str, dst: str, progress_cb=None, cancel_flag=None) -> str:
    if not os.path.isdir(src):
        raise RuntimeError(f"Source folder does not exist: {src}")
    all_files = []
    for root, _, files in os.walk(src):
        for f in files:
            all_files.append(os.path.join(root, f))
    total = len(all_files) or 1
    part = dst + ".part"
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=True) as zf:
            for i, full in enumerate(all_files, 1):
                if cancel_flag and cancel_flag():
                    raise RuntimeError("Backup cancelled.")
                try:
                    arc = os.path.relpath(full, start=os.path.dirname(src))
                    zf.write(full, arcname=arc)
                except (OSError, PermissionError) as e:
                    LOG.warning("Backup skip %s: %s", full, e)
                if progress_cb:
                    try:
                        progress_cb(i, total)
                    except Exception:
                        pass
        # Integrity check (improvement #7)
        with zipfile.ZipFile(part, "r") as zf:
            bad = zf.testzip()
            if bad:
                raise RuntimeError(f"Backup ZIP integrity check failed on: {bad}")
        os.replace(part, dst)
        return dst
    except Exception:
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        raise


def restore_backup_zip(zip_path: str, dst_world_folder: str,
                       archive_existing: bool = True):
    if not os.path.isfile(zip_path):
        raise RuntimeError(f"Not a file: {zip_path}")
    if not zipfile.is_zipfile(zip_path):
        raise RuntimeError(f"Not a valid zip: {zip_path}")
    if not dst_world_folder:
        raise RuntimeError("No destination world folder given.")
    parent = os.path.dirname(os.path.abspath(dst_world_folder)) or os.getcwd()
    archived = None
    if archive_existing and os.path.isdir(dst_world_folder):
        import shutil
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        archived = os.path.join(parent, f"pre-restore-{ts}.zip")
        backup_world_to_zip(dst_world_folder, archived)
        try:
            shutil.rmtree(dst_world_folder)
        except OSError as e:
            raise RuntimeError(f"Could not remove current world: {e}")
    try:
        os.makedirs(dst_world_folder, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            dest_root = os.path.abspath(parent)
            for member in zf.namelist():
                target = os.path.abspath(os.path.join(parent, member))
                if not target.startswith(dest_root + os.sep) \
                        and target != dest_root:
                    raise RuntimeError(f"Archive contains unsafe path: {member}")
            zf.extractall(parent)
    except Exception as e:
        raise RuntimeError(f"Extract failed: {e}")
    return archived


# -----------------------------------------------------------------------
# HiDPI (Windows)
# -----------------------------------------------------------------------
def enable_windows_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            return
        except Exception:
            pass
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
