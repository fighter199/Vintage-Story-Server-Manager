"""
core/utils.py — Miscellaneous utilities with no UI dependencies.
"""
from __future__ import annotations

import os
import re
import shutil
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
    # On Windows, SO_REUSEADDR lets bind() succeed even while another
    # socket is actively LISTENING on the port — which made this check
    # always report "free". SO_EXCLUSIVEADDRUSE restores the strict
    # behaviour. On POSIX, SO_REUSEADDR is what we want: it avoids
    # false "in use" results from sockets lingering in TIME_WAIT.
    try:
        if sys.platform.startswith("win"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except (AttributeError, OSError):
        pass
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
    except Exception:
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


def open_in_editor(path: str) -> bool:
    """Open a file with the system's default app for it (a text editor
    for .json on most setups). Returns False if that couldn't start."""
    if not path or not os.path.isfile(path):
        return False
    try:
        if sys.platform.startswith("win"):
            try:
                os.startfile(os.path.normpath(path))      # type: ignore
            except OSError:                               # no association
                subprocess.Popen(["notepad", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-t", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception as e:
        LOG.warning("open_in_editor(%r) failed: %s", path, e)
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
# Level 1: on a real savegame, ~3x faster than level 6 for files only ~3%
# larger (70 vs 23 MB/s) — what matters when a world is tens of GB.
BACKUP_COMPRESSLEVEL = 1
_COPY_CHUNK = 4 * 1024 * 1024


def _zip_file(zf: zipfile.ZipFile, full: str, arcname: str,
              on_bytes=None, cancel_flag=None) -> None:
    """zf.write() in chunks, reporting bytes as they go and honouring
    cancel mid-file (a world is often one multi-GB .vcdbs)."""
    zinfo = zipfile.ZipInfo.from_file(full, arcname)
    if zinfo.is_dir():
        zf.writestr(zinfo, b"")
        return
    zinfo.compress_type = zipfile.ZIP_DEFLATED
    zinfo._compresslevel = BACKUP_COMPRESSLEVEL
    with open(full, "rb") as src, zf.open(zinfo, "w") as dst:
        while True:
            if cancel_flag and cancel_flag():
                raise RuntimeError("Backup cancelled.")
            chunk = src.read(_COPY_CHUNK)
            if not chunk:
                break
            dst.write(chunk)
            if on_bytes:
                on_bytes(len(chunk))


def backup_world_to_zip(src: str, dst: str, progress_cb=None, cancel_flag=None) -> str:
    """Zip the world folder `src` to `dst` (atomically, via dst.part).
    progress_cb(done_bytes, total_bytes) is called as data is written."""
    if not os.path.isdir(src):
        raise RuntimeError(f"Source folder does not exist: {src}")
    all_files = []
    total = 0
    for root, _, files in os.walk(src):
        for f in files:
            full = os.path.join(root, f)
            all_files.append(full)
            try:
                total += os.path.getsize(full)
            except OSError:
                pass
    total = total or 1
    done = 0

    def on_bytes(n):
        nonlocal done
        done += n
        if progress_cb:
            try:
                progress_cb(done, total)
            except Exception:
                pass

    part = dst + ".part"
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=BACKUP_COMPRESSLEVEL,
                             allowZip64=True) as zf:
            for full in all_files:
                if cancel_flag and cancel_flag():
                    raise RuntimeError("Backup cancelled.")
                try:
                    arc = os.path.relpath(full, start=os.path.dirname(src))
                    _zip_file(zf, full, arc, on_bytes, cancel_flag)
                except (OSError, PermissionError) as e:
                    LOG.warning("Backup skip %s: %s", full, e)
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


def backup_single_file_to_zip(src_file: str, dst: str,
                              arcname: str | None = None) -> str:
    """Zip one file (e.g. a server-generated /genbackup savegame) into
    `dst`, with the same integrity check + atomic `.part` rename that
    backup_world_to_zip uses. `arcname` controls the stored name/path
    inside the archive (defaults to the file's own basename)."""
    if not os.path.isfile(src_file):
        raise RuntimeError(f"Source file does not exist: {src_file}")
    part = dst + ".part"
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=BACKUP_COMPRESSLEVEL,
                             allowZip64=True) as zf:
            _zip_file(zf, src_file, arcname or os.path.basename(src_file))
        with zipfile.ZipFile(part, "r") as zf:
            bad = zf.testzip()
            if bad:
                raise RuntimeError(
                    f"Backup ZIP integrity check failed on: {bad}")
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
    """Restore a world from a backup zip into `dst_world_folder`.

    Handles both archive layouts:
      - VSSM's own backups: a single top-level folder (named after the
        world folder as it was when the backup was taken) containing
        the world files
      - bare zips: world files at the archive root

    The top-level folder name inside the archive does NOT have to match
    the configured world folder — contents always land in
    `dst_world_folder`. (Previously the archive was extracted into the
    world folder's parent under whatever name the zip carried, so a
    backup taken from a differently-named world folder recreated the
    old folder and left the configured one empty.)

    The archive is fully extracted to a temp dir and validated BEFORE
    the current world is archived/replaced, so a corrupt zip can never
    destroy the existing world.
    """
    if not os.path.isfile(zip_path):
        raise RuntimeError(f"Not a file: {zip_path}")
    if not zipfile.is_zipfile(zip_path):
        raise RuntimeError(f"Not a valid zip: {zip_path}")
    if not dst_world_folder:
        raise RuntimeError("No destination world folder given.")
    dst_world_folder = os.path.abspath(dst_world_folder)
    parent = os.path.dirname(dst_world_folder) or os.getcwd()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp_extract = os.path.join(parent, f".restore-tmp-{ts}")

    # 1. Extract to a temp dir first — the whole archive is unpacked
    #    and checked before anything touches the current world.
    try:
        os.makedirs(tmp_extract, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            dest_root = os.path.abspath(tmp_extract)
            for member in zf.namelist():
                target = os.path.abspath(os.path.join(tmp_extract, member))
                if not target.startswith(dest_root + os.sep) \
                        and target != dest_root:
                    raise RuntimeError(
                        f"Archive contains unsafe path: {member}")
            zf.extractall(tmp_extract)
    except Exception as e:
        shutil.rmtree(tmp_extract, ignore_errors=True)
        raise RuntimeError(f"Extract failed: {e}")

    try:
        # 2. Locate the world root inside the extraction. Exactly one
        #    top-level directory → our layout; anything else → world
        #    files live at the archive root.
        entries = os.listdir(tmp_extract)
        if not entries:
            raise RuntimeError("Archive is empty.")
        if len(entries) == 1 and os.path.isdir(
                os.path.join(tmp_extract, entries[0])):
            src_root = os.path.join(tmp_extract, entries[0])
        else:
            src_root = tmp_extract

        # 3. Archive, then remove, the current world.
        archived = None
        if os.path.isdir(dst_world_folder):
            if archive_existing:
                archived = os.path.join(parent, f"pre-restore-{ts}.zip")
                backup_world_to_zip(dst_world_folder, archived)
            try:
                shutil.rmtree(dst_world_folder)
            except OSError as e:
                raise RuntimeError(f"Could not remove current world: {e}")

        # 4. Move the restored world into place under the CONFIGURED
        #    folder name — the name inside the archive doesn't matter.
        shutil.move(src_root, dst_world_folder)
        return archived
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Restore failed: {e}")
    finally:
        shutil.rmtree(tmp_extract, ignore_errors=True)


# -----------------------------------------------------------------------
# HiDPI (Windows)
# -----------------------------------------------------------------------
def enable_windows_dpi_awareness():
    """Render at the display's real DPI instead of being bitmap-stretched
    (blurry) by Windows. Must run before the Tk root is created.

    System-DPI awareness (1), the same as IDLE uses: Tk 8.6 can't rescale
    a window when per-monitor awareness (2) reports a DPI change, so that
    mode would leave text the wrong size after moving between monitors."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            return
        except Exception:
            pass
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
