"""Tests for core.discord_status (webhook status poster)."""
import io
import json
import urllib.error

import pytest

from core.discord_status import (
    COLOR_OFFLINE,
    COLOR_ONLINE,
    DiscordStatusScheduler,
    DiscordStatusWorker,
    WebhookClient,
    WebhookError,
    build_payload,
    escape_markdown,
    fetch_public_ip,
    fmt_uptime,
    format_address,
    make_default_config,
    normalize_config,
    validate_webhook_url,
)
from core.settings import load_discord_config, save_discord_config

WEBHOOK = "https://discord.com/api/webhooks/123456789/abcDEF-_xyz"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t
    def __call__(self) -> float:
        return self.t
    def advance(self, dt: float) -> None:
        self.t += dt


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Records requests; replies from a queue of bytes / exceptions."""
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
    def __call__(self, req, timeout=None):
        self.requests.append(req)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(reply)


def http_error(code: int, body: dict | None = None):
    data = json.dumps(body or {}).encode()
    return urllib.error.HTTPError(WEBHOOK, code, "err", {}, io.BytesIO(data))


def enabled_cfg(**over):
    cfg = make_default_config()
    cfg.update(enabled=True, webhook_url=WEBHOOK)
    cfg.update(over)
    return cfg


def snapshot(**over):
    snap = {"online": True, "players": ["Alice", "Bob"], "max_players": 16,
            "port": 42420, "server_name": "My Server", "uptime_secs": 3700}
    snap.update(over)
    return snap


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
class TestConfig:
    def test_defaults_are_disabled(self):
        cfg = normalize_config(None)
        assert cfg["enabled"] is False
        assert cfg["update_mode"] == "edit"

    def test_drops_unknown_keys_and_coerces(self):
        cfg = normalize_config({"evil": 1, "interval_mins": "-3",
                                "update_mode": "nope", "enabled": 1,
                                "webhook_url": "  x  "})
        assert "evil" not in cfg
        assert cfg["interval_mins"] == 0
        assert cfg["update_mode"] == "edit"
        assert cfg["enabled"] is True
        assert cfg["webhook_url"] == "x"

    def test_bad_interval_falls_back_to_default(self):
        assert normalize_config({"interval_mins": "abc"})["interval_mins"] == 5

    def test_settings_round_trip_is_per_profile(self):
        s = {"active_profile": "a", "profiles": {"a": {}, "b": {}}}
        save_discord_config(s, enabled_cfg())
        assert load_discord_config(s)["webhook_url"] == WEBHOOK
        s["active_profile"] = "b"
        assert load_discord_config(s)["webhook_url"] == ""


# Plain loops rather than pytest.mark.parametrize so run_tests.py's
# pytest shim can run this file too.
class TestValidateWebhookUrl:
    def test_accepts(self):
        for url in [
            WEBHOOK,
            WEBHOOK + "/",
            "https://discordapp.com/api/webhooks/1/tok",
            "https://canary.discord.com/api/v10/webhooks/1/tok",
            WEBHOOK + "?thread_id=42",
        ]:
            assert validate_webhook_url(url)[0], url

    def test_rejects(self):
        for url in [
            "",
            "http://discord.com/api/webhooks/1/tok",           # not https
            "https://evil.com/api/webhooks/1/tok",
            "https://discord.com.evil.com/api/webhooks/1/tok",
            "https://discord.com/api/webhooks/abc/tok",         # id not numeric
            "https://discord.com/channels/1/2",
        ]:
            ok, reason = validate_webhook_url(url)
            assert not ok and reason, url


# ----------------------------------------------------------------------
# Formatting
# ----------------------------------------------------------------------
class TestFormatting:
    def test_format_address(self):
        assert format_address("1.2.3.4", 42420) == "1.2.3.4:42420"
        assert format_address("play.example.com", 42420) == \
            "play.example.com:42420"
        assert format_address("play.example.com:5000", 42420) == \
            "play.example.com:5000"
        assert format_address("2001:db8::1", 42420) == "[2001:db8::1]:42420"
        assert format_address("[2001:db8::1]:5000", 42420) == \
            "[2001:db8::1]:5000"
        assert format_address("", 42420) == ""

    def test_fmt_uptime(self):
        assert fmt_uptime(None) == "—"
        assert fmt_uptime(59) == "0m"
        assert fmt_uptime(3700) == "1h 1m"
        assert fmt_uptime(90000) == "1d 1h 0m"

    def test_escape_markdown(self):
        assert escape_markdown("a_b*c") == r"a\_b\*c"


class TestBuildPayload:
    def fields(self, payload):
        return {f["name"]: f["value"] for f in payload["embeds"][0]["fields"]}

    def test_online(self):
        p = build_payload(snapshot(), enabled_cfg(), "1.2.3.4:42420")
        emb = p["embeds"][0]
        f = self.fields(p)
        assert emb["title"] == "My Server"
        assert emb["color"] == COLOR_ONLINE
        assert f["Players"] == "2 / 16"
        assert f["Address"] == "`1.2.3.4:42420`"
        assert f["Online now"] == "Alice, Bob"
        assert f["Uptime"] == "1h 1m"
        assert p["allowed_mentions"] == {"parse": []}

    def test_offline_hides_players_and_uptime(self):
        p = build_payload(snapshot(online=False, players=[]),
                          enabled_cfg(), "1.2.3.4:42420")
        f = self.fields(p)
        assert p["embeds"][0]["color"] == COLOR_OFFLINE
        assert f["Players"] == "0"
        assert "Uptime" not in f and "Online now" not in f

    def test_names_can_be_hidden(self):
        p = build_payload(snapshot(), enabled_cfg(show_player_names=False),
                          "x")
        assert "Online now" not in self.fields(p)

    def test_unknown_max_players(self):
        p = build_payload(snapshot(max_players=None), enabled_cfg(), "x")
        assert self.fields(p)["Players"] == "2"

    def test_long_name_list_is_truncated(self):
        names = [f"player{i:04d}" for i in range(300)]
        p = build_payload(snapshot(players=names), enabled_cfg(), "x")
        assert len(self.fields(p)["Online now"]) <= 1024

    def test_default_title(self):
        p = build_payload(snapshot(server_name=""), enabled_cfg(), "x")
        assert p["embeds"][0]["title"] == "Vintage Story Server"


# ----------------------------------------------------------------------
# Public IP
# ----------------------------------------------------------------------
class TestFetchPublicIp:
    SERVICES = ("https://a.test", "https://b.test", "https://c.test")

    def test_returns_first_valid(self):
        opener = FakeOpener(b"203.0.113.7\n")
        assert fetch_public_ip(opener, services=self.SERVICES[:1]) == \
            "203.0.113.7"

    def test_skips_garbage_and_failures(self):
        opener = FakeOpener(urllib.error.URLError("down"),
                            b"<html>captive portal</html>",
                            b"198.51.100.2")
        assert fetch_public_ip(opener, services=self.SERVICES) == \
            "198.51.100.2"
        assert len(opener.requests) == 3

    def test_none_when_all_fail(self):
        opener = FakeOpener(urllib.error.URLError("down"))
        assert fetch_public_ip(opener, services=self.SERVICES[:1]) is None


# ----------------------------------------------------------------------
# Webhook client
# ----------------------------------------------------------------------
class TestWebhookClient:
    def test_execute_posts_with_wait_and_returns_id(self):
        opener = FakeOpener(b'{"id": "999"}')
        client = WebhookClient(WEBHOOK, opener=opener)
        assert client.execute({"content": "hi"}) == "999"
        req = opener.requests[0]
        assert req.get_method() == "POST"
        assert req.full_url == WEBHOOK + "?wait=true"
        assert req.get_header("User-agent").startswith("VSSM/")

    def test_thread_id_query_preserved(self):
        opener = FakeOpener(b'{"id": "1"}', b"")
        client = WebhookClient(WEBHOOK + "?thread_id=42", opener=opener)
        client.execute({})
        client.edit("1", {})
        assert opener.requests[0].full_url == \
            WEBHOOK + "?thread_id=42&wait=true"
        assert opener.requests[1].full_url == \
            WEBHOOK + "/messages/1?thread_id=42"

    def test_edit_patches_and_strips_username(self):
        opener = FakeOpener(b"{}")
        WebhookClient(WEBHOOK, opener=opener).edit(
            "55", {"username": "VSSM", "embeds": [], "allowed_mentions": {}})
        req = opener.requests[0]
        assert req.get_method() == "PATCH"
        assert req.full_url == WEBHOOK + "/messages/55"
        assert json.loads(req.data) == {"embeds": [], "allowed_mentions": {}}

    def test_429_retries_once_after_retry_after(self):
        slept = []
        opener = FakeOpener(http_error(429, {"retry_after": 0.5}),
                            b'{"id": "7"}')
        client = WebhookClient(WEBHOOK, opener=opener, sleep=slept.append)
        assert client.execute({}) == "7"
        assert slept == [0.5]

    def test_http_error_raises_with_status_and_message(self):
        opener = FakeOpener(http_error(404, {"message": "Unknown Webhook"}))
        try:
            WebhookClient(WEBHOOK, opener=opener).execute({})
        except WebhookError as e:
            assert e.status == 404
            assert "Unknown Webhook" in str(e)
        else:
            raise AssertionError("WebhookError not raised")

    def test_network_error_is_status_zero(self):
        opener = FakeOpener(urllib.error.URLError("no route"))
        with pytest.raises(WebhookError, match="network error"):
            WebhookClient(WEBHOOK, opener=opener).execute({})


# ----------------------------------------------------------------------
# Scheduler
# ----------------------------------------------------------------------
class TestScheduler:
    def test_start_and_stop_reasons(self):
        s = DiscordStatusScheduler(FakeClock())
        cfg = enabled_cfg()
        assert s.on_server_start(cfg) == "start"
        assert s.on_server_stop(cfg) == "stop"

    def test_disabled_never_fires(self):
        clock = FakeClock()
        s = DiscordStatusScheduler(clock)
        cfg = enabled_cfg(enabled=False)
        assert s.on_server_start(cfg) is None
        s.on_players_changed(cfg)
        clock.advance(3600)
        assert s.tick(cfg) is None
        assert s.on_server_stop(cfg) is None

    def test_stop_without_start_is_silent(self):
        s = DiscordStatusScheduler(FakeClock())
        assert s.on_server_stop(enabled_cfg()) is None

    def test_trigger_toggles(self):
        s = DiscordStatusScheduler(FakeClock())
        cfg = enabled_cfg(post_on_start=False, post_on_stop=False)
        assert s.on_server_start(cfg) is None
        assert s.on_server_stop(cfg) is None

    def test_interval(self):
        clock = FakeClock()
        s = DiscordStatusScheduler(clock)
        cfg = enabled_cfg(interval_mins=5)
        s.on_server_start(cfg)
        clock.advance(299)
        assert s.tick(cfg) is None
        clock.advance(1)
        assert s.tick(cfg) == "interval"
        s.mark_posted(cfg)
        assert s.tick(cfg) is None

    def test_interval_zero_means_events_only(self):
        clock = FakeClock()
        s = DiscordStatusScheduler(clock)
        cfg = enabled_cfg(interval_mins=0)
        s.on_server_start(cfg)
        clock.advance(86400)
        assert s.tick(cfg) is None

    def test_player_changes_are_debounced(self):
        clock = FakeClock()
        s = DiscordStatusScheduler(clock)
        cfg = enabled_cfg(interval_mins=0)
        s.on_server_start(cfg)
        s.on_players_changed(cfg)
        clock.advance(5)
        s.on_players_changed(cfg)          # doesn't push the deadline out
        assert s.tick(cfg) is None
        clock.advance(5)
        assert s.tick(cfg) == "players"
        s.mark_posted(cfg)
        assert s.tick(cfg) is None

    def test_player_changes_ignored_while_stopped_or_toggled_off(self):
        clock = FakeClock()
        s = DiscordStatusScheduler(clock)
        cfg = enabled_cfg(interval_mins=0)
        s.on_players_changed(cfg)          # not running
        s.on_server_start(cfg)
        s.on_players_changed(enabled_cfg(post_on_player_change=False))
        clock.advance(60)
        assert s.tick(cfg) is None

    def test_tick_is_noop_while_stopped(self):
        clock = FakeClock()
        s = DiscordStatusScheduler(clock)
        clock.advance(10_000)
        assert s.tick(enabled_cfg()) is None

    def test_reconfigure_applies_new_interval(self):
        clock = FakeClock()
        s = DiscordStatusScheduler(clock)
        s.on_server_start(enabled_cfg(interval_mins=60))
        clock.advance(120)
        cfg = enabled_cfg(interval_mins=1)
        s.reconfigure(cfg)
        clock.advance(59)
        assert s.tick(cfg) is None
        clock.advance(1)
        assert s.tick(cfg) == "interval"


# ----------------------------------------------------------------------
# Worker (synchronous process())
# ----------------------------------------------------------------------
class FakeClient:
    def __init__(self, edit_error=None, post_id="new-id", post_error=None):
        self.edit_error = edit_error
        self.post_id = post_id
        self.post_error = post_error
        self.edits = []
        self.posts = []
    def edit(self, message_id, payload):
        self.edits.append((message_id, payload))
        if self.edit_error:
            raise self.edit_error
    def execute(self, payload):
        self.posts.append(payload)
        if self.post_error:
            raise self.post_error
        return self.post_id


def make_worker(client, ip="203.0.113.7"):
    lookups = []
    def ip_lookup():
        lookups.append(1)
        return ip
    w = DiscordStatusWorker(on_result=lambda r: None, ip_lookup=ip_lookup,
                            client_factory=lambda url: client,
                            clock=FakeClock())
    return w, lookups


class TestWorker:
    def test_first_post_in_edit_mode(self):
        client = FakeClient()
        w, _ = make_worker(client)
        r = w.process(enabled_cfg(), snapshot(), "start")
        assert r.ok and r.message_id == "new-id"
        assert r.address == "203.0.113.7:42420"
        assert len(client.posts) == 1 and not client.edits

    def test_edits_existing_message(self):
        client = FakeClient()
        w, _ = make_worker(client)
        r = w.process(enabled_cfg(message_id="42"), snapshot(), "players")
        assert r.ok and r.message_id == "42"
        assert client.edits[0][0] == "42" and not client.posts

    def test_deleted_message_falls_back_to_new_post(self):
        client = FakeClient(edit_error=WebhookError(404, "Unknown Message"))
        w, _ = make_worker(client)
        r = w.process(enabled_cfg(message_id="42"), snapshot(), "interval")
        assert r.ok and r.message_id == "new-id"
        assert len(client.posts) == 1

    def test_other_edit_errors_are_reported(self):
        client = FakeClient(edit_error=WebhookError(401, "Invalid token"))
        w, _ = make_worker(client)
        r = w.process(enabled_cfg(message_id="42"), snapshot(), "interval")
        assert not r.ok and "Invalid token" in r.message
        assert not client.posts

    def test_post_mode_always_posts(self):
        client = FakeClient()
        w, _ = make_worker(client)
        w.process(enabled_cfg(update_mode="post", message_id="42"),
                  snapshot(), "players")
        assert len(client.posts) == 1 and not client.edits

    def test_address_override_skips_ip_lookup(self):
        client = FakeClient()
        w, lookups = make_worker(client)
        r = w.process(enabled_cfg(address_override="play.example.com"),
                      snapshot(), "start")
        assert r.address == "play.example.com:42420"
        assert not lookups

    def test_ip_is_cached_between_posts(self):
        client = FakeClient()
        w, lookups = make_worker(client)
        w.process(enabled_cfg(), snapshot(), "players")
        w.process(enabled_cfg(), snapshot(), "players")
        assert len(lookups) == 1

    def test_invalid_webhook_is_rejected_without_network(self):
        client = FakeClient()
        w, lookups = make_worker(client)
        r = w.process(enabled_cfg(webhook_url="https://evil.com/x"),
                      snapshot(), "manual")
        assert not r.ok
        assert not client.posts and not lookups

    def test_unknown_ip_still_posts(self):
        client = FakeClient()
        w, _ = make_worker(client, ip=None)
        r = w.process(enabled_cfg(), snapshot(), "start")
        assert r.ok and r.address == ""

    def test_threaded_submit_delivers_result(self):
        import threading
        got = []
        done = threading.Event()
        def on_result(r):
            got.append(r)
            done.set()
        w = DiscordStatusWorker(on_result=on_result, ip_lookup=lambda: "1.1.1.1",
                                client_factory=lambda url: FakeClient())
        w.submit(enabled_cfg(), snapshot(), "manual")
        assert done.wait(5)
        assert w.wait_idle(5)
        w.shutdown()
        assert got[0].ok and got[0].reason == "manual"


# ----------------------------------------------------------------------
# Map snapshots
# ----------------------------------------------------------------------
from core.discord_status import (  # noqa: E402
    MAX_UPLOAD_BYTES,
    build_map_payload,
    encode_multipart,
    find_map_image,
)


def write(path, data=b"\x89PNG fake", mtime=None):
    import os
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return str(path)


class TestFindMapImage:
    def test_file_used_as_is(self, tmp_path):
        p = write(tmp_path / "world.png")
        assert find_map_image(p) == p

    def test_folder_picks_newest_image(self, tmp_path):
        write(tmp_path / "old.png", mtime=1000)
        newest = write(tmp_path / "new.jpg", mtime=3000)
        write(tmp_path / "notes.txt", mtime=5000)      # not an image
        assert find_map_image(str(tmp_path)) == newest

    def test_missing_or_empty(self, tmp_path):
        assert find_map_image("") is None
        assert find_map_image(str(tmp_path / "nope")) is None
        assert find_map_image(str(tmp_path)) is None


class TestMapPayload:
    def test_embed_references_attachment(self):
        p = build_map_payload(snapshot(), "map.png")
        assert p["embeds"][0]["image"]["url"] == "attachment://map.png"
        assert p["embeds"][0]["title"] == "My Server — world map"
        assert p["attachments"] == [{"id": 0, "filename": "map.png"}]

    def test_multipart_encoding(self):
        body, ctype = encode_multipart({"a": 1}, "map.png", b"IMGDATA")
        boundary = ctype.split("boundary=")[1]
        assert ctype.startswith("multipart/form-data")
        assert b'name="payload_json"' in body
        assert b'{"a": 1}' in body
        assert b'name="files[0]"; filename="map.png"' in body
        assert b"Content-Type: image/png" in body
        assert b"IMGDATA" in body
        assert body.endswith(f"--{boundary}--\r\n".encode())

    def test_client_sends_multipart_on_execute_and_edit(self):
        opener = FakeOpener(b'{"id": "5"}', b"{}")
        client = WebhookClient(WEBHOOK, opener=opener)
        payload = build_map_payload(snapshot(), "map.png")
        assert client.execute(payload, ("map.png", b"X")) == "5"
        client.edit("5", payload, ("map.png", b"X"))
        for req in opener.requests:
            assert req.get_header("Content-type").startswith(
                "multipart/form-data")
        # attachments survive the PATCH field filter (they replace the
        # old image); username does not.
        patch_body = opener.requests[1].data
        assert b'"attachments"' in patch_body
        assert b'"username"' not in patch_body


class MapClient(FakeClient):
    def edit(self, message_id, payload, file=None):
        self.edits.append((message_id, payload, file))
        if self.edit_error:
            raise self.edit_error
    def execute(self, payload, file=None):
        self.posts.append((payload, file))
        if self.post_error:
            raise self.post_error
        return self.post_id


class TestMapWorker:
    def cfg(self, path, **over):
        return enabled_cfg(map_enabled=True, map_source="file",
                           map_path=path, **over)

    def test_posts_map_then_skips_unchanged(self, tmp_path):
        p = write(tmp_path / "world.png")
        client = MapClient()
        w, _ = make_worker(client)
        r = w.process_map(self.cfg(p), snapshot(), "map")
        assert r.ok and r.kind == "map" and r.message_id == "new-id"
        assert client.posts[0][1] == ("map.png", b"\x89PNG fake")
        r2 = w.process_map(self.cfg(p), snapshot(), "map")
        assert r2.ok and "unchanged" in r2.message
        assert len(client.posts) == 1

    def test_manual_always_uploads(self, tmp_path):
        p = write(tmp_path / "world.png")
        client = MapClient()
        w, _ = make_worker(client)
        w.process_map(self.cfg(p), snapshot(), "map")
        w.process_map(self.cfg(p), snapshot(), "manual")
        assert len(client.posts) == 2

    def test_edit_mode_edits_map_message(self, tmp_path):
        p = write(tmp_path / "world.jpg")
        client = MapClient()
        w, _ = make_worker(client)
        r = w.process_map(self.cfg(p, map_message_id="77"), snapshot(), "map")
        assert r.ok and r.message_id == "77"
        assert client.edits[0][0] == "77"
        assert client.edits[0][2][0] == "map.jpg"

    def test_deleted_map_message_reposts(self, tmp_path):
        p = write(tmp_path / "world.png")
        client = MapClient(edit_error=WebhookError(404, "Unknown Message"))
        w, _ = make_worker(client)
        r = w.process_map(self.cfg(p, map_message_id="77"), snapshot(), "map")
        assert r.ok and r.message_id == "new-id"

    def test_missing_image_reports_error(self, tmp_path):
        client = MapClient()
        w, _ = make_worker(client)
        r = w.process_map(self.cfg(str(tmp_path)), snapshot(), "map")
        assert not r.ok and "no map image" in r.message
        assert not client.posts

    def test_oversized_image_rejected(self, tmp_path):
        p = write(tmp_path / "huge.png", b"\0" * (MAX_UPLOAD_BYTES + 1))
        client = MapClient()
        w, _ = make_worker(client)
        r = w.process_map(self.cfg(p), snapshot(), "map")
        assert not r.ok and "10 MB" in r.message
        assert not client.posts


class TestMapScheduler:
    def test_map_due_after_interval_only_when_enabled(self):
        clock = FakeClock()
        s = DiscordStatusScheduler(clock)
        cfg = enabled_cfg(map_enabled=True, map_interval_mins=30)
        s.on_server_start(cfg)
        clock.advance(30 * 60 - 1)
        assert not s.map_due(cfg)
        clock.advance(1)
        assert s.map_due(cfg)
        assert not s.map_due(enabled_cfg(map_enabled=False))
        s.mark_map_posted(cfg)
        assert not s.map_due(cfg)

    def test_no_map_while_stopped(self):
        clock = FakeClock()
        s = DiscordStatusScheduler(clock)
        cfg = enabled_cfg(map_enabled=True, map_interval_mins=1)
        s.on_server_start(cfg)
        s.on_server_stop(cfg)
        clock.advance(3600)
        assert not s.map_due(cfg)

    def test_map_config_normalized(self):
        cfg = normalize_config({"map_interval_mins": 0,
                                "map_command_wait_secs": -5,
                                "map_enabled": "yes"})
        assert cfg["map_interval_mins"] == 1
        assert cfg["map_command_wait_secs"] == 0
        assert cfg["map_enabled"] is True


class TestWorkerQueue:
    def test_status_and_map_jobs_both_delivered(self, tmp_path):
        import threading
        p = write(tmp_path / "world.png")
        got = []
        lock = threading.Lock()
        def on_result(r):
            with lock:
                got.append(r.kind)
        w = DiscordStatusWorker(on_result=on_result,
                                ip_lookup=lambda: "1.1.1.1",
                                client_factory=lambda url: MapClient())
        w.submit(enabled_cfg(map_path=p), snapshot(), "manual", kind="map")
        w.submit(enabled_cfg(), snapshot(), "manual")
        assert w.wait_idle(5)
        w.shutdown()
        assert sorted(got) == ["map", "status"]
