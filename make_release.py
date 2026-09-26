#!/usr/bin/env python3
"""
make_release.py — Package VSSM for deployment.

Copies only what VSSM needs at runtime into Release/VSSM-<version>/ and
zips it as Release/VSSM-<version>.zip. Left out: the test suite
(tests/, run_tests.py), this script, bytecode caches, logs, and any
local settings / chat-log files.

    python make_release.py
"""
from __future__ import annotations

import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Everything the app imports or reads, plus user-facing docs/tools.
INCLUDE_FILES = ["VSSM.py", "vs_commands.json", "requirements.txt",
                 "README.md", "probe_stdin.py"]
INCLUDE_PACKAGES = ["core", "ui", "backup", "mods"]


def app_version() -> str:
    sys.path.insert(0, HERE)
    from core.constants import APP_VERSION
    return APP_VERSION


def _copy_package(name: str, dest_root: str) -> None:
    src = os.path.join(HERE, name)
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        rel = os.path.relpath(dirpath, HERE)
        os.makedirs(os.path.join(dest_root, rel), exist_ok=True)
        for fn in filenames:
            if fn.endswith(".py"):
                shutil.copy2(os.path.join(dirpath, fn),
                             os.path.join(dest_root, rel, fn))


def build(out_dir: str | None = None) -> str:
    version = app_version()
    name = f"VSSM-{version}"
    out_dir = out_dir or os.path.join(HERE, "Release")
    dest = os.path.join(out_dir, name)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    for fn in INCLUDE_FILES:
        shutil.copy2(os.path.join(HERE, fn), os.path.join(dest, fn))
    for pkg in INCLUDE_PACKAGES:
        _copy_package(pkg, dest)

    zip_path = os.path.join(out_dir, name + ".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _dirs, filenames in os.walk(dest):
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                zf.write(full, os.path.relpath(full, out_dir))
    return zip_path


if __name__ == "__main__":
    path = build(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"Built {path}")
