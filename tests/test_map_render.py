"""Tests for core.map_render + the built-in map source of the Discord worker."""
import sqlite3
import struct
import zlib

from core.map_render import (draw_markers, downsample, encode_png, parse_ppm,
                             render_savegame_png)
from core.world_db import encode_chunk_pos
from core.discord_status import DiscordStatusWorker, make_default_config

WEBHOOK = "https://discord.com/api/webhooks/123456789/abcDEF-_xyz"


def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def packed(field, values):
    body = b"".join(varint(v) for v in values)
    return varint(field << 3 | 2) + varint(len(body)) + body


def make_savegame(path, size=4, height=130):
    conn = sqlite3.connect(path)
    for t in ("chunk", "mapchunk", "mapregion"):
        conn.execute(f"CREATE TABLE {t} (position INTEGER PRIMARY KEY, data BLOB)")
    conn.execute("CREATE TABLE gamedata (savegameid INTEGER PRIMARY KEY, data BLOB)")
    conn.execute("INSERT INTO gamedata VALUES (1, NULL)")
    for cx in range(size):
        for cz in range(size):
            h = [height + cx * 3 + (i % 32) // 8 for i in range(1024)]
            blob = packed(3, h) + packed(7, h)
            conn.execute("INSERT INTO mapchunk VALUES (?, ?)",
                         (encode_chunk_pos(cx, 0, cz), blob))
    conn.commit()
    conn.close()
    return path


def decode_png(png):
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, ihdr = 8, b"", None
    while pos < len(png):
        (n,) = struct.unpack(">I", png[pos:pos + 4])
        tag = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + n]
        (crc,) = struct.unpack(">I", png[pos + 8 + n:pos + 12 + n])
        assert crc == zlib.crc32(tag + data) & 0xFFFFFFFF
        if tag == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", data)
        elif tag == b"IDAT":
            idat += data
        pos += 12 + n
    w, h = ihdr[0], ihdr[1]
    raw = zlib.decompress(idat)
    assert len(raw) == h * (1 + w * 3)
    return w, h, raw


class TestPngHelpers:
    def test_parse_ppm(self):
        w, h, rgb = parse_ppm(b"P6 2 1 255\n" + bytes(6))
        assert (w, h, len(rgb)) == (2, 1, 6)

    def test_parse_ppm_rejects_bad_length(self):
        try:
            parse_ppm(b"P6 2 2 255\n" + bytes(6))
        except ValueError:
            return
        raise AssertionError("expected ValueError")

    def test_encode_png_round_trip(self):
        rgb = bytes(range(2 * 3 * 3))
        w, h, raw = decode_png(encode_png(3, 2, rgb))
        assert (w, h) == (3, 2)
        assert raw[0] == 0 and raw[1:10] == rgb[:9]
        assert raw[10] == 0 and raw[11:20] == rgb[9:18]

    def test_downsample_takes_every_kth_pixel(self):
        rgb = bytes(v for i in range(8) for v in (i, i, i))   # 4x2, value=index
        w, h, out = downsample(4, 2, rgb, 2)
        assert (w, h) == (2, 1)
        assert out == bytes([0, 0, 0, 2, 2, 2])

    def test_downsample_k1_is_identity(self):
        assert downsample(1, 1, b"abc", 1) == (1, 1, b"abc")

    def test_markers_clip_at_edges(self):
        out = draw_markers(3, 3, bytes(27), [(0, 0)], radius=1)
        assert out[0:3] == bytes((0, 229, 255))       # centre filled
        assert len(out) == 27


class TestRenderSavegame:
    def test_renders_valid_png(self, tmp_path):
        path = make_savegame(str(tmp_path / "world.vcdbs"))
        w, h, _raw = decode_png(render_savegame_png(path))
        assert w > 0 and h > 0

    def test_respects_max_side(self, tmp_path):
        path = make_savegame(str(tmp_path / "world.vcdbs"), size=8)
        full_w, _, _ = decode_png(render_savegame_png(path))
        w, h, _ = decode_png(render_savegame_png(path, max_side=full_w // 2))
        assert max(w, h) <= full_w // 2

    def test_not_a_savegame_raises(self, tmp_path):
        p = tmp_path / "junk.vcdbs"
        p.write_bytes(b"not sqlite")
        try:
            render_savegame_png(str(p))
        except Exception:
            return
        raise AssertionError("expected an error")


class FakeClient:
    def __init__(self):
        self.posts = []
    def execute(self, payload, file=None):
        self.posts.append((payload, file))
        return "m1"
    def edit(self, *a, **k):
        raise AssertionError("no edit expected")


def make_worker(renderer=None):
    client = FakeClient()
    calls = []
    def fake_render(path, **kw):
        calls.append((path, kw))
        return b"\x89PNG-fake"
    w = DiscordStatusWorker(on_result=lambda r: None,
                            ip_lookup=lambda: "1.1.1.1",
                            client_factory=lambda url: client,
                            map_renderer=renderer or fake_render)
    cfg = make_default_config()
    cfg.update(enabled=True, webhook_url=WEBHOOK, map_enabled=True)
    return w, client, calls, cfg


class TestWorkerSavegameSource:
    def test_default_source_is_savegame(self):
        assert make_default_config()["map_source"] == "savegame"

    def test_renders_and_uploads_png(self, tmp_path):
        path = make_savegame(str(tmp_path / "w.vcdbs"))
        w, client, calls, cfg = make_worker()
        snap = {"savegame": path, "players": ["Bob", "Alice"],
                "server_name": "S"}
        r = w.process_map(cfg, snap, "map")
        assert r.ok and r.kind == "map" and r.message_id == "m1"
        assert client.posts[0][1] == ("map.png", b"\x89PNG-fake")
        assert calls[0][0] == path
        assert calls[0][1]["mark_players"] == ("Alice", "Bob")

    def test_unchanged_world_is_skipped_unless_manual(self, tmp_path):
        path = make_savegame(str(tmp_path / "w.vcdbs"))
        w, client, calls, cfg = make_worker()
        snap = {"savegame": path, "players": []}
        w.process_map(cfg, snap, "map")
        r = w.process_map(cfg, snap, "map")
        assert r.ok and "skipped" in r.message and len(calls) == 1
        w.process_map(cfg, snap, "manual")
        assert len(calls) == 2

    def test_player_change_counts_as_changed(self, tmp_path):
        path = make_savegame(str(tmp_path / "w.vcdbs"))
        w, client, calls, cfg = make_worker()
        w.process_map(cfg, {"savegame": path, "players": []}, "map")
        w.process_map(cfg, {"savegame": path, "players": ["Ann"]}, "map")
        assert len(calls) == 2

    def test_marking_can_be_turned_off(self, tmp_path):
        path = make_savegame(str(tmp_path / "w.vcdbs"))
        w, client, calls, cfg = make_worker()
        cfg["map_mark_players"] = False
        w.process_map(cfg, {"savegame": path, "players": ["Ann"]}, "map")
        assert calls[0][1]["mark_players"] == ()

    def test_no_savegame_reports_error(self, tmp_path):
        w, client, calls, cfg = make_worker()
        r = w.process_map(cfg, {"savegame": ""}, "map")
        assert not r.ok and "world folder" in r.message and r.kind == "map"
        assert not client.posts

    def test_render_failure_reported(self, tmp_path):
        path = make_savegame(str(tmp_path / "w.vcdbs"))
        def boom(path, **kw):
            raise ValueError("corrupt")
        w, client, calls, cfg = make_worker(renderer=boom)
        r = w.process_map(cfg, {"savegame": path}, "map")
        assert not r.ok and "corrupt" in r.message
        assert not client.posts

    def test_real_renderer_end_to_end(self, tmp_path):
        path = make_savegame(str(tmp_path / "w.vcdbs"))
        client = FakeClient()
        w = DiscordStatusWorker(on_result=lambda r: None,
                                ip_lookup=lambda: None,
                                client_factory=lambda url: client)
        cfg = make_default_config()
        cfg.update(enabled=True, webhook_url=WEBHOOK, map_enabled=True)
        r = w.process_map(cfg, {"savegame": path, "players": []}, "manual")
        assert r.ok, r.message
        decode_png(client.posts[0][1][1])
