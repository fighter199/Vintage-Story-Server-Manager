"""
ui/map_painters.py — Where the world map's overlays get drawn.

The map window draws its overlays (grid, selection, claims, players, the
centre mark) through a painter, so the same code can draw on the canvas
or into a Tk PhotoImage that is then saved as a PNG ("📷 Save image…").

Only what the overlays need is supported: horizontal/vertical lines,
rectangles (solid, outlined, dashed or half-filled), filled circles and
single-line text. Tk can't draw text into an image, so ImagePainter
carries a small 5×7 bitmap font of its own.
"""
from __future__ import annotations

import math
import tkinter as tk


class CanvasPainter:
    """Draws onto the map canvas as tagged items ("overlay")."""

    def __init__(self, canvas: tk.Canvas, font, stipple_ok: bool = True):
        self._c = canvas
        self._font = font
        self.stipple_ok = stipple_ok

    def line(self, x0, y0, x1, y1, color, width=1, dash=None):
        opts = {"dash": dash} if dash else {}
        self._c.create_line(x0, y0, x1, y1, fill=color, width=width,
                            tags="overlay", **opts)

    def rect(self, x0, y0, x1, y1, outline="", fill="", width=1, dash=None,
             half=False):
        opts = {"dash": dash} if dash else {}
        if half and fill:
            opts["stipple"] = "gray50"
        self._c.create_rectangle(x0, y0, x1, y1, outline=outline, fill=fill,
                                 width=width, tags="overlay", **opts)

    def dot(self, x, y, radius, fill, outline, width):
        self._c.create_oval(x - radius, y - radius, x + radius, y + radius,
                            fill=fill, outline=outline, width=width,
                            tags="overlay")

    def text(self, x, y, text, color, anchor):
        self._c.create_text(x, y, text=text, anchor=anchor, fill=color,
                            font=self._font, tags="overlay")


class ImagePainter:
    """Draws into a PhotoImage (pixels outside it are clipped). Dashes
    and half fills are tiled pattern images, so even a big export takes
    a few thousand Tk calls rather than one per pixel."""

    stipple_ok = True

    def __init__(self, image: tk.PhotoImage, master, text_scale: int = 2):
        self.img = image
        self._master = master
        self.w, self.h = int(image.cget("width")), int(image.cget("height"))
        self.s = max(1, int(text_scale))
        self._patterns: dict = {}

    # --------------------------------------------------------- primitives
    def _clip(self, x0, y0, x1, y1):
        x0, y0 = max(0, int(round(x0))), max(0, int(round(y0)))
        x1, y1 = min(self.w, int(round(x1))), min(self.h, int(round(y1)))
        return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None

    def _fill(self, x0, y0, x1, y1, color):
        box = self._clip(x0, y0, x1, y1)
        if box:
            self.img.put(color, to=box)

    def _pattern(self, key, width, height, lit):
        """A cached image with `lit` pixels in the colour and the rest
        transparent, for tiling over an area."""
        pat = self._patterns.get(key)
        if pat is None:
            color = key[0]
            pat = tk.PhotoImage(master=self._master, width=width,
                                height=height)
            for y in range(height):
                for x in range(width):
                    if lit(x, y):
                        pat.put(color, to=(x, y, x + 1, y + 1))
                    else:
                        pat.transparency_set(x, y, True)
            self._patterns[key] = pat
        return pat

    def _tile(self, pattern, x0, y0, x1, y1):
        box = self._clip(x0, y0, x1, y1)
        if box:
            self.img.tk.call(self.img, "copy", pattern, "-to", *box)

    # ---------------------------------------------------------- interface
    def line(self, x0, y0, x1, y1, color, width=1, dash=None):
        """Horizontal or vertical lines only (all the map draws)."""
        half = width / 2
        if abs(y1 - y0) < abs(x1 - x0):                 # horizontal
            box = (min(x0, x1), y0 - half, max(x0, x1), y0 + half)
            vertical = False
        else:
            box = (x0 - half, min(y0, y1), x0 + half, max(y0, y1))
            vertical = True
        if width < 1:
            return
        if not dash:
            self._fill(*box, color)
            return
        on, off = dash[0], dash[1] if len(dash) > 1 else dash[0]
        period = on + off
        if vertical:
            pat = self._pattern((color, "v", on, off), 1, period,
                                lambda _x, y: y < on)
        else:
            pat = self._pattern((color, "h", on, off), period, 1,
                                lambda x, _y: x < on)
        self._tile(pat, *box)

    def rect(self, x0, y0, x1, y1, outline="", fill="", width=1, dash=None,
             half=False):
        if fill:
            if half:
                pat = self._pattern((fill, "half"), 2, 2,
                                    lambda x, y: (x + y) % 2 == 0)
                self._tile(pat, x0, y0, x1, y1)
            else:
                self._fill(x0, y0, x1, y1, fill)
        if outline and width >= 1:
            for ax, ay, bx, by in ((x0, y0, x1, y0), (x0, y1, x1, y1),
                                   (x0, y0, x0, y1), (x1, y0, x1, y1)):
                self.line(ax, ay, bx, by, outline, width, dash)

    def dot(self, x, y, radius, fill, outline, width):
        r_out = radius + width / 2
        r_in = radius - width / 2
        for dy in range(-math.ceil(r_out), math.ceil(r_out) + 1):
            yy = dy + 0.5
            if abs(yy) > r_out:
                continue
            outer = math.sqrt(max(0.0, r_out * r_out - yy * yy))
            self._fill(x - outer, y + dy, x + outer, y + dy + 1, outline)
            if abs(yy) < r_in:
                inner = math.sqrt(r_in * r_in - yy * yy)
                self._fill(x - inner, y + dy, x + inner, y + dy + 1, fill)

    def text(self, x, y, text, color, anchor):
        s = self.s
        height = GLYPH_H * s
        top = y - height / 2 if anchor == tk.W else y - height   # W or SW
        top = int(round(top))
        cx = int(round(x))
        for ch in text:
            rows = FONT.get(ch) or FONT["?"]
            for r, row in enumerate(rows):
                c = 0
                while c < GLYPH_W:
                    if row[c] != "#":
                        c += 1
                        continue
                    start = c
                    while c < GLYPH_W and row[c] == "#":
                        c += 1
                    self._fill(cx + start * s, top + r * s,
                               cx + c * s, top + (r + 1) * s, color)
            cx += (GLYPH_W + 1) * s

    @staticmethod
    def text_width(text: str, scale: int = 2) -> int:
        return len(text) * (GLYPH_W + 1) * scale


# ----------------------------------------------------------------------
# 5×7 bitmap font (drawn for this project). Rows top to bottom.
# ----------------------------------------------------------------------
GLYPH_W, GLYPH_H = 5, 7

_GLYPHS = {
    " ": "..... ..... ..... ..... ..... ..... .....",
    "!": "..#.. ..#.. ..#.. ..#.. ..#.. ..... ..#..",
    '"': ".#.#. .#.#. .#.#. ..... ..... ..... .....",
    "#": ".#.#. .#.#. ##### .#.#. ##### .#.#. .#.#.",
    "$": "..#.. .#### #.#.. .###. ..#.# ####. ..#..",
    "%": "##... ##..# ...#. ..#.. .#... #..## ...##",
    "&": ".##.. #..#. #.#.. .#... #.#.# #..#. .##.#",
    "'": "..#.. ..#.. .#... ..... ..... ..... .....",
    "(": "...#. ..#.. .#... .#... .#... ..#.. ...#.",
    ")": ".#... ..#.. ...#. ...#. ...#. ..#.. .#...",
    "*": "..... ..#.. #.#.# .###. #.#.# ..#.. .....",
    "+": "..... ..#.. ..#.. ##### ..#.. ..#.. .....",
    ",": "..... ..... ..... ..... .##.. ..#.. .#...",
    "-": "..... ..... ..... ##### ..... ..... .....",
    ".": "..... ..... ..... ..... ..... .##.. .##..",
    "/": "..... ....# ...#. ..#.. .#... #.... .....",
    "0": ".###. #...# #..## #.#.# ##..# #...# .###.",
    "1": "..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###.",
    "2": ".###. #...# ....# ...#. ..#.. .#... #####",
    "3": "##### ...#. ..#.. ...#. ....# #...# .###.",
    "4": "...#. ..##. .#.#. #..#. ##### ...#. ...#.",
    "5": "##### #.... ####. ....# ....# #...# .###.",
    "6": "..##. .#... #.... ####. #...# #...# .###.",
    "7": "##### ....# ...#. ..#.. .#... .#... .#...",
    "8": ".###. #...# #...# .###. #...# #...# .###.",
    "9": ".###. #...# #...# .#### ....# ...#. .##..",
    ":": "..... .##.. .##.. ..... .##.. .##.. .....",
    ";": "..... .##.. .##.. ..... .##.. ..#.. .#...",
    "<": "...#. ..#.. .#... #.... .#... ..#.. ...#.",
    "=": "..... ..... ##### ..... ##### ..... .....",
    ">": ".#... ..#.. ...#. ....# ...#. ..#.. .#...",
    "?": ".###. #...# ....# ...#. ..#.. ..... ..#..",
    "@": ".###. #...# ....# .##.# #.#.# #.#.# .###.",
    "A": ".###. #...# #...# #...# ##### #...# #...#",
    "B": "####. #...# #...# ####. #...# #...# ####.",
    "C": ".###. #...# #.... #.... #.... #...# .###.",
    "D": "###.. #..#. #...# #...# #...# #..#. ###..",
    "E": "##### #.... #.... ####. #.... #.... #####",
    "F": "##### #.... #.... ####. #.... #.... #....",
    "G": ".###. #...# #.... #.### #...# #...# .####",
    "H": "#...# #...# #...# ##### #...# #...# #...#",
    "I": ".###. ..#.. ..#.. ..#.. ..#.. ..#.. .###.",
    "J": "..### ...#. ...#. ...#. ...#. #..#. .##..",
    "K": "#...# #..#. #.#.. ##... #.#.. #..#. #...#",
    "L": "#.... #.... #.... #.... #.... #.... #####",
    "M": "#...# ##.## #.#.# #.#.# #...# #...# #...#",
    "N": "#...# #...# ##..# #.#.# #..## #...# #...#",
    "O": ".###. #...# #...# #...# #...# #...# .###.",
    "P": "####. #...# #...# ####. #.... #.... #....",
    "Q": ".###. #...# #...# #...# #.#.# #..#. .##.#",
    "R": "####. #...# #...# ####. #.#.. #..#. #...#",
    "S": ".#### #.... #.... .###. ....# ....# ####.",
    "T": "##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#..",
    "U": "#...# #...# #...# #...# #...# #...# .###.",
    "V": "#...# #...# #...# #...# #...# .#.#. ..#..",
    "W": "#...# #...# #...# #.#.# #.#.# #.#.# .#.#.",
    "X": "#...# #...# .#.#. ..#.. .#.#. #...# #...#",
    "Y": "#...# #...# .#.#. ..#.. ..#.. ..#.. ..#..",
    "Z": "##### ....# ...#. ..#.. .#... #.... #####",
    "[": ".###. .#... .#... .#... .#... .#... .###.",
    "\\": "..... #.... .#... ..#.. ...#. ....# .....",
    "]": ".###. ...#. ...#. ...#. ...#. ...#. .###.",
    "^": "..#.. .#.#. #...# ..... ..... ..... .....",
    "_": "..... ..... ..... ..... ..... ..... #####",
    "`": ".#... ..#.. ...#. ..... ..... ..... .....",
    "a": "..... ..... .###. ....# .#### #...# .####",
    "b": "#.... #.... #.##. ##..# #...# #...# ####.",
    "c": "..... ..... .###. #.... #.... #...# .###.",
    "d": "....# ....# .##.# #..## #...# #...# .####",
    "e": "..... ..... .###. #...# ##### #.... .###.",
    "f": "..##. .#..# .#... ###.. .#... .#... .#...",
    "g": "..... .#### #...# #...# .#### ....# .###.",
    "h": "#.... #.... #.##. ##..# #...# #...# #...#",
    "i": "..#.. ..... .##.. ..#.. ..#.. ..#.. .###.",
    "j": "...#. ..... ..##. ...#. ...#. #..#. .##..",
    "k": "#.... #.... #..#. #.#.. ##... #.#.. #..#.",
    "l": ".##.. ..#.. ..#.. ..#.. ..#.. ..#.. .###.",
    "m": "..... ..... ##.#. #.#.# #.#.# #...# #...#",
    "n": "..... ..... #.##. ##..# #...# #...# #...#",
    "o": "..... ..... .###. #...# #...# #...# .###.",
    "p": "..... ..... ####. #...# ####. #.... #....",
    "q": "..... ..... .##.# #..## .#### ....# ....#",
    "r": "..... ..... #.##. ##..# #.... #.... #....",
    "s": "..... ..... .###. #.... .###. ....# ####.",
    "t": ".#... .#... ###.. .#... .#... .#..# ..##.",
    "u": "..... ..... #...# #...# #...# #..## .##.#",
    "v": "..... ..... #...# #...# #...# .#.#. ..#..",
    "w": "..... ..... #...# #...# #.#.# #.#.# .#.#.",
    "x": "..... ..... #...# .#.#. ..#.. .#.#. #...#",
    "y": "..... ..... #...# #...# .#### ....# .###.",
    "z": "..... ..... ##### ...#. ..#.. .#... #####",
    "{": "...#. ..#.. ..#.. .#... ..#.. ..#.. ...#.",
    "|": "..#.. ..#.. ..#.. ..#.. ..#.. ..#.. ..#..",
    "}": ".#... ..#.. ..#.. ...#. ..#.. ..#.. .#...",
    "~": "..... ..... .#... #.#.# ...#. ..... .....",
    "⚑": "#.... ####. ##### ####. #.... #.... #....",
    "·": "..... ..... ..... ..#.. ..... ..... .....",
    "×": "..... #...# .#.#. ..#.. .#.#. #...# .....",
    "…": "..... ..... ..... ..... ..... ..... #.#.#",
}
FONT = {ch: glyph.split() for ch, glyph in _GLYPHS.items()}
