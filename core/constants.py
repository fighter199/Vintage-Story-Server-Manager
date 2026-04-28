"""
core/constants.py — App-wide constants and logging bootstrap.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

APP_NAME    = "VSSM"
APP_VERSION = "3.1"
SETTINGS_SCHEMA_VERSION = 6

# -----------------------------------------------------------------------
# Directory helpers
# -----------------------------------------------------------------------
def script_dir() -> str:
    """Return the directory containing the VSSM package (where VSSM.py
    and vs_commands.json live). This file is at <package>/core/constants.py,
    so we go up two levels."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))   # …/core
        return os.path.dirname(here)                        # package root
    except NameError:
        return os.getcwd()


def log_dir() -> str:
    d = os.path.join(script_dir(), "logs")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


# -----------------------------------------------------------------------
# Logging — rotating file log (app) + server stdout mirror
# -----------------------------------------------------------------------
def _configure_logger() -> logging.Logger:
    log = logging.getLogger(APP_NAME)
    if log.handlers:
        return log
    log.setLevel(logging.DEBUG)
    try:
        # NOTE: Filename intentionally kept as 'vserverman.log' (and the
        # settings file as 'vserverman_settings.json') so existing users
        # don't lose their log/rotation history or have their settings
        # silently reset after the v3 → VSSM rename.
        path = os.path.join(log_dir(), "vserverman.log")
        h = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        log.addHandler(h)
    except Exception:
        pass
    return log


def _get_server_log() -> logging.Logger:
    log = logging.getLogger(f"{APP_NAME}.server")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        path = os.path.join(log_dir(), "server-output.log")
        h = logging.handlers.RotatingFileHandler(
            path, maxBytes=5_000_000, backupCount=10, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        log.addHandler(h)
    except Exception:
        pass
    return log


LOG        = _configure_logger()
SERVER_LOG = _get_server_log()

# Roles that grant operator-level access on VS servers
OPERATOR_ROLES = {"admin", "suadmin", "superadmin", "operator", "op"}
