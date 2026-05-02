"""
mods/moddb.py — ModDB REST client (stdlib only, no extra dependencies).

All HTTP calls are off-thread-friendly (blocking); callers are responsible
for threading. Structured error results instead of raw exceptions so the
UI never crashes on network flakes.
"""
from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

from core.constants import APP_NAME, APP_VERSION
from core.utils import clean_mod_filename


def _sanitize_url(url: str) -> str:
    """Percent-encode any unsafe characters (spaces, etc.) in a URL's
    path and query without double-encoding already-encoded segments.

    ModDB returns download URLs whose `?dl=...` query parameter contains
    a friendly filename with literal spaces and other characters that
    aren't legal in URLs. urllib.urlopen rejects these outright, so we
    re-quote here. The scheme, host, and existing percent-escapes are
    preserved verbatim.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except (ValueError, AttributeError):
        return url
    # `safe` chars: keep '/' in the path, plus '%' so already-encoded
    # bytes don't get double-encoded.
    path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=")
    # For the query, re-quote with everything-but-the-separators safe.
    # Keeping '%' safe means existing escapes stay intact.
    query = urllib.parse.quote(parts.query, safe="=&%:@!$'()*+,;/?")
    fragment = urllib.parse.quote(parts.fragment, safe="%/?#")
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, path, query, fragment))


class ModDbClient:
    API_BASE      = "https://mods.vintagestory.at/api"
    SITE_BASE     = "https://mods.vintagestory.at"
    ALLOWED_HOSTS = {"mods.vintagestory.at", "moddbcdn.vintagestory.at"}
    USER_AGENT    = f"{APP_NAME}/{APP_VERSION} (+vintagestory mod manager)"
    REQ_TIMEOUT   = 15

    def __init__(self):
        self._tags_cache        = None
        self._gameversions_cache = None
        self._icon_cache: dict  = {}
        self._ssl_ctx = ssl.create_default_context()

    def _open(self, req):
        return urllib.request.urlopen(req, timeout=self.REQ_TIMEOUT,
                                      context=self._ssl_ctx)

    def _get_json(self, path, params=None):
        url = self.API_BASE + path
        if params:
            parts = []
            for k, v in params.items():
                if v is None:
                    continue
                if isinstance(v, (list, tuple)):
                    for item in v:
                        parts.append((k, str(item)))
                else:
                    parts.append((k, str(v)))
            if parts:
                url += "?" + urllib.parse.urlencode(parts)
        req = urllib.request.Request(
            url, headers={"User-Agent": self.USER_AGENT, "Accept": "application/json"})
        try:
            with self._open(req) as resp:
                raw = resp.read()
        except (urllib.error.URLError, socket.timeout) as e:
            raise RuntimeError(f"Network error: {e}")
        except ssl.SSLError as e:
            raise RuntimeError(f"TLS error: {e}")
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Malformed API response: {e}")
        sc = str(data.get("statuscode") or "")
        if sc and sc != "200":
            raise RuntimeError(f"API returned status {sc}")
        return data

    def get_tags(self, force_refresh=False):
        if self._tags_cache is not None and not force_refresh:
            return self._tags_cache
        data = self._get_json("/tags")
        self._tags_cache = data.get("tags") or []
        return self._tags_cache

    def get_gameversions(self, force_refresh=False):
        if self._gameversions_cache is not None and not force_refresh:
            return self._gameversions_cache
        data = self._get_json("/gameversions")
        self._gameversions_cache = data.get("gameversions") or []
        return self._gameversions_cache

    def search_mods(self, text=None, tagids=None, gameversion=None,
                    orderby="trendingpoints", orderdirection="desc"):
        params = {
            "text":           text or None,
            "tagids[]":       tagids or None,
            "gv":             gameversion or None,
            "orderby":        orderby,
            "orderdirection": orderdirection,
        }
        data = self._get_json("/mods", params=params)
        return data.get("mods") or []

    def get_mod(self, mod_id_or_slug):
        path = f"/mod/{urllib.parse.quote(str(mod_id_or_slug))}"
        data = self._get_json(path)
        return data.get("mod") or {}

    def is_trusted_url(self, url: str) -> bool:
        try:
            host = urllib.parse.urlparse(url).hostname or ""
        except Exception:
            return False
        return host.lower() in self.ALLOWED_HOSTS

    def download_file(self, url, dest_path, progress_cb=None,
                      cancel_flag=None, expected_size=None):
        if not self.is_trusted_url(url):
            raise RuntimeError(f"Refused to download from untrusted host: {url}")
        # ModDB sometimes returns URLs with literal spaces in the
        # `?dl=` query parameter (the friendly filename), which Python's
        # urlopen rejects with "URL can't contain control characters".
        # Percent-encode the path and query before opening.
        url = _sanitize_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": self.USER_AGENT})
        part_path = dest_path + ".part"
        try:
            with self._open(req) as resp:
                try:
                    total = int(resp.headers.get("Content-Length") or 0)
                except ValueError:
                    total = 0
                if expected_size and total and abs(total - int(expected_size)) > 1024:
                    raise RuntimeError(
                        f"Server file size ({total}) does not match ModDB ({expected_size}).")
                got = 0
                with open(part_path, "wb") as f:
                    while True:
                        if cancel_flag and cancel_flag():
                            raise RuntimeError("Download cancelled.")
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if progress_cb:
                            try:
                                progress_cb(got, total)
                            except Exception:
                                pass
        except (urllib.error.URLError, socket.timeout) as e:
            self._safe_remove(part_path)
            raise RuntimeError(f"Download error: {e}")
        except Exception:
            self._safe_remove(part_path)
            raise
        if expected_size:
            import os
            actual = os.path.getsize(part_path)
            if abs(actual - int(expected_size)) > 1024:
                self._safe_remove(part_path)
                raise RuntimeError(
                    f"Downloaded size {actual} ≠ expected {expected_size} bytes.")
        import os
        try:
            os.replace(part_path, dest_path)
        except OSError as e:
            self._safe_remove(part_path)
            raise RuntimeError(f"Could not finalize file: {e}")
        return dest_path

    def fetch_icon_bytes(self, url):
        if not url:
            return None
        if url in self._icon_cache:
            return self._icon_cache[url]
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.USER_AGENT})
            with self._open(req) as resp:
                data = resp.read()
            self._icon_cache[url] = data
            return data
        except Exception:
            return None

    @staticmethod
    def _safe_remove(path):
        import os
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
