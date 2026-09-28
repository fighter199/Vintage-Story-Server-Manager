"""
core/updates.py — Is a newer VSSM release out?

Asks GitHub for the project's latest release (the one the release
workflow publishes from Main) and compares its version with ours.
Nothing is downloaded or installed; the caller just offers the
release page.
"""
from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.request

from .constants import APP_NAME, APP_VERSION

REPO = "fighter199/Vintage-Story-Server-Manager"
PROJECT_URL = f"https://github.com/{REPO}"
RELEASES_URL = f"{PROJECT_URL}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
TIMEOUT = 10


class UpdateCheckError(Exception):
    """The check couldn't be done; the message is user-facing."""


def parse_version(text: str) -> tuple:
    """"v3.4", "3.4.1", "VSSM 3.10-beta" -> (3, 4), (3, 4, 1), (3, 10).
    Trailing zeros are dropped so 3.4 and 3.4.0 compare equal."""
    m = re.search(r"(\d+(?:\.\d+)*)", text or "")
    if not m:
        return ()
    parts = [int(p) for p in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(latest: str, current: str = APP_VERSION) -> bool:
    new, cur = parse_version(latest), parse_version(current)
    return bool(new) and new > cur


def release_info(payload: dict) -> dict:
    """The parts of a GitHub release object the app shows."""
    tag = str(payload.get("tag_name") or "")
    return {
        "version": ".".join(map(str, parse_version(tag))) or tag,
        "tag": tag,
        "name": payload.get("name") or tag,
        "url": payload.get("html_url") or RELEASES_URL,
        "notes": payload.get("body") or "",
    }


def fetch_latest_release(timeout: float = TIMEOUT) -> dict:
    """The latest published release (see release_info). Blocking — run
    it off the Tk thread. Raises UpdateCheckError."""
    req = urllib.request.Request(LATEST_RELEASE_API, headers={
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl.create_default_context()) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateCheckError("No releases are published yet.") from e
        if e.code == 403:
            raise UpdateCheckError("GitHub is rate-limiting update checks; "
                                   "try again in an hour.") from e
        raise UpdateCheckError(f"GitHub answered {e.code}.") from e
    except (urllib.error.URLError, OSError, ssl.SSLError) as e:
        reason = getattr(e, "reason", e)
        raise UpdateCheckError(f"Couldn't reach GitHub ({reason}).") from e
    except ValueError as e:
        raise UpdateCheckError("GitHub sent an unreadable answer.") from e
    if not isinstance(payload, dict) or not payload.get("tag_name"):
        raise UpdateCheckError("GitHub sent an unreadable answer.")
    return release_info(payload)
