"""
core/discord_status.py — Post server status (address + player count) to
a Discord channel via a channel webhook.

Webhooks need no bot account, no token juggling and no extra pip
packages: the user creates one in Discord (Channel → Edit Channel →
Integrations → Webhooks → New Webhook → Copy Webhook URL) and pastes the
URL into the DISCORD tab.

Layering mirrors core/autorun.py:
- Config is a plain dict (round-trips through settings JSON).
- DiscordStatusScheduler is pure logic with an injectable clock — it
  only decides *when* an update is due. No Tk, no network.
- DiscordStatusWorker owns the network side (public-IP lookup and the
  webhook HTTP calls) on a background thread so the Tk loop never
  blocks. Its process() method is synchronous so tests can drive it
  directly with a fake HTTP client.

Two update modes:
    "edit"  — post one status message, then keep editing it in place
              (the channel always shows a single, current status card).
    "post"  — post a fresh message on every update.

Config schema (per profile):

    {
        "enabled":               False,
        "webhook_url":           "",
        "address_override":      "",     # blank = auto-detect public IP
        "update_mode":           "edit", # "edit" | "post"
        "interval_mins":         5,      # 0 = only on events
        "post_on_start":         True,
        "post_on_stop":          True,
        "post_on_player_change": True,
        "show_player_names":     True,
        "message_id":            "",     # edit mode: the message we edit

        # Map snapshots. map_source "savegame" renders the live savegame
        # with the WORLD MAP tab's renderer (core/map_render.py).
        # "file" uploads an image a map mod exported: map_path may be a
        # file or a folder (newest image in it is used); the optional
        # map_command is sent to the server console first, then the
        # upload waits map_command_wait_secs for the export.
        "map_enabled":           False,
        "map_source":            "savegame",   # "savegame" | "file"
        "map_mark_players":      True,         # savegame: mark online players
        "map_path":              "",
        "map_interval_mins":     60,
        "map_command":           "",
        "map_command_wait_secs": 30,
        "map_message_id":        "",     # edit mode: the map message
    }
"""
from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .constants import APP_NAME, APP_VERSION, LOG

# Discord's Cloudflare front-end rejects urllib's default User-Agent
# (error 1010), so every request carries an explicit one.
USER_AGENT = (f"{APP_NAME}/{APP_VERSION} "
              "(+https://github.com/fighter199/Vintage-Story-Server-Manager)")

REQ_TIMEOUT = 10

UPDATE_MODES = ("edit", "post")

# Public-IP echo services, tried in order. Each returns the caller's
# address as plain text.
IP_SERVICES = (
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://checkip.amazonaws.com",
)

# Re-detect the public IP at most this often (it rarely changes, and
# a dynamic-IP change is picked up within this window).
IP_CACHE_SECS = 600

COLOR_ONLINE  = 0x3BA55C
COLOR_OFFLINE = 0xED4245
COLOR_MAP     = 0x5865F2

# Discord's per-file upload limit for webhooks on unboosted servers.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

MAP_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
MAP_SOURCES = ("savegame", "file")

_WEBHOOK_RE = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com"
    r"/api(?:/v\d+)?/webhooks/(\d+)/([A-Za-z0-9_\-]+)/?$")

_DEFAULTS = {
    "enabled":               False,
    "webhook_url":           "",
    "address_override":      "",
    "update_mode":           "edit",
    "interval_mins":         5,
    "post_on_start":         True,
    "post_on_stop":          True,
    "post_on_player_change": True,
    "show_player_names":     True,
    "message_id":            "",
    "map_enabled":           False,
    "map_source":            "savegame",
    "map_mark_players":      True,
    "map_path":              "",
    "map_interval_mins":     60,
    "map_command":           "",
    "map_command_wait_secs": 30,
    "map_message_id":        "",
}


# ----------------------------------------------------------------------
# Config helpers
# ----------------------------------------------------------------------
def make_default_config() -> dict:
    return dict(_DEFAULTS)


def normalize_config(cfg) -> dict:
    """Return a new dict coerced into the canonical schema. Unknown
    keys are dropped so nothing odd round-trips through settings."""
    src = cfg if isinstance(cfg, dict) else {}
    out = make_default_config()
    for k in out:
        if k in src:
            out[k] = src[k]
    for k in ("enabled", "post_on_start", "post_on_stop",
              "post_on_player_change", "show_player_names", "map_enabled",
              "map_mark_players"):
        out[k] = bool(out[k])
    for k in ("webhook_url", "address_override", "message_id",
              "map_path", "map_command", "map_message_id"):
        out[k] = str(out[k] or "").strip()
    if out["update_mode"] not in UPDATE_MODES:
        out["update_mode"] = "edit"
    if out["map_source"] not in MAP_SOURCES:
        out["map_source"] = "savegame"
    for k, lo in (("interval_mins", 0), ("map_interval_mins", 1),
                  ("map_command_wait_secs", 0)):
        try:
            out[k] = max(lo, int(out[k]))
        except (TypeError, ValueError):
            out[k] = _DEFAULTS[k]
    return out


def validate_webhook_url(url: str) -> tuple[bool, str]:
    """Return (ok, reason). Accepts discord.com / discordapp.com webhook
    URLs, optionally with a ?thread_id= query for forum threads."""
    url = (url or "").strip()
    if not url:
        return False, "webhook URL is empty"
    parts = urllib.parse.urlsplit(url)
    base = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "", ""))
    if not _WEBHOOK_RE.match(base):
        return False, ("not a Discord webhook URL "
                       "(expected https://discord.com/api/webhooks/…)")
    return True, ""


# ----------------------------------------------------------------------
# Address + formatting
# ----------------------------------------------------------------------
def format_address(host: str, port: int | None) -> str:
    """Join host and port the way a player would type it into the VS
    multiplayer dialog. A host that already carries a port is left as
    is; bare IPv6 addresses get brackets."""
    host = (host or "").strip()
    if not host:
        return ""
    if not port:
        return host
    if host.startswith("[") and "]:" in host:
        return host                       # [v6]:port already
    try:
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
            return f"[{host}]:{port}"
    except ValueError:
        pass
    if ":" in host:
        return host                       # hostname:port already
    return f"{host}:{port}"


def fmt_uptime(secs) -> str:
    if secs is None:
        return "—"
    secs = max(0, int(secs))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


_MD_SPECIAL = re.compile(r"([\\*_~`|>])")


def escape_markdown(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text or "")


def build_payload(snapshot: dict, cfg: dict, address: str) -> dict:
    """Build the webhook JSON body for one status update.

    snapshot keys: online, players (list[str]), max_players (int|None),
    uptime_secs (float|None), server_name (str).
    """
    online  = bool(snapshot.get("online"))
    players = list(snapshot.get("players") or [])
    n       = len(players)
    maxp    = snapshot.get("max_players")
    count   = f"{n} / {maxp}" if maxp else str(n)

    fields = [
        {"name": "Status",
         "value": "🟢 Online" if online else "🔴 Offline",
         "inline": True},
        {"name": "Players",
         "value": count if online else "0",
         "inline": True},
    ]
    if online:
        fields.append({"name": "Uptime",
                       "value": fmt_uptime(snapshot.get("uptime_secs")),
                       "inline": True})
    fields.append({"name": "Address",
                   "value": f"`{address}`" if address else "*unknown*",
                   "inline": False})
    if online and cfg.get("show_player_names") and players:
        names = ", ".join(escape_markdown(p) for p in players)
        if len(names) > 1024:                  # Discord field limit
            names = names[:1020] + " …"
        fields.append({"name": "Online now", "value": names,
                       "inline": False})

    title = (snapshot.get("server_name") or "").strip() \
        or "Vintage Story Server"
    return {
        "username": "VSSM",
        "embeds": [{
            "title":     title[:256],
            "color":     COLOR_ONLINE if online else COLOR_OFFLINE,
            "fields":    fields,
            "footer":    {"text": f"{APP_NAME} {APP_VERSION}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
        # Player names must never turn into @everyone / role pings.
        "allowed_mentions": {"parse": []},
    }


# ----------------------------------------------------------------------
# Map snapshots
# ----------------------------------------------------------------------
def find_map_image(path: str) -> str | None:
    """Resolve the configured map path to one image file. A file is
    used as-is; a folder yields its most recently modified image (map
    mods often write timestamped exports). Not recursive."""
    path = (path or "").strip()
    if not path:
        return None
    if os.path.isfile(path):
        return path
    if not os.path.isdir(path):
        return None
    best, best_mtime = None, -1.0
    try:
        with os.scandir(path) as it:
            for entry in it:
                if not entry.name.lower().endswith(MAP_IMAGE_EXTS):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if mtime > best_mtime:
                    best, best_mtime = entry.path, mtime
    except OSError:
        return None
    return best


def build_map_payload(snapshot: dict, filename: str) -> dict:
    title = (snapshot.get("server_name") or "").strip() \
        or "Vintage Story Server"
    return {
        "username": "VSSM",
        "embeds": [{
            "title":     f"{title} — world map"[:256],
            "color":     COLOR_MAP,
            "image":     {"url": f"attachment://{filename}"},
            "footer":    {"text": f"{APP_NAME} {APP_VERSION}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
        "allowed_mentions": {"parse": []},
        # New uploads must be declared here; on an edit this list also
        # replaces the message's previous attachments.
        "attachments": [{"id": 0, "filename": filename}],
    }


def encode_multipart(payload: dict, filename: str, data: bytes
                     ) -> tuple[bytes, str]:
    """Encode payload_json + one file as multipart/form-data.
    Returns (body, content_type)."""
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    safe_name = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="payload_json"\r\n'
        "Content-Type: application/json\r\n\r\n"
        f"{json.dumps(payload)}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files[0]"; '
        f'filename="{safe_name}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    return head + data + tail, f"multipart/form-data; boundary={boundary}"


# ----------------------------------------------------------------------
# Public IP lookup
# ----------------------------------------------------------------------
def fetch_public_ip(opener=urllib.request.urlopen,
                    services=IP_SERVICES,
                    timeout: float = 5) -> str | None:
    """Return this machine's public IPv4 address, or None if every
    service failed. Each response is validated as a real IP so a
    captive-portal HTML page can't end up in the Discord post."""
    for url in services:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with opener(req, timeout=timeout) as resp:
                text = resp.read(64).decode("ascii", "replace").strip()
            return str(ipaddress.ip_address(text))
        except (urllib.error.URLError, socket.timeout, ValueError, OSError) as e:
            LOG.debug("public IP lookup via %s failed: %s", url, e)
    return None


# ----------------------------------------------------------------------
# Webhook client
# ----------------------------------------------------------------------
class WebhookError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class WebhookClient:
    """Minimal Discord webhook client: execute (POST) and edit (PATCH).

    One automatic retry on HTTP 429, honouring Discord's retry_after
    (capped so a misbehaving header can't stall the worker for long).
    """
    MAX_RETRY_WAIT = 10.0

    def __init__(self, url: str, opener=urllib.request.urlopen,
                 sleep: Callable[[float], None] = time.sleep):
        parts = urllib.parse.urlsplit(url.strip())
        self._base  = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
        self._query = urllib.parse.parse_qsl(parts.query)  # e.g. thread_id
        self._opener = opener
        self._sleep  = sleep

    def _url(self, suffix: str = "", **extra) -> str:
        q = self._query + list(extra.items())
        qs = ("?" + urllib.parse.urlencode(q)) if q else ""
        return self._base + suffix + qs

    def execute(self, payload: dict, file: tuple[str, bytes] | None = None
                ) -> str:
        """Post a new message (optionally with one file attached as
        (filename, bytes)); return its message id."""
        body = self._request("POST", self._url(wait="true"), payload, file)
        return str((body or {}).get("id") or "")

    def edit(self, message_id: str, payload: dict,
             file: tuple[str, bytes] | None = None) -> None:
        """Edit a message previously posted by this webhook. With a
        file, the new upload replaces the message's old attachments."""
        # PATCH doesn't take username/avatar — strip anything it rejects.
        patch = {k: v for k, v in payload.items()
                 if k in ("content", "embeds", "allowed_mentions",
                          "attachments")}
        self._request("PATCH",
                      self._url(f"/messages/{urllib.parse.quote(message_id)}"),
                      patch, file)

    def _request(self, method: str, url: str, payload: dict,
                 file: tuple[str, bytes] | None = None,
                 _retried: bool = False):
        if file is not None:
            data, ctype = encode_multipart(payload, file[0], file[1])
        else:
            data, ctype = json.dumps(payload).encode("utf-8"), "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers={
            "User-Agent":   USER_AGENT,
            "Content-Type": ctype,
        })
        try:
            with self._opener(req, timeout=REQ_TIMEOUT) as resp:
                raw = resp.read()
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            if e.code == 429 and not _retried:
                wait = 1.0
                try:
                    wait = float(json.loads(detail).get("retry_after", 1.0))
                except (ValueError, TypeError, AttributeError):
                    pass
                self._sleep(min(max(wait, 0.0), self.MAX_RETRY_WAIT))
                return self._request(method, url, payload, file,
                                     _retried=True)
            msg = detail
            try:
                msg = json.loads(detail).get("message") or detail
            except (ValueError, AttributeError):
                pass
            raise WebhookError(e.code, (msg or e.reason or "")[:200]) from None
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            raise WebhookError(0, f"network error: {e}") from None


# ----------------------------------------------------------------------
# Scheduler — decides WHEN an update is due (pure logic)
# ----------------------------------------------------------------------
class DiscordStatusScheduler:
    """The host calls on_server_start / on_server_stop /
    on_players_changed as things happen, and tick() about once a
    second. Each returns a reason string when an update should go out
    now, else None. The host then builds a snapshot, hands it to the
    worker and calls mark_posted().

    Player changes are debounced: a burst of joins (or the /list resync
    after start) produces a single update PLAYER_DEBOUNCE_SECS after
    the first change, which also keeps "post" mode from spamming.
    """
    PLAYER_DEBOUNCE_SECS = 10

    def __init__(self, clock: Callable[[], float] | None = None):
        self._clock = clock or time.time
        self._running = False
        self._next_interval: float | None = None
        self._player_deadline: float | None = None
        self._next_map: float | None = None

    def on_server_start(self, cfg: dict) -> str | None:
        now = self._clock()
        self._running = True
        self._player_deadline = None
        self._arm_interval(cfg, now)
        # First map goes out one full map interval after start — the
        # world needs to be up (and explored) before a render is useful.
        self._arm_map(cfg, now)
        if cfg.get("enabled") and cfg.get("post_on_start"):
            return "start"
        return None

    def on_server_stop(self, cfg: dict) -> str | None:
        was_running = self._running
        self._running = False
        self._next_interval = None
        self._player_deadline = None
        self._next_map = None
        if was_running and cfg.get("enabled") and cfg.get("post_on_stop"):
            return "stop"
        return None

    def on_players_changed(self, cfg: dict) -> None:
        if not (self._running and cfg.get("enabled")
                and cfg.get("post_on_player_change")):
            return
        if self._player_deadline is None:
            self._player_deadline = self._clock() + self.PLAYER_DEBOUNCE_SECS

    def tick(self, cfg: dict) -> str | None:
        if not (self._running and cfg.get("enabled")):
            return None
        now = self._clock()
        if self._player_deadline is not None and now >= self._player_deadline:
            return "players"
        if self._next_interval is None:
            # Interval was 0 (or the feature was just enabled) — re-arm
            # so a newly-set interval takes effect without a restart.
            self._arm_interval(cfg, now)
        elif now >= self._next_interval:
            return "interval"
        return None

    def reconfigure(self, cfg: dict) -> None:
        """Settings were saved — re-anchor the interval timer so a new
        interval applies now rather than after the old one expires."""
        if self._running:
            self._arm_interval(cfg, self._clock())
            self._arm_map(cfg, self._clock())
        if not (cfg.get("enabled") and cfg.get("post_on_player_change")):
            self._player_deadline = None

    def map_due(self, cfg: dict) -> bool:
        """True when a map snapshot should be taken now (server running,
        feature on, interval elapsed). Call mark_map_posted() after."""
        if not (self._running and cfg.get("enabled")
                and cfg.get("map_enabled")):
            return False
        now = self._clock()
        if self._next_map is None:
            self._arm_map(cfg, now)
            return False
        return now >= self._next_map

    def mark_map_posted(self, cfg: dict) -> None:
        self._arm_map(cfg, self._clock())

    def _arm_map(self, cfg: dict, now: float) -> None:
        mins = max(1, int(cfg.get("map_interval_mins") or 60))
        self._next_map = now + mins * 60

    def mark_posted(self, cfg: dict) -> None:
        """Any update carries the full current state, so it satisfies
        both a pending player-change post and the interval timer."""
        self._player_deadline = None
        self._arm_interval(cfg, self._clock())

    def _arm_interval(self, cfg: dict, now: float) -> None:
        mins = int(cfg.get("interval_mins") or 0)
        self._next_interval = (now + mins * 60) if mins > 0 else None


# ----------------------------------------------------------------------
# Worker — does the network I/O off the Tk thread
# ----------------------------------------------------------------------
@dataclass
class DiscordResult:
    ok:          bool
    message:     str
    reason:      str = ""     # what triggered the update
    webhook_url: str = ""     # which webhook the message_id belongs to
    message_id:  str = ""     # set when a message was posted / edited
    address:     str = ""
    kind:        str = "status"   # "status" | "map"


class DiscordStatusWorker:
    """Background sender. submit() never blocks; jobs are coalesced per
    kind ("status" / "map") so if several updates queue up while one is
    in flight, only the newest of each kind is sent (it carries the
    latest state anyway)."""

    def __init__(self, on_result: Callable[[DiscordResult], None],
                 ip_lookup: Callable[[], str | None] = fetch_public_ip,
                 client_factory: Callable[[str], WebhookClient] = WebhookClient,
                 clock: Callable[[], float] | None = None,
                 map_renderer: Callable[..., bytes] | None = None):
        self._on_result = on_result
        self._ip_lookup = ip_lookup
        self._client_factory = client_factory
        self._clock = clock or time.time
        self._map_renderer = map_renderer or _default_map_renderer
        self._cv = threading.Condition()
        self._pending: dict[str, dict] = {}     # kind -> newest job
        self._thread: threading.Thread | None = None
        self._stopped = False
        self._busy = False
        self._ip: str | None = None
        self._ip_at: float = 0.0
        # (path, mtime, size) of the last map uploaded, so an unchanged
        # export isn't re-uploaded every interval.
        self._last_map_sig: tuple | None = None

    # -- public ---------------------------------------------------------
    def submit(self, cfg: dict, snapshot: dict, reason: str,
               kind: str = "status") -> None:
        with self._cv:
            if self._stopped:
                return
            self._pending[kind] = {"cfg": dict(cfg),
                                   "snapshot": dict(snapshot),
                                   "reason": reason, "kind": kind}
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="discord-status", daemon=True)
                self._thread.start()
            self._cv.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        """Block until nothing is queued or in flight (or timeout).
        Used at app exit so the final "offline" post isn't cut off when
        the daemon thread dies with the process."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cv:
            while self._pending or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)
        return True

    def shutdown(self) -> None:
        with self._cv:
            self._stopped = True
            self._pending.clear()
            self._cv.notify_all()

    def public_ip(self, refresh: bool = False) -> str | None:
        """Cached public IP; refreshed when stale or on request."""
        now = self._clock()
        if refresh or not self._ip or now - self._ip_at > IP_CACHE_SECS:
            ip = self._ip_lookup()
            if ip:
                self._ip, self._ip_at = ip, now
        return self._ip

    def process(self, cfg: dict, snapshot: dict, reason: str) -> DiscordResult:
        """Send one update synchronously. Never raises."""
        url = cfg.get("webhook_url") or ""
        ok, why = validate_webhook_url(url)
        if not ok:
            return DiscordResult(False, why, reason, url)

        host = cfg.get("address_override") or \
            self.public_ip(refresh=(reason == "start")) or ""
        address = format_address(host, snapshot.get("port"))
        payload = build_payload(snapshot, cfg, address)
        client = self._client_factory(url)

        try:
            msg_id = cfg.get("message_id") or ""
            if cfg.get("update_mode") == "edit" and msg_id:
                try:
                    client.edit(msg_id, payload)
                    return DiscordResult(True, "status message updated",
                                         reason, url, msg_id, address)
                except WebhookError as e:
                    # 404 = message deleted (or webhook recreated):
                    # fall through and post a fresh one.
                    if e.status != 404:
                        raise
            new_id = client.execute(payload)
            return DiscordResult(True, "status message posted",
                                 reason, url, new_id, address)
        except WebhookError as e:
            return DiscordResult(False, str(e), reason, url, "", address)
        except Exception as e:      # never let the worker thread die
            LOG.exception("discord status update failed")
            return DiscordResult(False, f"unexpected error: {e}",
                                 reason, url, "", address)

    def process_map(self, cfg: dict, snapshot: dict, reason: str
                    ) -> DiscordResult:
        """Upload the current map image synchronously. Never raises.
        Scheduled uploads are skipped when the source (savegame or image
        file) hasn't changed since the last upload; a manual ("Post Map
        Now") upload always goes out."""
        url = cfg.get("webhook_url") or ""

        def result(ok, msg, msg_id=""):
            return DiscordResult(ok, msg, reason, url, msg_id, kind="map")

        ok, why = validate_webhook_url(url)
        if not ok:
            return result(False, why)
        if cfg.get("map_source") == "file":
            image = self._file_map_image(cfg, reason)
        else:
            image = self._savegame_map_image(cfg, snapshot, reason)
        if isinstance(image, DiscordResult):
            image.reason, image.webhook_url, image.kind = reason, url, "map"
            return image
        filename, data, sig = image

        payload = build_map_payload(snapshot, filename)
        client = self._client_factory(url)
        try:
            msg_id = cfg.get("map_message_id") or ""
            if cfg.get("update_mode") == "edit" and msg_id:
                try:
                    client.edit(msg_id, payload, (filename, data))
                    self._last_map_sig = sig
                    return result(True, "map image updated", msg_id)
                except WebhookError as e:
                    if e.status != 404:
                        raise
            new_id = client.execute(payload, (filename, data))
            self._last_map_sig = sig
            return result(True, "map image posted", new_id)
        except WebhookError as e:
            return result(False, str(e))
        except Exception as e:      # never let the worker thread die
            LOG.exception("discord map upload failed")
            return result(False, f"unexpected error: {e}")

    # Each map source returns (filename, bytes, change-signature), or a
    # DiscordResult explaining why there's nothing to upload.
    def _unchanged(self, sig: tuple, reason: str) -> bool:
        return reason != "manual" and sig == self._last_map_sig

    def _file_map_image(self, cfg: dict, reason: str):
        path = find_map_image(cfg.get("map_path") or "")
        if not path:
            return DiscordResult(False, "no map image found at "
                                 f"{cfg.get('map_path') or '(no path set)'}")
        try:
            st = os.stat(path)
        except OSError as e:
            return DiscordResult(False, f"can't read map image: {e}")
        if st.st_size > MAX_UPLOAD_BYTES:
            return DiscordResult(
                False, f"map image is {st.st_size / 1048576:.1f} MB "
                       "— over Discord's 10 MB upload limit")
        sig = ("file", path, st.st_mtime, st.st_size)
        if self._unchanged(sig, reason):
            return DiscordResult(True, "map unchanged since last upload — "
                                       "skipped")
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            return DiscordResult(False, f"can't read map image: {e}")
        ext = os.path.splitext(path)[1].lower() or ".png"
        return f"map{ext}", data, sig

    def _savegame_map_image(self, cfg: dict, snapshot: dict, reason: str):
        path = snapshot.get("savegame") or ""
        if not path or not os.path.isfile(path):
            return DiscordResult(False, "no savegame (.vcdbs) found — set the "
                                        "world folder in SETTINGS")
        # The server writes through SQLite's WAL, so the -wal file's
        # mtime moves on every save even when the main file doesn't.
        stamp = []
        for p in (path, path + "-wal"):
            try:
                st = os.stat(p)
                stamp.append((st.st_mtime, st.st_size))
            except OSError:
                stamp.append(None)
        names = tuple(sorted(snapshot.get("players") or ())) \
            if cfg.get("map_mark_players") else ()
        sig = ("savegame", path, tuple(stamp), names)
        if self._unchanged(sig, reason):
            return DiscordResult(True, "world unchanged since last map — "
                                       "skipped")
        try:
            data = self._map_renderer(path, mark_players=names,
                                      max_bytes=MAX_UPLOAD_BYTES)
        except Exception as e:
            LOG.warning("discord map render of %s failed: %s", path, e)
            return DiscordResult(False, f"couldn't render the savegame: {e}")
        return "map.png", data, sig


    # -- thread ---------------------------------------------------------
    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._pending and not self._stopped:
                    self._cv.wait()
                if self._stopped:
                    return
                # Status first: it's small and time-sensitive; a map
                # upload can be several MB.
                kind = "status" if "status" in self._pending \
                    else next(iter(self._pending))
                job = self._pending.pop(kind)
                self._busy = True
            try:
                fn = self.process_map if kind == "map" else self.process
                result = fn(job["cfg"], job["snapshot"], job["reason"])
                try:
                    self._on_result(result)
                except Exception:
                    LOG.exception("discord result callback failed")
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()


def _default_map_renderer(path: str, **kwargs) -> bytes:
    # Imported lazily: world_db is only needed once a map is rendered.
    from .map_render import render_savegame_png
    return render_savegame_png(path, **kwargs)
