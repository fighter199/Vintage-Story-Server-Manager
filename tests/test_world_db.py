"""Tests for core.world_db — savegame reading, selection and pruning."""
import os
import random
import sqlite3

import pytest

from core.world_db import (
    MISSING,
    ChunkSelection,
    ColumnIndex,
    HeightGrid,
    WorldDbError,
    complement_ranges,
    decode_chunk_pos,
    delete_chunk_columns,
    encode_chunk_pos,
    iter_fields,
    load_detail,
    load_overview,
    parse_mapchunk_heights,
    parse_savegame_meta,
    read_varint,
    plan_rows,
    read_world_meta,
    render_ppm,
    rewrite_savegame,
    sample_indices,
    sample_packed_varints,
    sea_level,
    selected_key_ranges,
    surface_value,
)


# ----------------------------------------------------------------------
# Synthetic savegame helpers
# ----------------------------------------------------------------------
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


def f_varint(field, value):
    return varint(field << 3) + varint(value)


def f_bytes(field, payload):
    return varint((field << 3) | 2) + varint(len(payload)) + payload


def f_packed(field, values):
    return f_bytes(field, b"".join(varint(v) for v in values))


def mapchunk_blob(rain, terrain, packed=True):
    extra = f_varint(1, 5) + f_bytes(2, b"\x00" * 40)
    if packed:
        return extra + f_packed(3, rain) + f_bytes(5, b"zz") + f_packed(7, terrain)
    body = b"".join(f_varint(3, v) for v in rain)
    body += b"".join(f_varint(7, v) for v in terrain)
    return extra + body


def column_heights(cx, cz):
    # Land (terrain >= 110) east of cx 3, sea to the west.
    terrain = 90 + cx * 5
    return [max(terrain, 110)] * 1024, [terrain] * 1024


def make_world(path, x0=0, z0=0, w=20, h=20, ysize=8, blob_size=300):
    conn = sqlite3.connect(str(path))
    for table in ("chunk", "mapchunk", "mapregion"):
        conn.execute(f"CREATE TABLE {table} (position integer PRIMARY KEY, data BLOB)")
    conn.execute("CREATE TABLE gamedata (savegameid integer PRIMARY KEY, data BLOB)")
    conn.execute("CREATE TABLE playerdata (playerid integer PRIMARY KEY "
                 "AUTOINCREMENT, playeruid TEXT, data BLOB)")
    conn.execute("INSERT INTO gamedata VALUES (1, ?)",
                 (f_varint(1, 1024000) + f_varint(2, 256) + f_varint(3, 1024000)
                  + f_bytes(11, b"moddata"),))
    regions = set()
    for cz in range(z0, z0 + h):
        for cx in range(x0, x0 + w):
            rain, terrain = column_heights(cx - x0, cz - z0)
            conn.execute("INSERT INTO mapchunk VALUES (?, ?)",
                         (encode_chunk_pos(cx, 0, cz), mapchunk_blob(rain, terrain)))
            for cy in range(ysize):
                conn.execute("INSERT INTO chunk VALUES (?, ?)",
                             (encode_chunk_pos(cx, cy, cz), b"c" * blob_size))
            regions.add((cx // 16, cz // 16))
    for rx, rz in regions:
        conn.execute("INSERT INTO mapregion VALUES (?, ?)",
                     (encode_chunk_pos(rx, 0, rz), b"r" * 100))
    # A mini-dimension chunk that pruning must never touch.
    conn.execute("INSERT INTO chunk VALUES (?, ?)",
                 (encode_chunk_pos(x0, 0, z0, dim=1), b"dim1"))
    conn.commit()
    conn.close()
    return str(path)


def table_positions(path, table):
    conn = sqlite3.connect(path)
    try:
        return {decode_chunk_pos(p) for (p,) in conn.execute(
            f"SELECT position FROM {table}")}
    finally:
        conn.close()


# ----------------------------------------------------------------------
# ChunkPos packing
# ----------------------------------------------------------------------
class TestChunkPos:
    def test_known_bit_positions(self):
        assert encode_chunk_pos(1, 0, 0) == 1
        assert encode_chunk_pos(0, 0, 1) == 1 << 27
        assert encode_chunk_pos(0, 1, 0) == 1 << 54
        assert encode_chunk_pos(0, 0, 0, dim=1) == 1 << 22
        assert encode_chunk_pos(0, 0, 0, dim=32) == 1 << 49

    def test_round_trip(self):
        for pos in [(0, 0, 0, 0), (16000, 3, 16002, 0), (-1, 0, -1, 0),
                    (-(1 << 20), 511, (1 << 20) - 1, 0), (5, 7, 9, 1),
                    (123, 2, 456, 1023), (31999, 0, 31999, 31)]:
            assert decode_chunk_pos(encode_chunk_pos(*pos)) == pos

    def test_keys_fit_a_signed_sqlite_integer(self):
        assert encode_chunk_pos(-1, 511, -1, 1023) < (1 << 63)

    def test_rejects_guard_bits(self):
        for bad in (1 << 21, 1 << 48, 1 << 63):
            with pytest.raises(ValueError):
                decode_chunk_pos(bad)

    def test_rejects_out_of_range(self):
        with pytest.raises(ValueError):
            encode_chunk_pos(1 << 20, 0, 0)
        with pytest.raises(ValueError):
            encode_chunk_pos(0, 512, 0)
        with pytest.raises(ValueError):
            encode_chunk_pos(0, 0, 0, dim=1024)

    def test_x_runs_are_contiguous_on_each_side_of_zero(self):
        pos = [encode_chunk_pos(x, 2, 40) for x in range(0, 50)]
        assert pos == sorted(pos) and pos[-1] - pos[0] == 49
        neg = [encode_chunk_pos(x, 2, 40) for x in range(-50, 0)]
        assert neg == sorted(neg) and neg[-1] - neg[0] == 49

    def test_sea_level(self):
        assert sea_level(256) == 110
        assert sea_level(512) == 220


# ----------------------------------------------------------------------
# Protobuf wire reading
# ----------------------------------------------------------------------
class TestWireFormat:
    def test_read_varint(self):
        for n in (0, 1, 127, 128, 300, 16383, 16384, 65535, 1 << 40):
            assert read_varint(varint(n), 0) == (n, len(varint(n)))

    def test_truncated_varint_raises(self):
        with pytest.raises(ValueError):
            read_varint(b"\x80\x80", 0)

    def test_iter_fields(self):
        msg = (f_varint(1, 300) + f_bytes(2, b"abc")
               + varint((3 << 3) | 5) + b"\x01\x02\x03\x04"
               + varint((4 << 3) | 1) + b"\x00" * 8)
        fields = list(iter_fields(msg))
        assert [(f, w) for f, w, _ in fields] == [(1, 0), (2, 2), (3, 5), (4, 1)]
        assert fields[0][2] == 300
        start, end = fields[1][2]
        assert msg[start:end] == b"abc"

    def test_truncated_field_raises(self):
        with pytest.raises(ValueError):
            list(iter_fields(f_bytes(2, b"abcdef")[:-2]))

    def test_sample_packed_varints(self):
        values = [i * 97 % 70000 for i in range(1024)]
        buf = b"".join(varint(v) for v in values)
        idx = [0, 1, 5, 300, 1023]
        assert sample_packed_varints(buf, 0, len(buf), idx) == [values[i] for i in idx]

    def test_sample_packed_varints_short_array(self):
        buf = b"".join(varint(v) for v in range(10))
        assert sample_packed_varints(buf, 0, len(buf), [3, 12]) is None

    def test_sample_indices(self):
        assert sample_indices(1) == [16 * 32 + 16]
        assert sample_indices(2) == [8 * 32 + 8, 8 * 32 + 24,
                                     24 * 32 + 8, 24 * 32 + 24]
        assert len(sample_indices(8)) == 64

    def test_parse_mapchunk_packed_and_unpacked(self):
        rain = [100 + (i % 50) for i in range(1024)]
        terrain = [200 + (i % 7) for i in range(1024)]
        idx = sample_indices(4)
        want = ([rain[i] for i in idx], [terrain[i] for i in idx])
        for packed in (True, False):
            blob = mapchunk_blob(rain, terrain, packed=packed)
            assert parse_mapchunk_heights(blob, idx) == want

    def test_parse_mapchunk_real_layout(self):
        # Layout seen in a real savegame: unpacked arrays for fields 3, 7,
        # 12 and 13 with single fields in between.
        rain = [109 + i % 11 for i in range(1024)]
        terrain = [20000 + i for i in range(1024)]       # 3-byte varints
        blob = (b"".join(f_varint(3, v) for v in rain) + f_varint(4, 1)
                + b"".join(f_varint(7, v) for v in terrain) + f_varint(10, 2)
                + f_bytes(11, b"moddata")
                + b"".join(f_varint(12, 5) for _ in range(1024)))
        idx = sample_indices(8)
        assert parse_mapchunk_heights(blob, idx) == (
            [rain[i] for i in idx], [terrain[i] for i in idx])

    def test_parse_mapchunk_unpacked_skips_other_repeated_fields(self):
        rain = list(range(1024))
        blob = (b"".join(f_varint(2, 7) for _ in range(50))
                + b"".join(f_varint(3, v) for v in rain))
        assert parse_mapchunk_heights(blob, [0, 1023]) == ([0, 1023], None)

    def test_parse_mapchunk_short_unpacked_array(self):
        blob = b"".join(f_varint(3, v) for v in range(100))
        assert parse_mapchunk_heights(blob, [5, 500]) == (None, None)

    def test_parse_mapchunk_missing_maps(self):
        assert parse_mapchunk_heights(f_varint(1, 1), [0]) == (None, None)

    def test_parse_savegame_meta(self):
        blob = f_varint(1, 64000) + f_varint(2, 384) + f_varint(3, 32000) + f_bytes(11, b"x")
        assert parse_savegame_meta(blob) == {
            "map_size_x": 64000, "map_size_y": 384, "map_size_z": 32000}

    def test_parse_savegame_meta_defaults(self):
        assert parse_savegame_meta(f_bytes(11, b"x"))["map_size_y"] == 256


# ----------------------------------------------------------------------
# Selection geometry
# ----------------------------------------------------------------------
def _brute(sel, xmin, zmin, xmax, zmax):
    return {(x, z) for z in range(zmin, zmax + 1) for x in range(xmin, xmax + 1)
            if sel.contains(x, z)}


def _from_bands(sel, xmin, zmin, xmax, zmax):
    out = set()
    for z0, z1, runs in sel.bands(xmin, zmin, xmax, zmax):
        for z in range(z0, z1 + 1):
            for a, b in runs:
                out.update((x, z) for x in range(a, b + 1))
    return out


class TestChunkSelection:
    def test_empty(self):
        sel = ChunkSelection()
        assert sel.is_empty()
        assert not sel.contains(0, 0)
        assert sel.bands(0, 0, 9, 9) == []

    def test_add_subtract_later_wins(self):
        sel = ChunkSelection()
        sel.add(0, 0, 9, 9)
        sel.subtract(3, 3, 5, 5)
        sel.add(4, 4, 4, 4)
        assert sel.contains(0, 0) and sel.contains(4, 4)
        assert not sel.contains(3, 3) and not sel.contains(10, 0)

    def test_rect_corners_normalised(self):
        sel = ChunkSelection()
        sel.add(9, 9, 0, 0)
        assert sel.contains(0, 0) and sel.contains(9, 9)

    def test_invert(self):
        sel = ChunkSelection()
        sel.add(2, 2, 4, 4)
        sel.invert()
        assert not sel.contains(3, 3) and sel.contains(0, 0)
        assert not sel.is_empty()
        sel.invert()
        assert sel.contains(3, 3) and not sel.contains(0, 0)

    def test_bands_match_contains_randomised(self):
        rng = random.Random(1234)
        for _ in range(60):
            sel = ChunkSelection()
            for _ in range(rng.randint(0, 6)):
                x0, z0 = rng.randint(-8, 12), rng.randint(-8, 12)
                x1, z1 = rng.randint(-8, 12), rng.randint(-8, 12)
                (sel.add if rng.random() < 0.6 else sel.subtract)(x0, z0, x1, z1)
            if rng.random() < 0.4:
                sel.invert()
            ext = (-5, -5, 9, 9)
            assert _from_bands(sel, *ext) == _brute(sel, *ext)

    def test_bands_merge_equal_rows(self):
        sel = ChunkSelection()
        sel.add(0, 0, 4, 9)
        assert sel.bands(0, 0, 20, 20) == [(0, 9, [(0, 4)])]

    def test_count(self):
        sel = ChunkSelection()
        cols = {(x, z) for x in range(10) for z in range(10)}
        assert sel.count(cols) == 0
        sel.base_all = True
        assert sel.count(cols) == 100
        sel.subtract(0, 0, 4, 9)
        assert sel.count(cols) == 50


# ----------------------------------------------------------------------
# Height grid + rendering
# ----------------------------------------------------------------------
class TestHeightGrid:
    def test_put_and_read_samples(self):
        g = HeightGrid(10, 20, 12, 21, samples=2)
        assert (g.width, g.height) == (6, 4)
        g.put_chunk(11, 21, [1, 2, 3, 4])
        assert [g.value_at_px(x, y) for y in (2, 3) for x in (2, 3)] == [1, 2, 3, 4]
        assert g.value_at_px(0, 0) == MISSING
        assert g.value_at_px(99, 0) == MISSING
        g.clear_chunk(11, 21)
        assert g.value_at_px(2, 2) == MISSING

    def test_coordinate_mapping(self):
        g = HeightGrid(100, 200, 110, 205, samples=4)
        assert g.chunk_to_px(101, 201) == (4, 4)
        assert g.px_to_chunk(7.9, 4.0) == (101, 201)

    def test_chunks_per_px(self):
        g = HeightGrid(0, 0, 7, 7, samples=1, chunks_per_px=4)
        assert (g.width, g.height) == (2, 2)
        g.put_chunk(5, 1, [42])
        assert g.value_at_px(1, 0) == 42
        assert g.px_to_chunk(1, 0) == (4, 0)

    def test_surface_value(self):
        assert surface_value(120, 118, 110, 256) == 120          # land
        assert surface_value(110, 80, 110, 256) == -30           # 30 deep
        assert surface_value(None, None, 110, 256) == MISSING
        assert surface_value(65535, 130, 110, 256) == 130         # underflow
        assert surface_value(140, None, 110, 256) == 140

    def test_render_ppm(self):
        g = HeightGrid(0, 0, 2, 1, samples=1)
        g.put_chunk(0, 0, [150])
        g.put_chunk(1, 0, [-10])
        ppm = render_ppm(g, 110, 256, background="#010203")
        header = b"P6 3 2 255\n"
        assert ppm.startswith(header)
        pixels = ppm[len(header):]
        assert len(pixels) == 3 * 2 * 3
        assert pixels[6:9] == b"\x01\x02\x03"                     # missing
        blue = pixels[3:6]
        assert blue[2] > blue[0]                                  # water


class TestColumnIndex:
    def _index(self, step=1):
        idx = ColumnIndex(step)
        for cz in range(5):
            for cx in (9, 3, 5, 1, 7):
                idx.add(cx, cz)
        idx.finalize()
        return idx

    def test_contains_and_sorting(self):
        idx = self._index()
        assert list(idx.rows[0]) == [1, 3, 5, 7, 9]
        assert idx.contains(5, 4) and not idx.contains(4, 4) and not idx.contains(5, 9)

    def test_count_and_remove(self):
        idx = self._index()
        sel = ChunkSelection()
        sel.add(2, 1, 6, 3)
        ext = (0, 0, 9, 4)
        assert idx.count(sel, ext) == 6
        removed = idx.remove(sel, ext)
        assert sorted(removed) == sorted((x, z) for z in (1, 2, 3) for x in (3, 5))
        assert idx.hits == 19 and idx.count(sel, ext) == 0
        assert not idx.contains(3, 2) and idx.contains(1, 2)

    def test_estimate_scales_with_step(self):
        idx = self._index(step=4)
        assert idx.estimated_total == 25 * 16


# ----------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------
class TestWorldDatabase:
    def test_read_world_meta(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", w=2, h=2)
        meta = read_world_meta(path)
        assert meta["map_size_y"] == 256
        assert meta["sea_level"] == 110
        assert meta["file_size"] == os.path.getsize(path)

    def test_rejects_non_savegame(self, tmp_path):
        path = str(tmp_path / "other.vcdbs")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE foo (x)")
        conn.commit()
        conn.close()
        with pytest.raises(WorldDbError):
            read_world_meta(path)

    def test_missing_file(self, tmp_path):
        with pytest.raises(WorldDbError):
            read_world_meta(str(tmp_path / "nope.vcdbs"))

    def test_plan_rows(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", x0=100, z0=200, w=8, h=5)
        conn = sqlite3.connect(path)
        # A second, detached patch in one row and a mini-dimension key.
        rain, terrain = column_heights(0, 0)
        conn.execute("INSERT INTO mapchunk VALUES (?, ?)",
                     (encode_chunk_pos(300, 0, 202), mapchunk_blob(rain, terrain)))
        conn.execute("INSERT INTO mapchunk VALUES (?, ?)",
                     (encode_chunk_pos(5, 0, 203, dim=1), mapchunk_blob(rain, terrain)))
        conn.commit()
        runs = plan_rows(conn)
        conn.close()
        assert runs == [(200, 100, 107), (201, 100, 107), (202, 100, 300),
                        (203, 100, 107), (204, 100, 107)]

    def test_plan_rows_negative_x(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", x0=-3, z0=0, w=6, h=1)
        conn = sqlite3.connect(path)
        assert sorted(plan_rows(conn)) == [(0, -3, -1), (0, 0, 2)]
        conn.close()

    def test_load_overview_full(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", x0=100, z0=200, w=8, h=5)
        meta = read_world_meta(path)
        grid, index, skipped = load_overview(path, meta)
        assert skipped == 0
        assert index.step == 1 and index.hits == 40 and index.estimated_total == 40
        assert index.contains(107, 204) and not index.contains(108, 204)
        assert (grid.min_cx, grid.min_cz, grid.max_cx, grid.max_cz) == (100, 200, 107, 204)
        assert grid.samples == 2 and (grid.width, grid.height) == (16, 10)
        # Column 0: terrain 90 < sea level 110 -> 20 deep.
        assert grid.value_at_px(0, 0) == -20
        # Column 5: terrain 115 -> land at 115.
        assert grid.value_at_px(10, 0) == 115

    def test_load_overview_samples_big_worlds(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", x0=1000, z0=2000, w=32, h=32)
        meta = read_world_meta(path)
        reads = []
        grid, index, _ = load_overview(path, meta, max_reads=100,
                                       progress=reads.append)
        # 1024 columns > 100 reads -> step 4 lattice (1024 / 16 = 64 reads).
        assert index.step == 4
        assert index.hits == 64 and index.estimated_total == 1024
        assert grid.chunks_per_px == 4 and grid.samples == 1
        assert (grid.width, grid.height) == (8, 8)
        assert all(v != MISSING for v in grid.data)
        # Samples sit in the middle of each 4x4 block.
        assert index.contains(1002, 2002) and not index.contains(1000, 2000)
        assert reads and reads[-1] == 1.0

    def test_load_overview_respects_pixel_budget(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", w=8, h=8)
        meta = read_world_meta(path)
        grid, _, _ = load_overview(path, meta, max_pixels=20)
        assert grid.width * grid.height <= 20

    def test_load_overview_ignores_other_dimensions(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", w=2, h=2)
        conn = sqlite3.connect(path)
        rain, terrain = column_heights(0, 0)
        conn.execute("INSERT INTO mapchunk VALUES (?, ?)",
                     (encode_chunk_pos(50, 0, 50, dim=1), mapchunk_blob(rain, terrain)))
        conn.commit()
        conn.close()
        _, index, _ = load_overview(path, read_world_meta(path))
        assert not index.contains(50, 50) and index.hits == 4

    def test_load_overview_empty_world(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", w=0, h=0)
        with pytest.raises(WorldDbError, match="no generated terrain"):
            load_overview(path, read_world_meta(path))

    def test_load_detail(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", x0=100, z0=200, w=8, h=5)
        meta = read_world_meta(path)
        grid, index, _ = load_detail(path, meta, (104, 202, 110, 203), samples=8)
        assert index.hits == 8                   # x 104..107 exist, rows 202-203
        assert (grid.width, grid.height) == (7 * 8, 2 * 8)
        assert grid.value_at_px(8, 0) == 115     # column x=105 -> terrain 115
        assert grid.value_at_px(6 * 8, 0) == MISSING

    def test_delete_rectangle(self, tmp_path):
        # 40x20 columns spanning regions (0,0), (1,0) and (2,0).
        path = make_world(tmp_path / "w.vcdbs", x0=0, z0=0, w=40, h=16)
        sel = ChunkSelection()
        sel.add(16, 0, 39, 15)            # regions (1,0) fully, (2,0) fully
        sel.subtract(20, 3, 20, 3)        # ...except one column in region 1
        stats = delete_chunk_columns(path, sel, (0, 0, 39, 15))

        deleted = {(x, z) for x in range(16, 40) for z in range(16)} - {(20, 3)}
        assert stats["mapchunks"] == len(deleted)
        assert stats["chunks"] == len(deleted) * 8
        assert stats["mapregions"] == 1                    # only region 2
        mapchunks = {(x, z) for x, _y, z, _d in table_positions(path, "mapchunk")}
        assert not mapchunks & deleted
        assert len(mapchunks) == 40 * 16 - len(deleted)
        chunks = table_positions(path, "chunk")
        assert (0, 0, 0, 1) in chunks                      # other dimension kept
        assert not {(x, z) for x, _y, z, d in chunks if d == 0} & deleted
        regions = {(x, z) for x, _y, z, _d in table_positions(path, "mapregion")}
        assert regions == {(0, 0), (1, 0)}

    def test_delete_keeps_regions_when_asked(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", w=32, h=16)
        sel = ChunkSelection()
        sel.add(16, 0, 31, 15)
        stats = delete_chunk_columns(path, sel, (0, 0, 31, 15),
                                     delete_empty_regions=False)
        assert stats["mapregions"] == 0
        assert len(table_positions(path, "mapregion")) == 2

    def test_delete_inverted_selection(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", x0=1000, z0=1000, w=12, h=12)
        sel = ChunkSelection()
        sel.base_all = True
        sel.subtract(1004, 1004, 1006, 1005)
        delete_chunk_columns(path, sel, (1000, 1000, 1011, 1011))
        left = {(x, z) for x, _y, z, _d in table_positions(path, "mapchunk")}
        assert left == {(x, z) for x in range(1004, 1007) for z in range(1004, 1006)}

    def test_delete_negative_coordinates(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", x0=-4, z0=-4, w=8, h=8)
        sel = ChunkSelection()
        sel.add(-2, -2, 1, 1)
        stats = delete_chunk_columns(path, sel, (-4, -4, 3, 3))
        assert stats["mapchunks"] == 16
        left = {(x, z) for x, _y, z, _d in table_positions(path, "mapchunk")}
        assert not any(-2 <= x <= 1 and -2 <= z <= 1 for x, z in left)
        assert len(left) == 48

    def test_delete_covers_chunks_above_world_height(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", w=2, h=2, ysize=12)
        sel = ChunkSelection()
        sel.base_all = True
        stats = delete_chunk_columns(path, sel, (0, 0, 1, 1), map_size_y=256)
        assert stats["chunks"] == 4 * 12

    def test_cancel_rolls_back_current_batch(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", w=10, h=40)
        sel = ChunkSelection()
        sel.base_all = True
        stats = delete_chunk_columns(path, sel, (0, 0, 9, 39), cancel=lambda: True)
        assert stats["cancelled"] and stats["mapchunks"] == 0
        assert len(table_positions(path, "mapchunk")) == 400

    def test_batches_commit_and_cancel_keeps_whole_rows(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", w=10, h=40)
        sel = ChunkSelection()
        sel.base_all = True
        calls = []

        def cancel():
            calls.append(1)
            return len(calls) > 25                 # stop during row 26

        # 10 columns x 8 chunks = 80 chunk rows per z-row; batch = 2 rows.
        stats = delete_chunk_columns(path, sel, (0, 0, 9, 39),
                                     batch_chunks=160, cancel=cancel)
        assert stats["cancelled"]
        assert stats["mapchunks"] == 240 and stats["chunks"] == 240 * 8
        left = {(x, z) for x, _y, z, _d in table_positions(path, "mapchunk")}
        assert left == {(x, z) for x in range(10) for z in range(24, 40)}
        chunks = {(x, z) for x, _y, z, d in table_positions(path, "chunk") if d == 0}
        assert chunks == left                      # no half-deleted columns

    def test_progress_reaches_one(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", w=4, h=20)
        sel = ChunkSelection()
        sel.base_all = True
        seen = []
        delete_chunk_columns(path, sel, (0, 0, 3, 19), progress=seen.append)
        assert seen[-1] == 1.0

    def test_locked_database(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", w=2, h=2)
        holder = sqlite3.connect(path, isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            sel = ChunkSelection()
            sel.base_all = True
            with pytest.raises(WorldDbError, match="locked"):
                delete_chunk_columns(path, sel, (0, 0, 1, 1))
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        assert len(table_positions(path, "mapchunk")) == 4

    def test_empty_selection_is_a_no_op(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", w=2, h=2)
        stats = delete_chunk_columns(path, ChunkSelection(), (0, 0, 1, 1))
        assert stats == {"chunks": 0, "mapchunks": 0, "mapregions": 0,
                         "cancelled": False}



# ----------------------------------------------------------------------
# Rewrite (copy what's kept, swap it in)
# ----------------------------------------------------------------------
def _dump(path):
    conn = sqlite3.connect(path)
    try:
        out = {}
        for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            out[name] = sorted(conn.execute(f'SELECT * FROM "{name}"').fetchall())
        return out
    finally:
        conn.close()


class TestKeyRanges:
    def test_selected_key_ranges(self):
        sel = ChunkSelection()
        sel.add(2, 5, 4, 6)
        ranges = selected_key_ranges(sel, (0, 0, 9, 9), [0, 1])
        assert ranges == sorted([
            (encode_chunk_pos(2, y, z), encode_chunk_pos(4, y, z))
            for y in (0, 1) for z in (5, 6)])

    def test_complement(self):
        full = complement_ranges([])
        assert full == [(-(1 << 63), (1 << 63) - 1)]
        assert complement_ranges([(5, 9), (12, 12)]) == [
            (-(1 << 63), 4), (10, 11), (13, (1 << 63) - 1)]


class TestRewriteSavegame:
    def _world(self, tmp_path, **kw):
        path = make_world(tmp_path / "w.vcdbs", **kw)
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO playerdata (playeruid, data) VALUES ('a', x'01')")
        conn.execute("INSERT INTO playerdata (playeruid, data) VALUES ('b', x'02')")
        conn.execute("DELETE FROM playerdata WHERE playeruid = 'b'")
        conn.execute("CREATE INDEX index_playeruid ON playerdata(playeruid)")
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
        conn.close()
        return path

    def test_matches_in_place_delete(self, tmp_path):
        os.makedirs(tmp_path / "a")
        os.makedirs(tmp_path / "b")
        a = self._world(tmp_path / "a", w=40, h=16)
        b = self._world(tmp_path / "b", w=40, h=16)
        sel = ChunkSelection()
        sel.add(16, 0, 39, 15)
        sel.subtract(20, 3, 20, 3)
        ext = (0, 0, 39, 15)
        delete_chunk_columns(a, sel, ext)
        stats = rewrite_savegame(b, sel, ext)
        assert _dump(a) == _dump(b)
        assert stats["mapregions"] == 1
        assert stats["mapchunks_kept"] == 40 * 16 - (24 * 16 - 1)
        assert stats["size_after"] < stats["size_before"]
        assert stats["backup_path"] is None
        assert sorted(os.listdir(tmp_path / "b")) == ["w.vcdbs"]

    def test_keeps_schema_and_metadata(self, tmp_path):
        path = self._world(tmp_path, w=4, h=4)
        before = _dump(path)
        rewrite_savegame(path, ChunkSelection(), (0, 0, 3, 3))
        assert _dump(path) == before
        conn = sqlite3.connect(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
            assert conn.execute("SELECT seq FROM sqlite_sequence "
                                "WHERE name = 'playerdata'").fetchone()[0] == 2
            assert conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' "
                                "AND name = 'index_playeruid'").fetchone()
        finally:
            conn.close()

    def test_backup_keeps_original(self, tmp_path):
        path = self._world(tmp_path, w=8, h=8)
        original = _dump(path)
        backup = str(tmp_path / "w.vcdbs.pre-prune-test")
        sel = ChunkSelection()
        sel.base_all = True
        sel.subtract(2, 2, 3, 3)
        stats = rewrite_savegame(path, sel, (0, 0, 7, 7), backup_path=backup)
        assert stats["backup_path"] == backup
        assert _dump(backup) == original
        left = {(x, z) for x, _y, z, _d in table_positions(path, "mapchunk")}
        assert left == {(2, 2), (3, 2), (2, 3), (3, 3)}
        assert (0, 0, 0, 1) in table_positions(path, "chunk")   # other dimension

    def test_preserves_wal_mode(self, tmp_path):
        path = self._world(tmp_path, w=4, h=4)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.close()
        rewrite_savegame(path, ChunkSelection(), (0, 0, 3, 3))
        conn = sqlite3.connect(path)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            conn.close()

    def test_cancel_leaves_original_untouched(self, tmp_path):
        path = self._world(tmp_path, w=20, h=20, blob_size=2000)
        before = _dump(path)
        sel = ChunkSelection()
        sel.add(0, 0, 4, 4)
        with pytest.raises(InterruptedError):
            rewrite_savegame(path, sel, (0, 0, 19, 19), cancel=lambda: True)
        assert _dump(path) == before
        assert sorted(os.listdir(tmp_path)) == ["w.vcdbs"]

    def test_locked_source(self, tmp_path):
        path = self._world(tmp_path, w=2, h=2)
        holder = sqlite3.connect(path, isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            with pytest.raises(WorldDbError, match="locked"):
                rewrite_savegame(path, ChunkSelection(), (0, 0, 1, 1))
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        assert sorted(os.listdir(tmp_path)) == ["w.vcdbs"]

    def test_progress(self, tmp_path):
        path = self._world(tmp_path, w=30, h=30, blob_size=3000)
        seen = []
        rewrite_savegame(path, ChunkSelection(), (0, 0, 29, 29),
                         progress=seen.append)
        assert seen[-1] == 1.0


# ----------------------------------------------------------------------
# Player positions
# ----------------------------------------------------------------------
import struct  # noqa: E402

from core.world_db import parse_player_entity, read_players  # noqa: E402


def net_str(s):
    b = s.encode("utf-8")
    n, out = len(b), bytearray()
    while True:
        if n < 0x80:
            out.append(n)
            break
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(out) + b


def attr(kind, key, payload=b""):
    return bytes([kind]) + net_str(key) + payload


def every_attribute_type():
    """A WatchedAttributes tree using each serialized attribute type."""
    stack = b"\x00" + struct.pack("<iii", 1, 42, 3) + attr(1, "q", struct.pack("<i", 1)) + b"\x00"
    tree_arr = struct.pack("<i", 2) + attr(9, "b", b"\x01") + b"\x00" + b"\x00"
    return b"".join([
        attr(6, "animations", b"\x00"),
        attr(1, "int", struct.pack("<i", 7)),
        attr(2, "long", struct.pack("<q", 7)),
        attr(3, "double", struct.pack("<d", 1.5)),
        attr(4, "float", struct.pack("<f", 1.5)),
        attr(5, "text", net_str("x" * 200)),          # 2-byte length prefix
        attr(6, "nametag", attr(5, "name", net_str("Ünïcode_Player"))
             + attr(9, "showtagonlywhentargeted", b"\x00") + b"\x00"),
        attr(7, "held", stack),
        attr(7, "empty", b"\x01"),
        attr(8, "bytes", struct.pack("<H", 3) + b"abc"),
        attr(9, "bool", b"\x01"),
        attr(10, "strings", struct.pack("<i", 2) + net_str("a") + net_str("bc")),
        attr(11, "ints", struct.pack("<i", 2) + b"\x00" * 8),
        attr(12, "floats", struct.pack("<i", 1) + b"\x00" * 4),
        attr(13, "doubles", struct.pack("<i", 1) + b"\x00" * 8),
        attr(14, "trees", tree_arr),
        attr(15, "longs", struct.pack("<i", 1) + b"\x00" * 8),
        attr(16, "bools", struct.pack("<i", 3) + b"\x01\x00\x01"),
    ]) + b"\x00"


def entity_blob(x, y, z, tree=None, with_class=True):
    head = (net_str("EntityPlayer") if with_class else b"") + net_str("1.22.2")
    body = struct.pack("<q", 25) + (every_attribute_type() if tree is None else tree)
    return head + body + struct.pack("<ddd", x, y, z) + b"trailing entity data"


class TestPlayers:
    def test_parse_entity_every_attribute_type(self):
        p = parse_player_entity(entity_blob(511975.5, 112.0, 512034.5))
        assert p == {"name": "Ünïcode_Player", "x": 511975.5, "y": 112.0,
                     "z": 512034.5, "dimension": 0, "game_version": "1.22.2"}

    def test_parse_entity_without_class_prefix(self):
        p = parse_player_entity(entity_blob(1.0, 2.0, 3.0, with_class=False))
        assert (p["x"], p["y"], p["z"]) == (1.0, 2.0, 3.0)

    def test_other_dimension(self):
        p = parse_player_entity(entity_blob(10.0, 2 * 32768 + 90.0, 20.0))
        assert p["dimension"] == 2 and p["y"] == 90.0

    def test_no_nametag(self):
        p = parse_player_entity(entity_blob(1.0, 2.0, 3.0, tree=b"\x00"))
        assert p["name"] is None

    def test_rejects_garbage(self):
        for bad in (b"", net_str("Chicken") + b"\x00" * 40,
                    entity_blob(1, 2, 3)[:30],
                    entity_blob(1, 2, 3, tree=attr(99, "x") + b"\x00"),
                    entity_blob(float("nan"), 2, 3)):
            with pytest.raises(ValueError):
                parse_player_entity(bad)

    def test_read_players(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", w=2, h=2)
        conn = sqlite3.connect(path)
        good = f_bytes(1, b"uid-a") + f_bytes(3, entity_blob(512001.0, 120.0, 511990.0))
        nameless = f_bytes(1, b"uid-b") + f_bytes(3, entity_blob(5.0, 6.0, 7.0, tree=b"\x00"))
        broken = f_bytes(1, b"uid-c") + f_bytes(3, b"\x05junk")
        for uid, data in (("uid-a", good), ("uid-b", nameless), ("uid-c", broken)):
            conn.execute("INSERT INTO playerdata (playeruid, data) VALUES (?, ?)",
                         (uid, data))
        conn.commit()
        conn.close()
        players, unreadable = read_players(path)
        assert unreadable == 1
        assert [(p["uid"], p["name"]) for p in players] == [
            ("uid-b", None), ("uid-a", "Ünïcode_Player")]
        assert (players[1]["x"], players[1]["z"]) == (512001.0, 511990.0)

    def test_read_players_empty(self, tmp_path):
        path = make_world(tmp_path / "w.vcdbs", w=1, h=1)
        assert read_players(path) == ([], 0)
