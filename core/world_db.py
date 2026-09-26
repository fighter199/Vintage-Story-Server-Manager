"""
core/world_db.py — Read and prune a Vintage Story savegame (.vcdbs).

A .vcdbs file is a SQLite database. The tables this module touches:

    chunk      (position INTEGER PRIMARY KEY, data BLOB)   32³ block chunks
    mapchunk   (position INTEGER PRIMARY KEY, data BLOB)   per-column data
    mapregion  (position INTEGER PRIMARY KEY, data BLOB)   512×512 regions
    gamedata   (savegameid INTEGER PRIMARY KEY, data BLOB) SaveGame record

`position` is a packed ChunkPos (see encode_chunk_pos). mapchunk and
mapregion rows use the same packing with y = 0; region coordinates are
chunk coordinates // REGION_CHUNKS.

Blobs are protobuf. We only need a handful of fields, so a tiny wire
reader is used instead of a protobuf dependency:

    mapchunk  field 3  RainHeightMap              packed ushort[1024]
              field 7  WorldGenTerrainHeightMap   packed ushort[1024]
    SaveGame  fields 1/2/3  MapSizeX / MapSizeY / MapSizeZ

Deleting a chunk column means removing its mapchunk row and every
dimension-0 chunk row above it; the server regenerates the column from
the world seed the next time a player gets near it. Map regions are
only removed once no column inside them is left.

Built for savegames of hundreds of GB: nothing ever scans a whole
table. Rows are located with indexed key lookups (plan_rows), the
whole-world view samples a bounded number of columns (load_overview),
zoomed-in areas are read by key range (load_detail), and the rewrite
path copies only the data being kept (rewrite_savegame).

No Tk here — the map window lives in ui/world_map.py.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from array import array
from bisect import bisect_left, bisect_right
from pathlib import Path
from typing import Callable, Iterator, Optional


CHUNK_SIZE = 32
REGION_CHUNKS = 16                  # map regions are 512×512 blocks
HEIGHTMAP_LEN = CHUNK_SIZE * CHUNK_SIZE

DEFAULT_MAP_SIZE_X = 1024000
DEFAULT_MAP_SIZE_Y = 256
DEFAULT_MAP_SIZE_Z = 1024000

# Protobuf field numbers
MAPCHUNK_RAIN_HEIGHT_FIELD = 3
MAPCHUNK_TERRAIN_HEIGHT_FIELD = 7
SAVEGAME_MAP_SIZE_FIELDS = {1: "map_size_x", 2: "map_size_y", 3: "map_size_z"}


class WorldDbError(Exception):
    """Unreadable/unsupported savegame, or a refused write."""


# ----------------------------------------------------------------------
# ChunkPos packing
# ----------------------------------------------------------------------
# bit  0-20  x (21-bit two's complement)    bit 21     guard (0)
# bit 22-26  dimension low 5 bits           bit 27-47  z (21-bit)
# bit 48     guard (0)                      bit 49-53  dimension high
# bit 54-62  y (chunk y, 0..511)            bit 63     reserved (0)
_COORD_MASK = (1 << 21) - 1
_COORD_SIGN = 1 << 20
_COORD_RANGE = 1 << 21
_Z_SHIFT = 27
_Y_SHIFT = 54
_Y_MAX = 0x1FF
_DIM_LO_SHIFT = 22
_DIM_HI_SHIFT = 49
_GUARD_BITS = (1 << 21) | (1 << 48) | (1 << 63)


def encode_chunk_pos(x: int, y: int, z: int, dim: int = 0) -> int:
    if not (-_COORD_SIGN <= x < _COORD_SIGN and -_COORD_SIGN <= z < _COORD_SIGN):
        raise ValueError(f"chunk x/z out of range: {x}, {z}")
    if not 0 <= y <= _Y_MAX:
        raise ValueError(f"chunk y out of range: {y}")
    if not 0 <= dim < 1024:
        raise ValueError(f"dimension out of range: {dim}")
    return ((x & _COORD_MASK)
            | ((dim & 0x1F) << _DIM_LO_SHIFT)
            | ((z & _COORD_MASK) << _Z_SHIFT)
            | ((dim >> 5) << _DIM_HI_SHIFT)
            | (y << _Y_SHIFT))


def decode_chunk_pos(pos: int) -> tuple[int, int, int, int]:
    """Return (x, y, z, dimension). Raises ValueError on a malformed key."""
    pos &= 0xFFFFFFFFFFFFFFFF
    if pos & _GUARD_BITS:
        raise ValueError(f"not a ChunkPos: {pos:#x}")
    x = pos & _COORD_MASK
    if x & _COORD_SIGN:
        x -= _COORD_RANGE
    z = (pos >> _Z_SHIFT) & _COORD_MASK
    if z & _COORD_SIGN:
        z -= _COORD_RANGE
    y = (pos >> _Y_SHIFT) & _Y_MAX
    dim = ((pos >> _DIM_LO_SHIFT) & 0x1F) | (((pos >> _DIM_HI_SHIFT) & 0x1F) << 5)
    return x, y, z, dim


def sea_level(map_size_y: int) -> int:
    """Same formula as the game's TerraGenConfig (110 for a 256-high world)."""
    return int(0.4313725490196078 * map_size_y)


# ----------------------------------------------------------------------
# Protobuf wire reading
# ----------------------------------------------------------------------
def read_varint(buf, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    try:
        while True:
            b = buf[pos]
            pos += 1
            result |= (b & 0x7F) << shift
            if b < 0x80:
                return result, pos
            shift += 7
            if shift > 63:
                raise ValueError("varint too long")
    except IndexError:
        raise ValueError("truncated varint") from None


def iter_fields(buf) -> Iterator[tuple[int, int, object]]:
    """Yield (field_number, wire_type, value) for each top-level field.

    Varints yield an int; length-delimited fields yield a (start, end)
    slice into `buf` so large payloads aren't copied."""
    pos = 0
    end = len(buf)
    while pos < end:
        key, pos = read_varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = read_varint(buf, pos)
        elif wire == 2:
            length, pos = read_varint(buf, pos)
            value = (pos, pos + length)
            pos += length
        elif wire == 1:
            value = (pos, pos + 8)
            pos += 8
        elif wire == 5:
            value = (pos, pos + 4)
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        if pos > end:
            raise ValueError("truncated field")
        yield field, wire, value


_VARINT = rb"[\x80-\xff]*[\x00-\x7f]"
_PATTERNS: dict[tuple, re.Pattern] = {}


def _pattern(kind: str, tag: bytes, n: int = 0) -> re.Pattern:
    """Cached regexes over varint streams. `tag` prefixes every element
    (b"" for packed arrays, the field's tag bytes for unpacked ones):
      "skip" — exactly n elements, "one" — one element (value in group 1),
      "run"  — as many consecutive elements as there are."""
    key = (kind, tag, n)
    pat = _PATTERNS.get(key)
    if pat is None:
        t = re.escape(tag)
        if kind == "skip":
            pat = re.compile(rb"(?:%s%s){%d}" % (t, _VARINT, n))
        elif kind == "one":
            pat = re.compile(rb"%s(%s)" % (t, _VARINT))
        else:
            pat = re.compile(rb"(?:%s%s)+" % (t, _VARINT))
        _PATTERNS[key] = pat
    return pat


def sample_varints(buf, start: int, end: int, indices: list[int],
                   tag: bytes = b"") -> Optional[list[int]]:
    """Decode the elements at `indices` (sorted, ascending) of a repeated
    varint field occupying buf[start:end], without decoding the rest —
    the regex engine skips the elements in between. `tag` is b"" for a
    packed array, or the tag bytes repeated before each element of an
    unpacked one (protobuf-net's default, which the game uses). Returns
    None if the array is shorter than the last index."""
    out = []
    pos = start
    prev = -1
    one = _pattern("one", tag)
    for idx in indices:
        gap = idx - prev - 1
        if gap:
            m = _pattern("skip", tag, gap).match(buf, pos, end)
            if m is None:
                return None
            pos = m.end()
        m = one.match(buf, pos, end)
        if m is None:
            return None
        tok = m.group(1)
        out.append(tok[0] if len(tok) == 1 else read_varint(tok, 0)[0])
        pos = m.end()
        prev = idx
    return out


def sample_packed_varints(buf, start: int, end: int,
                          indices: list[int]) -> Optional[list[int]]:
    """sample_varints for a packed (length-delimited) array."""
    return sample_varints(buf, start, end, indices)


def sample_offsets(samples: int) -> list[int]:
    """Block offsets inside a chunk that a `samples`-per-side grid reads."""
    step = CHUNK_SIZE // samples
    return [i * step + step // 2 for i in range(samples)]


def sample_indices(samples: int) -> list[int]:
    """Heightmap indices (z * 32 + x) for a samples×samples grid, row-major."""
    offs = sample_offsets(samples)
    return [z * CHUNK_SIZE + x for z in offs for x in offs]


def parse_mapchunk_heights(blob, indices: list[int]):
    """Return (rain_heights, terrain_heights) sampled at `indices`.
    Either element is None when that height map is missing/short.

    Real map chunks store each height map unpacked (1024 tag+value
    pairs), so a map chunk has 4000+ top-level fields. Every run of a
    repeated varint field is jumped over with one regex match instead
    of being walked field by field, and the walk stops once both maps
    are found."""
    wanted = (MAPCHUNK_RAIN_HEIGHT_FIELD, MAPCHUNK_TERRAIN_HEIGHT_FIELD)
    found: dict[int, Optional[list[int]]] = {}
    pos = 0
    n = len(blob)
    while pos < n and len(found) < len(wanted):
        start = pos
        key, pos = read_varint(blob, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            tag = bytes(blob[start:pos])
            m = _pattern("run", tag).match(blob, start)
            if m is None:
                raise ValueError("truncated varint field")
            if field in wanted and field not in found:
                found[field] = sample_varints(blob, start, m.end(), indices, tag)
            pos = m.end()
        elif wire == 2:
            length, pos = read_varint(blob, pos)
            if field in wanted and field not in found:
                found[field] = sample_varints(blob, pos, pos + length, indices)
            pos += length
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        if pos > n:
            raise ValueError("truncated field")
    return (found.get(MAPCHUNK_RAIN_HEIGHT_FIELD),
            found.get(MAPCHUNK_TERRAIN_HEIGHT_FIELD))


def parse_savegame_meta(blob) -> dict:
    meta = {"map_size_x": DEFAULT_MAP_SIZE_X,
            "map_size_y": DEFAULT_MAP_SIZE_Y,
            "map_size_z": DEFAULT_MAP_SIZE_Z}
    for field, wire, value in iter_fields(blob):
        name = SAVEGAME_MAP_SIZE_FIELDS.get(field)
        if name and wire == 0 and 0 < value < (1 << 31):
            meta[name] = value
    return meta


# ----------------------------------------------------------------------
# Selection
# ----------------------------------------------------------------------
class ChunkSelection:
    """A set of chunk columns built from add/subtract rectangles.

    Rectangles are inclusive chunk coordinates. Later operations win; the
    optional `base_all` flag starts from "everything" (inverted
    selections). Kept as geometry rather than a set of columns so that
    selecting a whole enormous world costs nothing."""

    def __init__(self):
        self.base_all = False
        self.ops: list[tuple[bool, int, int, int, int]] = []

    def clear(self) -> None:
        self.base_all = False
        self.ops = []

    def add(self, x0: int, z0: int, x1: int, z1: int) -> None:
        self.ops.append((True, min(x0, x1), min(z0, z1), max(x0, x1), max(z0, z1)))

    def subtract(self, x0: int, z0: int, x1: int, z1: int) -> None:
        self.ops.append((False, min(x0, x1), min(z0, z1), max(x0, x1), max(z0, z1)))

    def invert(self) -> None:
        # not(base op1 op2 …) == (not base) with every op's sign flipped.
        self.base_all = not self.base_all
        self.ops = [(not add, *rect) for add, *rect in self.ops]

    def is_empty(self) -> bool:
        return not self.base_all and not any(op[0] for op in self.ops)

    def contains(self, cx: int, cz: int) -> bool:
        for add, x0, z0, x1, z1 in reversed(self.ops):
            if x0 <= cx <= x1 and z0 <= cz <= z1:
                return add
        return self.base_all

    def row_intervals(self, cz: int, xmin: int, xmax: int) -> list[tuple[int, int]]:
        """Selected [a, b] x-runs in row `cz`, clipped to [xmin, xmax]."""
        runs = [(xmin, xmax)] if self.base_all else []
        for add, x0, z0, x1, z1 in self.ops:
            if not z0 <= cz <= z1:
                continue
            a, b = max(x0, xmin), min(x1, xmax)
            if a > b:
                continue
            runs = _union(runs, a, b) if add else _subtract(runs, a, b)
        return runs

    def bands(self, xmin: int, zmin: int, xmax: int, zmax: int):
        """[(z0, z1, runs)] — rows z0..z1 all share the x-runs `runs`.
        Empty bands are dropped; equal neighbours are merged."""
        cuts = {zmin, zmax + 1}
        for _add, _x0, z0, _x1, z1 in self.ops:
            for z in (z0, z1 + 1):
                if zmin < z <= zmax:
                    cuts.add(z)
        cuts = sorted(cuts)
        out: list[tuple[int, int, list]] = []
        for lo, hi in zip(cuts, cuts[1:]):
            runs = self.row_intervals(lo, xmin, xmax)
            if not runs:
                continue
            if out and out[-1][1] == lo - 1 and out[-1][2] == runs:
                out[-1] = (out[-1][0], hi - 1, runs)
            else:
                out.append((lo, hi - 1, runs))
        return out

    def count(self, columns) -> int:
        """How many of the (cx, cz) pairs in `columns` are selected."""
        if not self.ops:
            return len(columns) if self.base_all else 0
        return sum(1 for cx, cz in columns if self.contains(cx, cz))


def _union(runs, a, b):
    out = []
    for x0, x1 in runs:
        if x1 < a - 1 or x0 > b + 1:
            out.append((x0, x1))
        else:
            a, b = min(a, x0), max(b, x1)
    out.append((a, b))
    out.sort()
    return out


def _subtract(runs, a, b):
    out = []
    for x0, x1 in runs:
        if x1 < a or x0 > b:
            out.append((x0, x1))
            continue
        if x0 < a:
            out.append((x0, a - 1))
        if x1 > b:
            out.append((b + 1, x1))
    return out


def _split_at_zero(a: int, b: int):
    # Negative coordinates pack into the top of the 21-bit range, so a
    # run crossing zero isn't one contiguous key range.
    if a < 0 <= b:
        return [(a, -1), (0, b)]
    return [(a, b)]


# ----------------------------------------------------------------------
# Height grid (what the map shows)
# ----------------------------------------------------------------------
MISSING = -32768


class HeightGrid:
    """Sampled surface heights for every generated column.

    Each pixel holds either the surface height (land, >= 0), minus the
    water depth (ocean/lake below sea level, < 0), or MISSING. With
    `samples` > 1 each chunk is samples×samples pixels; with
    `chunks_per_px` > 1 several chunks share a pixel (huge extents)."""

    def __init__(self, min_cx: int, min_cz: int, max_cx: int, max_cz: int,
                 samples: int = 1, chunks_per_px: int = 1):
        self.min_cx, self.min_cz = min_cx, min_cz
        self.max_cx, self.max_cz = max_cx, max_cz
        self.samples = samples
        self.chunks_per_px = chunks_per_px
        self.width = ((max_cx - min_cx) // chunks_per_px + 1) * samples
        self.height = ((max_cz - min_cz) // chunks_per_px + 1) * samples
        self.data = array("h", [MISSING]) * (self.width * self.height)

    @property
    def px_per_chunk(self) -> float:
        return self.samples / self.chunks_per_px

    def chunk_to_px(self, cx: float, cz: float) -> tuple[float, float]:
        ppc = self.px_per_chunk
        return (cx - self.min_cx) * ppc, (cz - self.min_cz) * ppc

    def px_to_chunk(self, px: float, py: float) -> tuple[int, int]:
        ppc = self.px_per_chunk
        return (int(px // ppc) + self.min_cx, int(py // ppc) + self.min_cz)

    def put_chunk(self, cx: int, cz: int, values) -> None:
        s = self.samples
        k = self.chunks_per_px
        px0 = ((cx - self.min_cx) // k) * s
        py0 = ((cz - self.min_cz) // k) * s
        w = self.width
        data = self.data
        if s == 1:
            data[py0 * w + px0] = values[0]
            return
        for row in range(s):
            base = (py0 + row) * w + px0
            data[base:base + s] = array("h", values[row * s:(row + 1) * s])

    def clear_chunk(self, cx: int, cz: int) -> None:
        self.put_chunk(cx, cz, [MISSING] * (self.samples * self.samples))

    def value_at_px(self, px: int, py: int) -> int:
        if 0 <= px < self.width and 0 <= py < self.height:
            return self.data[py * self.width + px]
        return MISSING


def surface_value(rain: Optional[int], terrain: Optional[int], sea: int,
                  map_size_y: int) -> int:
    """Combine the two height maps into one HeightGrid value."""
    if rain is not None and rain >= map_size_y:
        rain = None                        # ushort underflow garbage
    if terrain is not None and terrain >= map_size_y:
        terrain = None
    if terrain is not None and terrain < sea:
        return max(terrain - sea, -32767)
    if rain is not None:
        return rain
    if terrain is not None:
        return terrain
    return MISSING


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------
_LAND_STOPS = [
    (0.00, (78, 120, 64)),
    (0.12, (104, 142, 74)),
    (0.26, (150, 156, 96)),
    (0.42, (138, 112, 82)),
    (0.60, (120, 112, 106)),
    (0.78, (170, 168, 166)),
    (1.00, (246, 246, 250)),
]
_WATER_SHALLOW = (74, 132, 178)
_WATER_DEEP = (22, 44, 96)
_SHADE_STEPS = 6
_WATER_DEPTH_MAX = 48


def _lerp_stops(stops, t):
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return tuple(round(a + (b - a) * f) for a, b in zip(c0, c1))
    return stops[-1][1]


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def render_ppm(grid: HeightGrid, sea: int, map_size_y: int,
               background: str = "#0d0d0d",
               cancel: Callable[[], bool] = lambda: False) -> bytes:
    """Shaded-relief P6 image of the grid (one image pixel per grid pixel)."""
    blocks_per_px = CHUNK_SIZE / grid.px_per_chunk
    span = max(1, map_size_y - sea)
    n_shades = 2 * _SHADE_STEPS + 1
    # land[h * n_shades + shade] -> rgb bytes; slope compares a pixel
    # with its north-west neighbour, like the in-game map's hillshade.
    land = []
    for h in range(map_size_y + 1):
        base = _lerp_stops(_LAND_STOPS, min(1.0, max(0.0, (h - sea) / span)))
        for s in range(-_SHADE_STEPS, _SHADE_STEPS + 1):
            f = 1.0 + s * 0.07
            land.append(bytes(min(255, max(0, round(c * f))) for c in base))
    water = [bytes(_lerp_stops([(0.0, _WATER_SHALLOW), (1.0, _WATER_DEEP)],
                               d / _WATER_DEPTH_MAX))
             for d in range(_WATER_DEPTH_MAX + 1)]
    bg = bytes(_hex_to_rgb(background))
    shade_k = 4.0 / blocks_per_px

    w, h = grid.width, grid.height
    data = grid.data
    out = bytearray(b"P6 %d %d 255\n" % (w, h))
    for y in range(h):
        if y % 64 == 0 and cancel():
            raise InterruptedError("render cancelled")
        row = y * w
        prev_row = row - w if y else row
        west = MISSING
        for x in range(w):
            v = data[row + x]
            if v == MISSING:
                out += bg
            elif v < 0:
                out += water[-v if v > -_WATER_DEPTH_MAX else _WATER_DEPTH_MAX]
            else:
                nw = data[prev_row + x - 1] if x else MISSING
                if nw < 0:                       # water or MISSING
                    nw = west if west >= 0 else v
                shade = round((v - nw) * shade_k)
                if shade > _SHADE_STEPS:
                    shade = _SHADE_STEPS
                elif shade < -_SHADE_STEPS:
                    shade = -_SHADE_STEPS
                out += land[v * n_shades + shade + _SHADE_STEPS]
            west = v
    return bytes(out)




# ----------------------------------------------------------------------
# Column index
# ----------------------------------------------------------------------
class ColumnIndex:
    """Which chunk columns exist, as sorted per-row int arrays (~4 bytes
    per column, so millions of columns stay cheap).

    With step > 1 the world was sampled on a lattice — one column per
    step×step block — and counts are estimates (hits × step²)."""

    def __init__(self, step: int = 1):
        self.step = step
        self.rows: dict[int, array] = {}
        self.row_keys: list[int] = []
        self.hits = 0

    def add(self, cx: int, cz: int) -> None:
        row = self.rows.get(cz)
        if row is None:
            row = self.rows[cz] = array("i")
        row.append(cx)
        self.hits += 1

    def finalize(self) -> None:
        """Sort after bulk add()s."""
        for cz, row in self.rows.items():
            self.rows[cz] = array("i", sorted(row))
        self.row_keys = sorted(self.rows)

    @property
    def estimated_total(self) -> int:
        return self.hits * self.step * self.step

    def contains(self, cx: int, cz: int) -> bool:
        row = self.rows.get(cz)
        if not row:
            return False
        i = bisect_left(row, cx)
        return i < len(row) and row[i] == cx

    def _selected_slices(self, selection: "ChunkSelection", extent):
        keys = self.row_keys
        for z0, z1, runs in selection.bands(*extent):
            for cz in keys[bisect_left(keys, z0):bisect_right(keys, z1)]:
                row = self.rows[cz]
                for a, b in runs:
                    i, j = bisect_left(row, a), bisect_right(row, b)
                    if i < j:
                        yield cz, i, j

    def count(self, selection: "ChunkSelection", extent) -> int:
        """Indexed columns (lattice samples when step > 1) in selection."""
        return sum(j - i for _cz, i, j in self._selected_slices(selection, extent))

    def remove(self, selection: "ChunkSelection", extent) -> list[tuple[int, int]]:
        """Drop the selected columns; returns them as (cx, cz) pairs."""
        removed = []
        for cz, i, j in reversed(list(self._selected_slices(selection, extent))):
            row = self.rows[cz]
            removed.extend((cx, cz) for cx in row[i:j])
            del row[i:j]
        self.hits -= len(removed)
        return removed


# ----------------------------------------------------------------------
# Database access
# ----------------------------------------------------------------------
_RANGE_QUERY = "SELECT position, data FROM mapchunk WHERE position BETWEEN ? AND ?"
_FIRST_KEY = ("SELECT position FROM mapchunk WHERE position BETWEEN ? AND ? "
              "ORDER BY position LIMIT 1")
_LAST_KEY = ("SELECT position FROM mapchunk WHERE position BETWEEN ? AND ? "
             "ORDER BY position DESC LIMIT 1")
_DIM0_KEY_END = 1 << 48          # every dimension-0, y = 0 key is below this
_MIN_KEY = -(1 << 63)
_MAX_KEY = (1 << 63) - 1

OVERVIEW_MAX_READS = 150_000     # map chunks read for the whole-world view
MAX_GRID_PIXELS = 4_000_000
DELETE_BATCH_CHUNKS = 20_000     # chunk rows per in-place delete transaction


def _never() -> bool:
    return False


def _connect(path: str, readonly: bool, timeout: float = 5.0) -> sqlite3.Connection:
    if not os.path.isfile(path):
        raise WorldDbError(f"Savegame not found: {path}")
    uri = _file_uri(path) + ("?mode=ro" if readonly else "?mode=rw")
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=timeout,
                               isolation_level=None, check_same_thread=False)
    except sqlite3.Error as e:
        raise WorldDbError(f"Could not open savegame: {e}") from e
    return conn


def _file_uri(path: str) -> str:
    return Path(os.path.abspath(path)).as_uri()


def _check_schema(conn: sqlite3.Connection, schema: str = "main") -> None:
    try:
        names = {r[0] for r in conn.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError as e:
        raise WorldDbError(f"Not a readable savegame database: {e}") from e
    missing = {"chunk", "mapchunk", "mapregion", "gamedata"} - names
    if missing:
        raise WorldDbError(
            "Not a Vintage Story savegame (missing tables: "
            + ", ".join(sorted(missing)) + ")")


def _open_for_read(path: str) -> sqlite3.Connection:
    """Read-only connection with the schema verified. A WAL-mode file
    whose -shm/-wal sidecars don't exist can't be opened with mode=ro,
    so fall back to a normal connection (which this module only ever
    SELECTs through)."""
    conn = _connect(path, readonly=True)
    try:
        _check_schema(conn)
        return conn
    except WorldDbError as e:
        conn.close()
        if not isinstance(e.__cause__, sqlite3.OperationalError) \
                or "locked" in str(e.__cause__):
            raise
    conn = _connect(path, readonly=False)
    try:
        _check_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _friendly_sqlite_error(e: Exception) -> str:
    msg = str(e)
    if "locked" in msg or "busy" in msg:
        return ("The savegame is locked by another process — stop the "
                "server (or wait for it to finish saving) and try again.")
    if "full" in msg:
        return "The disk is full."
    return f"Database error: {msg}"


def read_world_meta(path: str) -> dict:
    """Map size + sea level from the SaveGame record (defaults if absent)."""
    conn = _open_for_read(path)
    try:
        row = conn.execute("SELECT data FROM gamedata LIMIT 1").fetchone()
    except sqlite3.Error as e:
        raise WorldDbError(_friendly_sqlite_error(e)) from e
    finally:
        conn.close()
    meta = {"map_size_x": DEFAULT_MAP_SIZE_X, "map_size_y": DEFAULT_MAP_SIZE_Y,
            "map_size_z": DEFAULT_MAP_SIZE_Z}
    if row and row[0]:
        try:
            meta = parse_savegame_meta(row[0])
        except ValueError:
            pass
    meta["sea_level"] = sea_level(meta["map_size_y"])
    meta["file_size"] = os.path.getsize(path)
    return meta


def plan_rows(conn: sqlite3.Connection,
              cancel: Callable[[], bool] = _never) -> list[tuple[int, int, int]]:
    """Every (cz, xmin, xmax) run of dimension-0 map chunks.

    Found with a few indexed first/last-key lookups per row instead of a
    table scan, so this is fast even on huge savegames. A row holding
    both negative and non-negative x yields two runs."""
    runs = []
    start = 0
    while start < _DIM0_KEY_END:
        row = conn.execute(_FIRST_KEY, (start, _DIM0_KEY_END - 1)).fetchone()
        if row is None:
            break
        base = ((row[0] >> _Z_SHIFT) & _COORD_MASK) << _Z_SHIFT
        for lo, hi in ((base, base + _COORD_SIGN - 1),
                       (base + _COORD_SIGN, base + _COORD_MASK)):
            first = conn.execute(_FIRST_KEY, (lo, hi)).fetchone()
            if first is None:
                continue
            last = conn.execute(_LAST_KEY, (lo, hi)).fetchone()
            x0, _y, cz, _d = decode_chunk_pos(first[0])
            runs.append((cz, x0, decode_chunk_pos(last[0])[0]))
        start = base + (1 << _Z_SHIFT)
        if cancel():
            raise InterruptedError("load cancelled")
    return runs


def _read_columns(cursor, indices, sea, msy, put, stats) -> None:
    n = len(indices)
    for position, blob in cursor:
        try:
            cx, cy, cz, dim = decode_chunk_pos(position)
            if dim or cy:
                continue
            rain, terrain = parse_mapchunk_heights(blob or b"", indices)
        except ValueError:
            stats["skipped"] += 1
            continue
        put(cx, cz, [surface_value(rain[i] if rain else None,
                                   terrain[i] if terrain else None, sea, msy)
                     for i in range(n)])


def load_overview(path: str, meta: dict,
                  max_reads: int = OVERVIEW_MAX_READS,
                  max_pixels: int = MAX_GRID_PIXELS,
                  progress: Optional[Callable[[float], None]] = None,
                  cancel: Callable[[], bool] = _never):
    """Read enough of the savegame to draw the whole world.

    Worlds up to `max_reads` columns are read completely. Bigger ones are
    sampled on a lattice (one column per step×step block, step a power
    of two), so load time stays bounded however large the savegame is;
    zoomed-in areas are then filled in by load_detail().

    Returns (grid, index, skipped)."""
    sea, msy = meta["sea_level"], meta["map_size_y"]
    stats = {"skipped": 0}
    conn = _open_for_read(path)
    try:
        runs = plan_rows(conn, cancel)
        if not runs:
            raise WorldDbError("The savegame has no generated terrain yet.")
        estimate = sum(x1 - x0 + 1 for _cz, x0, x1 in runs)
        xmin, xmax = min(r[1] for r in runs), max(r[2] for r in runs)
        zmin, zmax = min(r[0] for r in runs), max(r[0] for r in runs)
        span_x, span_z = xmax - xmin + 1, zmax - zmin + 1
        step = 1
        while (estimate > max_reads * step * step
               or (span_x // step + 1) * (span_z // step + 1) > max_pixels):
            step *= 2
        samples = 2 if step == 1 and span_x * span_z * 4 <= max_pixels else 1
        # Align the lattice so each grid pixel is exactly one block.
        gx0, gz0 = xmin - xmin % step, zmin - zmin % step
        grid = HeightGrid(gx0, gz0, xmax, zmax, samples=samples,
                          chunks_per_px=step)
        index = ColumnIndex(step)
        indices = sample_indices(samples)

        def put(cx, cz, values):
            grid.put_chunk(cx, cz, values)
            index.add(cx, cz)

        if step == 1:
            done = 0
            for cz, x0, x1 in runs:
                cur = conn.execute(_RANGE_QUERY, (encode_chunk_pos(x0, 0, cz),
                                                  encode_chunk_pos(x1, 0, cz)))
                _read_columns(cur, indices, sea, msy, put, stats)
                done += x1 - x0 + 1
                if progress:
                    progress(done / estimate)
                if cancel():
                    raise InterruptedError("load cancelled")
        else:
            phase = step // 2
            keys = []
            for cz, x0, x1 in runs:
                if (cz - gz0) % step == phase:
                    first = x0 + (gx0 + phase - x0) % step
                    keys.extend(encode_chunk_pos(x, 0, cz)
                                for x in range(first, x1 + 1, step))
            batch = 500
            for i in range(0, len(keys), batch):
                part = keys[i:i + batch]
                cur = conn.execute(
                    "SELECT position, data FROM mapchunk WHERE position IN (%s)"
                    % ",".join("?" * len(part)), part)
                _read_columns(cur, indices, sea, msy, put, stats)
                if progress:
                    progress(min(1.0, (i + batch) / len(keys)))
                if cancel():
                    raise InterruptedError("load cancelled")
    except sqlite3.Error as e:
        raise WorldDbError(_friendly_sqlite_error(e)) from e
    finally:
        conn.close()
    index.finalize()
    return grid, index, stats["skipped"]


def load_detail(path: str, meta: dict, rect: tuple[int, int, int, int],
                samples: int, cancel: Callable[[], bool] = _never):
    """Full-resolution grid + exact ColumnIndex for the chunk rectangle
    rect = (cx0, cz0, cx1, cz1). One key-range query per row, so the
    cost is proportional to the area, not the world."""
    cx0, cz0, cx1, cz1 = rect
    sea, msy = meta["sea_level"], meta["map_size_y"]
    grid = HeightGrid(cx0, cz0, cx1, cz1, samples=samples)
    index = ColumnIndex(1)
    indices = sample_indices(samples)
    stats = {"skipped": 0}

    def put(cx, cz, values):
        grid.put_chunk(cx, cz, values)
        index.add(cx, cz)

    conn = _open_for_read(path)
    try:
        for cz in range(cz0, cz1 + 1):
            for a, b in _split_at_zero(cx0, cx1):
                cur = conn.execute(_RANGE_QUERY, (encode_chunk_pos(a, 0, cz),
                                                  encode_chunk_pos(b, 0, cz)))
                _read_columns(cur, indices, sea, msy, put, stats)
            if cancel():
                raise InterruptedError("load cancelled")
    except sqlite3.Error as e:
        raise WorldDbError(_friendly_sqlite_error(e)) from e
    finally:
        conn.close()
    index.finalize()
    return grid, index, stats["skipped"]


# ----------------------------------------------------------------------
# Pruning
# ----------------------------------------------------------------------
def _max_chunk_y(conn: sqlite3.Connection, map_size_y: int,
                 schema: str = "main") -> int:
    top = -(-map_size_y // CHUNK_SIZE) - 1
    row = conn.execute(f"SELECT position FROM {schema}.chunk "
                       "ORDER BY position DESC LIMIT 1").fetchone()
    if row is not None:
        try:
            top = max(top, decode_chunk_pos(row[0])[1])
        except ValueError:
            pass
    return min(top, _Y_MAX)


def _region_has_columns(conn: sqlite3.Connection, rx: int, rz: int) -> bool:
    x0 = rx * REGION_CHUNKS
    x1 = x0 + REGION_CHUNKS - 1
    for cz in range(rz * REGION_CHUNKS, (rz + 1) * REGION_CHUNKS):
        for a, b in _split_at_zero(x0, x1):
            if conn.execute(
                    "SELECT 1 FROM mapchunk WHERE position BETWEEN ? AND ? LIMIT 1",
                    (encode_chunk_pos(a, 0, cz), encode_chunk_pos(b, 0, cz)),
            ).fetchone():
                return True
    return False


def _touched_regions(bands) -> set[tuple[int, int]]:
    out = set()
    for z0, z1, runs in bands:
        for rz in range(z0 // REGION_CHUNKS, z1 // REGION_CHUNKS + 1):
            for a, b in runs:
                for rx in range(a // REGION_CHUNKS, b // REGION_CHUNKS + 1):
                    out.add((rx, rz))
    return out


def _delete_empty_regions(conn: sqlite3.Connection, regions) -> int:
    removed = 0
    for rx, rz in sorted(regions):
        if not _region_has_columns(conn, rx, rz):
            removed += conn.execute("DELETE FROM mapregion WHERE position = ?",
                                    (encode_chunk_pos(rx, 0, rz),)).rowcount
    return removed


def _begin_write(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as e:
        raise WorldDbError(_friendly_sqlite_error(e)) from e


def delete_chunk_columns(path: str, selection: ChunkSelection,
                         extent: tuple[int, int, int, int],
                         map_size_y: int = DEFAULT_MAP_SIZE_Y,
                         delete_empty_regions: bool = True,
                         batch_chunks: int = DELETE_BATCH_CHUNKS,
                         progress: Optional[Callable[[float], None]] = None,
                         cancel: Callable[[], bool] = _never) -> dict:
    """Delete every dimension-0 chunk column in `selection` within
    `extent` = (min_cx, min_cz, max_cx, max_cz), in place.

    Work is committed in batches of whole rows (every column is either
    fully deleted or untouched) so SQLite's rollback journal stays small
    on huge worlds. Cancelling rolls back only the current batch and
    sets stats["cancelled"]. The file keeps its size — the server reuses
    the freed pages for new terrain; rewrite_savegame() shrinks it.

    The server must not be running. Returns row counts:
    {"chunks", "mapchunks", "mapregions", "cancelled"}."""
    xmin, zmin, xmax, zmax = extent
    bands = selection.bands(xmin, zmin, xmax, zmax)
    total_rows = sum(z1 - z0 + 1 for z0, z1, _ in bands)
    stats = {"chunks": 0, "mapchunks": 0, "mapregions": 0, "cancelled": False}
    if not total_rows:
        return stats

    conn = _connect(path, readonly=False, timeout=2.0)
    try:
        _check_schema(conn)
        top_y = _max_chunk_y(conn, map_size_y)
        pending = {"chunks": 0, "mapchunks": 0}
        done = 0
        _begin_write(conn)
        try:
            for z0, z1, runs in bands:
                key_runs = [part for a, b in runs for part in _split_at_zero(a, b)]
                for cz in range(z0, z1 + 1):
                    for a, b in key_runs:
                        pending["mapchunks"] += conn.execute(
                            "DELETE FROM mapchunk WHERE position BETWEEN ? AND ?",
                            (encode_chunk_pos(a, 0, cz), encode_chunk_pos(b, 0, cz)),
                        ).rowcount
                        for cy in range(top_y + 1):
                            pending["chunks"] += conn.execute(
                                "DELETE FROM chunk WHERE position BETWEEN ? AND ?",
                                (encode_chunk_pos(a, cy, cz),
                                 encode_chunk_pos(b, cy, cz)),
                            ).rowcount
                    done += 1
                    if cancel():
                        raise InterruptedError("delete cancelled")
                    if pending["chunks"] >= batch_chunks:
                        conn.execute("COMMIT")
                        for k in pending:
                            stats[k] += pending[k]
                            pending[k] = 0
                        _begin_write(conn)
                    if progress and (done % 16 == 0 or done == total_rows):
                        progress(done / total_rows)
            if delete_empty_regions:
                stats["mapregions"] = _delete_empty_regions(
                    conn, _touched_regions(bands))
            conn.execute("COMMIT")
            for k in pending:
                stats[k] += pending[k]
        except InterruptedError:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            stats["cancelled"] = True
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as e:
        raise WorldDbError(_friendly_sqlite_error(e)) from e
    finally:
        conn.close()
    return stats


def selected_key_ranges(selection: ChunkSelection,
                        extent: tuple[int, int, int, int],
                        ys) -> list[tuple[int, int]]:
    """Sorted, non-overlapping [lo, hi] key ranges covering the selected
    dimension-0 columns at each chunk level in `ys`."""
    out = []
    bands = selection.bands(*extent)
    for y in ys:
        for z0, z1, runs in bands:
            parts = [p for a, b in runs for p in _split_at_zero(a, b)]
            for cz in range(z0, z1 + 1):
                for a, b in parts:
                    out.append((encode_chunk_pos(a, y, cz), encode_chunk_pos(b, y, cz)))
    out.sort()
    return out


def complement_ranges(ranges) -> list[tuple[int, int]]:
    """Key ranges NOT covered by the sorted `ranges` (whole int64 space)."""
    out = []
    lo = _MIN_KEY
    for a, b in ranges:
        if a > lo:
            out.append((lo, a - 1))
        lo = max(lo, b + 1)
    if lo <= _MAX_KEY:
        out.append((lo, _MAX_KEY))
    return out


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _remove_quietly(*paths: str) -> None:
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def _replace_with_retry(src: str, dst: str, attempts: int = 8) -> None:
    # Windows: antivirus / indexers can hold a file for a moment.
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.5)


_SIDECARS = ("-wal", "-shm", "-journal")


def rewrite_savegame(path: str, selection: ChunkSelection,
                     extent: tuple[int, int, int, int],
                     map_size_y: int = DEFAULT_MAP_SIZE_Y,
                     backup_path: Optional[str] = None,
                     delete_empty_regions: bool = True,
                     expected_size: Optional[int] = None,
                     progress: Optional[Callable[[float], None]] = None,
                     cancel: Callable[[], bool] = _never) -> dict:
    """Write a new savegame without the selected columns and swap it in.

    Only the data being KEPT is read and written — SQLite copies it with
    INSERT … SELECT over key ranges, entirely in C — so the cost follows
    what's kept, not what's deleted, and the new file is compact (no
    VACUUM needed). Needs free space for the kept data.

    The original is renamed to `backup_path` (an instant backup; must be
    on the same drive — falls back to next to the savegame otherwise) or
    deleted after the swap when backup_path is None. Cancelling or any
    error leaves the original untouched.

    Returns {"size_before", "size_after", "mapchunks_kept", "mapregions",
    "backup_path"}."""
    tmp = path + ".vssm-rewrite"
    _remove_quietly(tmp, *(tmp + s for s in _SIDECARS))
    if not os.path.isfile(path):
        raise WorldDbError(f"Savegame not found: {path}")
    size_before = os.path.getsize(path)
    target = max(1, expected_size or size_before)
    last_tick = [0.0]

    def tick() -> int:
        if cancel():
            return 1                                  # interrupts SQLite
        now = time.monotonic()
        if progress and now - last_tick[0] > 0.25:
            last_tick[0] = now
            try:
                progress(min(0.99, os.path.getsize(tmp) / target))
            except OSError:
                pass
        return 0

    stats = {"size_before": size_before, "mapchunks_kept": 0, "mapregions": 0}
    dst = sqlite3.connect(_file_uri(tmp), uri=True, isolation_level=None,
                          check_same_thread=False, timeout=2.0)
    ok = False
    try:
        dst.execute("ATTACH DATABASE ? AS src", (_file_uri(path) + "?mode=rw",))
        _check_schema(dst, "src")
        pragmas = {p: dst.execute(f"PRAGMA src.{p}").fetchone()[0]
                   for p in ("page_size", "auto_vacuum", "user_version",
                             "application_id", "journal_mode")}
        top_y = _max_chunk_y(dst, map_size_y, "src")
        dst.execute(f"PRAGMA main.page_size = {int(pragmas['page_size'])}")
        dst.execute(f"PRAGMA main.auto_vacuum = {int(pragmas['auto_vacuum'])}")
        dst.execute("PRAGMA main.journal_mode = OFF")    # scratch file
        dst.execute("PRAGMA main.synchronous = OFF")
        dst.set_progress_handler(tick, 20_000)
        dst.execute("BEGIN")
        schema = dst.execute("SELECT type, name, sql FROM src.sqlite_master "
                             "WHERE sql IS NOT NULL ORDER BY rowid").fetchall()
        tables = [n for t, n, _s in schema
                  if t == "table" and not n.startswith("sqlite_")]
        for t, n, sql in schema:
            if t == "table" and not n.startswith("sqlite_"):
                dst.execute(sql)
        pruned = {
            "chunk": complement_ranges(
                selected_key_ranges(selection, extent, range(top_y + 1))),
            "mapchunk": complement_ranges(
                selected_key_ranges(selection, extent, [0])),
        }
        for name in tables:
            q = _quote(name)
            if name not in pruned:
                dst.execute(f"INSERT INTO main.{q} SELECT * FROM src.{q}")
                continue
            for lo, hi in pruned[name]:
                n = dst.execute(f"INSERT INTO main.{q} SELECT * FROM src.{q} "
                                "WHERE position BETWEEN ? AND ?", (lo, hi)).rowcount
                if name == "mapchunk":
                    stats["mapchunks_kept"] += n
        if dst.execute("SELECT 1 FROM src.sqlite_master "
                       "WHERE name = 'sqlite_sequence'").fetchone():
            dst.execute("DELETE FROM main.sqlite_sequence")
            dst.execute("INSERT INTO main.sqlite_sequence "
                        "SELECT * FROM src.sqlite_sequence")
        if delete_empty_regions:
            stats["mapregions"] = _delete_empty_regions(
                dst, _touched_regions(selection.bands(*extent)))
        for t, _n, sql in schema:
            if t in ("index", "trigger", "view"):
                dst.execute(sql)
        dst.execute(f"PRAGMA main.user_version = {int(pragmas['user_version'])}")
        dst.execute(f"PRAGMA main.application_id = {int(pragmas['application_id'])}")
        dst.execute("COMMIT")
        dst.set_progress_handler(None, 0)
        dst.execute("DETACH DATABASE src")
        if str(pragmas["journal_mode"]).lower() == "wal":
            dst.execute("PRAGMA main.journal_mode = WAL")
        ok = True
    except sqlite3.OperationalError as e:
        if "interrupt" in str(e):
            raise InterruptedError("rewrite cancelled") from e
        raise WorldDbError(_friendly_sqlite_error(e)) from e
    except sqlite3.Error as e:
        raise WorldDbError(_friendly_sqlite_error(e)) from e
    finally:
        dst.close()
        if not ok:
            _remove_quietly(tmp, *(tmp + s for s in _SIDECARS))

    with open(tmp, "rb+") as f:                      # durable before swap
        os.fsync(f.fileno())
    stats["backup_path"] = _swap_in(path, tmp, backup_path)
    stats["size_after"] = os.path.getsize(path)
    if progress:
        progress(1.0)
    return stats


def _swap_in(path: str, tmp: str, backup_path: Optional[str]) -> Optional[str]:
    """Move the original (and its SQLite sidecar files) aside, move the
    rewritten file into place, then drop the original unless it's kept."""
    old = backup_path or path + ".vssm-old"
    try:
        _replace_with_retry(path, old)
    except OSError:
        if not backup_path:
            raise
        old = path + ".pre-prune.bak"             # other drive: keep it here
        _replace_with_retry(path, old)
    for suffix in _SIDECARS:
        if os.path.exists(path + suffix):
            try:
                _replace_with_retry(path + suffix, old + suffix)
            except OSError:
                pass
    try:
        _replace_with_retry(tmp, path)
    except OSError:
        _replace_with_retry(old, path)            # put the original back
        raise
    if backup_path is None:
        _remove_quietly(old, *(old + s for s in _SIDECARS))
        return None
    return old
