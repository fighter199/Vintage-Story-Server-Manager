#!/usr/bin/env python3
"""
make_release.py — Package VSSM for deployment.

Copies only what VSSM needs at runtime into Release/VSSM-<version>/ and
zips it as Release/VSSM-<version>.zip. Left out: the test suite
(tests/, run_tests.py), this script, bytecode caches, logs, and any
local settings / chat-log files.

    python make_release.py                  # build the folder + zip
    python make_release.py --publish Main   # ...and commit it to Main

--publish turns the release branch into exactly the release contents:
it adds one commit on top of that branch whose files are the release
folder (files that aren't part of the release, like the tests, are
removed there). The development branch and your working tree are not
touched. Push afterwards with `git push origin Main`.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Everything the app imports or reads, plus user-facing docs/tools.
INCLUDE_FILES = ["VSSM.py", "vs_commands_builtin.json", "requirements.txt",
                 "README.md", "probe_stdin.py", ".gitignore"]
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


def build(out_dir: str | None = None) -> tuple[str, str]:
    """Build the release folder and zip. Returns (folder, zip path)."""
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
                if fn == ".gitignore":
                    continue                 # only meaningful in git
                full = os.path.join(dirpath, fn)
                zf.write(full, os.path.relpath(full, out_dir))
    return dest, zip_path


def _git(*args: str, env=None) -> str:
    return subprocess.run(["git", "-C", HERE, *args], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def publish(folder: str, branch: str, message: str) -> str:
    """Commit `folder`'s contents as the new tip of `branch` (local ref;
    created from origin/<branch> if needed). Returns the commit id."""
    git_dir = _git("rev-parse", "--absolute-git-dir")
    parent = None
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        try:
            parent = _git("rev-parse", "--verify", "--quiet", ref)
            break
        except subprocess.CalledProcessError:
            continue
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(tmp, "index"))
        base = ["--git-dir", git_dir, "--work-tree", folder]
        subprocess.run(["git", *base, "add", "--all", "--force", "."],
                       cwd=folder, env=env, check=True)
        tree = subprocess.run(["git", *base, "write-tree"], cwd=folder,
                              env=env, check=True, capture_output=True,
                              text=True).stdout.strip()
    if parent and _git("rev-parse", f"{parent}^{{tree}}") == tree:
        return parent                               # already published
    cmd = ["commit-tree", tree, "-m", message]
    if parent:
        cmd[2:2] = ["-p", parent]
    commit = _git(*cmd)
    _git("update-ref", f"refs/heads/{branch}", commit, *([parent] if parent else []))
    return commit


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out_dir", nargs="?", help="output folder (default: Release/)")
    ap.add_argument("--publish", metavar="BRANCH",
                    help="also commit the release contents to BRANCH")
    ap.add_argument("--message", help="commit message for --publish")
    args = ap.parse_args()
    folder, zip_path = build(args.out_dir)
    print(f"Built {zip_path}")
    if args.publish:
        version = app_version()
        commit = publish(folder, args.publish,
                         args.message or f"Release {version}")
        print(f"{args.publish} -> {commit[:12]}  "
              f"(push with: git push origin {args.publish})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
