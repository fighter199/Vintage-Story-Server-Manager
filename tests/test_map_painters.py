"""Tests for ui.map_painters — the bitmap font used in map images."""
import string

from ui.map_painters import FONT, GLYPH_H, GLYPH_W, ImagePainter


class TestFont:
    def test_every_glyph_is_5x7(self):
        for ch, rows in FONT.items():
            assert len(rows) == GLYPH_H, ch
            assert all(len(r) == GLYPH_W and set(r) <= {"#", "."}
                       for r in rows), ch

    def test_covers_printable_ascii_and_map_symbols(self):
        wanted = set(string.ascii_letters + string.digits
                     + string.punctuation + " ") | {"⚑", "·"}
        assert wanted <= set(FONT)

    def test_glyphs_are_distinct(self):
        seen = {}
        for ch, rows in FONT.items():
            key = tuple(rows)
            assert key not in seen or ch == " ", (ch, seen.get(key))
            seen[key] = ch

    def test_text_width(self):
        assert ImagePainter.text_width("abc", scale=2) == 3 * 6 * 2
