"""
core/map_render.py — Render a savegame's world map to a PNG, headless.

Used by the Discord poster to share the same shaded-relief map the WORLD
MAP window draws, without opening that window or touching Tk (which
can't be used from a worker thread). The terrain comes from
core/world_db.py (load_overview + render_ppm); this module only turns
the resulting PPM into a PNG of a shareable size, optionally marking
player positions. Stdlib only: PNG is written with zlib.

Reading the savegame is read-only and safe while the server runs.
"""
from __future__ import annotations

import struct
import zlib
from typing import Iterable

from .world_db import (CHUNK_SIZE, MAX_GRID_PIXELS, load_detail,
                       load_overview, read_players, read_world_meta,
                       render_ppm, WorldDbError)

# Background for chunks that were never generated.
MAP_BACKGROUND = "#0d0d0d"
MARKER_FILL    = (0, 229, 255)        # cyan, like the map window
MARKER_EDGE    = (0, 0, 0)

# Worlds up to this many chunk columns are re-read at full detail (the
# overview only has 1–2 px per chunk); same budget as the map window.
DETAIL_MAX_COLUMNS = 30_000
DETAIL_MAX_SAMPLES = 8
# Small results are enlarged (nearest neighbour) to at least this size
# so they don't show up as a thumbnail in Discord.
MIN_SIDE = 1024


# ----------------------------------------------------------------------
# PPM / RGB helpers
# ----------------------------------------------------------------------
def parse_ppm(ppm: bytes) -> tuple[int, int, bytes]:
    """(width, height, rgb) from a binary P6 PPM as written by
    world_db.render_ppm ("P6 W H 255\\n" + pixels)."""
    end = ppm.index(b"\n")
    parts = ppm[:end].split()
    if len(parts) != 4 or parts[0] != b"P6" or parts[3] != b"255":
        raise ValueError("not an 8-bit P6 PPM")
    w, h = int(parts[1]), int(parts[2])
    rgb = ppm[end + 1:]
    if len(rgb) != w * h * 3:
        raise ValueError("PPM pixel data has the wrong length")
    return w, h, rgb


def downsample(w: int, h: int, rgb: bytes, k: int) -> tuple[int, int, bytes]:
    """Keep every k-th pixel of every k-th row (nearest neighbour)."""
    if k <= 1:
        return w, h, rgb
    w2, h2 = (w + k - 1) // k, (h + k - 1) // k
    out = bytearray(w2 * h2 * 3)
    stride = w * 3
    step = 3 * k
    for y2 in range(h2):
        row = rgb[y2 * k * stride:(y2 * k + 1) * stride]
        base = y2 * w2 * 3
        for c in range(3):
            out[base + c:base + w2 * 3:3] = row[c::step]
    return w2, h2, bytes(out)


def upscale(w: int, h: int, rgb: bytes, k: int) -> tuple[int, int, bytes]:
    """Enlarge by an integer factor (each pixel becomes a k×k block)."""
    if k <= 1:
        return w, h, rgb
    stride = w * 3
    out = bytearray()
    for y in range(h):
        row = rgb[y * stride:(y + 1) * stride]
        wide = b"".join(row[i:i + 3] * k for i in range(0, stride, 3))
        out += wide * k
    return w * k, h * k, bytes(out)


def draw_markers(w: int, h: int, rgb: bytes,
                 points: Iterable[tuple[float, float]],
                 radius: int = 4) -> bytes:
    """Square markers (with a 1 px dark edge) centred on each (x, y)."""
    buf = bytearray(rgb)
    for px, py in points:
        cx, cy = int(round(px)), int(round(py))
        for y in range(cy - radius - 1, cy + radius + 2):
            if not 0 <= y < h:
                continue
            for x in range(cx - radius - 1, cx + radius + 2):
                if not 0 <= x < w:
                    continue
                edge = (abs(x - cx) > radius) or (abs(y - cy) > radius)
                i = (y * w + x) * 3
                buf[i:i + 3] = bytes(MARKER_EDGE if edge else MARKER_FILL)
    return bytes(buf)


def encode_png(w: int, h: int, rgb: bytes, level: int = 9) -> bytes:
    """8-bit truecolour PNG from packed RGB rows (filter type 0)."""
    stride = w * 3
    raw = bytearray()
    for y in range(h):
        raw += b"\x00"
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), level))
            + chunk(b"IEND", b""))


# ----------------------------------------------------------------------
# Savegame → PNG
# ----------------------------------------------------------------------
def render_savegame_png(path: str, mark_players: Iterable[str] = (),
                        max_side: int = 2048,
                        max_bytes: int = 10 * 1024 * 1024) -> bytes:
    """Render the whole explored world of `path` (.vcdbs) as a PNG.

    mark_players: names of players to mark at their last-saved
    position (positions are only as fresh as the server's last save).
    The image is shrunk to fit `max_side` and, if the PNG still exceeds
    `max_bytes`, halved until it fits. Raises WorldDbError / OSError /
    ValueError on unreadable savegames.
    """
    meta = read_world_meta(path)
    grid, index, _skipped = load_overview(path, meta)
    # Small worlds: re-read the whole extent at full detail, at the
    # largest power-of-two sampling that fits max_side and the pixel cap.
    span_x = grid.max_cx - grid.min_cx + 1
    span_z = grid.max_cz - grid.min_cz + 1
    if index.step == 1 and span_x * span_z <= DETAIL_MAX_COLUMNS:
        samples = DETAIL_MAX_SAMPLES
        while samples > grid.samples and (
                max(span_x, span_z) * samples > max_side
                or span_x * span_z * samples * samples > MAX_GRID_PIXELS):
            samples //= 2
        if samples > grid.samples:
            grid, _index, _ = load_detail(
                path, meta,
                (grid.min_cx, grid.min_cz, grid.max_cx, grid.max_cz), samples)
    ppm = render_ppm(grid, meta["sea_level"], meta["map_size_y"],
                     background=MAP_BACKGROUND)
    w, h, rgb = parse_ppm(ppm)

    points: list[tuple[float, float]] = []
    wanted = {n.lower() for n in mark_players if n}
    if wanted:
        try:
            players, _ = read_players(path)
        except (WorldDbError, OSError):
            players = []
        for p in players:
            if p.get("dimension", 0) != 0:
                continue
            if (p.get("name") or "").lower() in wanted:
                points.append(grid.chunk_to_px(p["x"] / CHUNK_SIZE,
                                               p["z"] / CHUNK_SIZE))

    # Enlarge small maps (never past max_side), shrink big ones.
    up = max(1, min(MIN_SIDE // max(w, h), max_side // max(w, h)))
    if up > 1:
        w, h, rgb = upscale(w, h, rgb, up)
        points = [(x * up, y * up) for x, y in points]

    k = max(1, -(-max(w, h) // max(1, max_side)))      # ceil division
    while True:
        w2, h2, small = downsample(w, h, rgb, k)
        if points:
            radius = max(4, max(w2, h2) // 120)
            small = draw_markers(w2, h2, small,
                                 ((x / k, y / k) for x, y in points),
                                 radius=radius)
        png = encode_png(w2, h2, small)
        if len(png) <= max_bytes or max(w2, h2) <= 64:
            return png
        k *= 2
