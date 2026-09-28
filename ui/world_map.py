"""
ui/world_map.py — WORLD MAP tab and the map window it opens.

The tab picks a savegame (.vcdbs) from the world folder; the window
renders the generated terrain as shaded relief and lets the user select
areas and delete them so the server regenerates that terrain from the
seed. Viewing only reads the database; deleting requires the server to
be stopped.

Two image layers keep huge worlds responsive: a whole-world overview
(sampled for big savegames, see world_db.load_overview) and a
full-detail tile for whatever is on screen once zoomed in far enough
(world_db.load_detail). Zoom levels are powers of two so both layers
scale with Tk's integer photo zoom/subsample.

All savegame parsing lives in core/world_db.py.
"""
from __future__ import annotations

import math
import os
import shutil
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, simpledialog, ttk

from core.constants import LOG
from core.utils import fmt_size
from core.world_db import (CHUNK_SIZE, MISSING, REGION_CHUNKS, ChunkSelection,
                           WorldDbError, delete_chunk_columns, load_detail,
                           load_overview, read_world_meta, render_ppm,
                           read_players, rewrite_savegame)
from .theme import Theme
from .widgets import TermButton, TermCheckbutton, panel_header


# Zoom = screen pixels per chunk = 2 ** exponent.
_MIN_ZOOM_EXP = -8
_MAX_ZOOM_EXP = 6
# Full-detail tile budget: columns read and pixels rendered per tile.
_DETAIL_MAX_COLUMNS = 30_000
_DETAIL_MAX_PIXELS = 1_500_000


def _savegame_label(path: str) -> str:
    try:
        size = fmt_size(os.path.getsize(path))
    except OSError:
        size = "?"
    return f"{os.path.basename(path)}  ({size})"


# ----------------------------------------------------------------------
# Notebook tab
# ----------------------------------------------------------------------
class WorldMapTab:
    """Savegame picker + launcher for WorldMapWindow."""

    def __init__(self, parent: tk.Frame, app):
        self._app = app
        self._paths: dict[str, str] = {}          # combobox label -> path
        self._save_var = tk.StringVar()
        self._info_var = tk.StringVar(value="")
        self._window: "WorldMapWindow | None" = None
        self._build(parent)
        app.world_folder_var.trace_add("write", lambda *_: self.refresh_saves())
        self.refresh_saves()

    def _build(self, parent):
        app = self._app
        pad = tk.Frame(parent, bg=Theme.BG_PANEL)
        pad.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        panel_header(pad, "World Map & Chunk Pruning", font_spec=app.F_HDR)

        row = tk.Frame(pad, bg=Theme.BG_PANEL)
        row.pack(fill=tk.X, pady=(10, 0))
        tk.Label(row, text="Savegame:", fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
                 font=app.F_NORMAL).pack(side=tk.LEFT)
        self._combo = ttk.Combobox(row, textvariable=self._save_var,
                                   state="readonly", style="Term.TCombobox",
                                   font=app.F_NORMAL)
        self._combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self._combo.bind("<<ComboboxSelected>>", lambda _e: self._update_info())
        TermButton(row, "↻", self.refresh_saves, variant="amber",
                   font_spec=app.F_SMALL, padx=8, pady=3).pack(side=tk.LEFT)
        TermButton(row, "Browse…", self._browse, variant="amber",
                   font_spec=app.F_SMALL, padx=8, pady=3
                   ).pack(side=tk.LEFT, padx=(6, 0))

        info_lbl = tk.Label(pad, textvariable=self._info_var, fg=Theme.MUTED,
                            bg=Theme.BG_PANEL, font=app.F_SMALL, anchor=tk.W,
                            justify=tk.LEFT)
        info_lbl.pack(fill=tk.X, pady=(4, 0))
        info_lbl.bind("<Configure>", lambda e: info_lbl.configure(
            wraplength=max(200, e.width - 8)))

        btn_row = tk.Frame(pad, bg=Theme.BG_PANEL)
        btn_row.pack(fill=tk.X, pady=(10, 0))
        TermButton(btn_row, "🗺 Open Map", self.open_map, variant="start",
                   font_spec=app.F_BTN, padx=12, pady=6).pack(side=tk.LEFT)

        help_text = (
            "The map shows every generated chunk column (32×32 blocks) as "
            "shaded relief. Select areas and delete them: the server "
            "regenerates deleted terrain from the world seed the next time "
            "a player goes there, and anything built there is lost.\n\n"
            "• Viewing only reads the savegame and works while the server "
            "runs. Big worlds open as a sampled overview; zoom in and the "
            "visible area is filled in at full detail.\n"
            "• Deleting needs the server stopped. Pick \"rewrite\" to get a "
            "smaller file (the original is kept as a backup) or delete in "
            "place for small edits.\n"
            "• Mouse: left-drag uses the current tool, right/middle-drag "
            "pans, wheel zooms. Shift+drag deselects.")
        help_lbl = tk.Label(pad, text=help_text, fg=Theme.AMBER_DIM,
                            bg=Theme.BG_PANEL, font=app.F_SMALL,
                            anchor=tk.NW, justify=tk.LEFT)
        help_lbl.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        help_lbl.bind("<Configure>", lambda e: help_lbl.configure(
            wraplength=max(200, e.width - 8)))

    def refresh_saves(self) -> None:
        files = sorted(self._app._savegame_db_files(),
                       key=lambda p: _safe_mtime(p), reverse=True)
        current = self._paths.get(self._save_var.get())
        self._paths = {_savegame_label(p): p for p in files}
        labels = list(self._paths)
        if current and current not in files and os.path.isfile(current):
            # Keep a browsed-to file selected across refreshes.
            labels.insert(0, _savegame_label(current))
            self._paths[labels[0]] = current
        self._combo.configure(values=labels)
        keep = next((lbl for lbl, p in self._paths.items() if p == current), None)
        # Default to the most recently written save — the live one.
        self._save_var.set(keep or (labels[0] if labels else ""))
        self._update_info()

    def _browse(self) -> None:
        path = filedialog.askopenfilename(
            title="Select a savegame",
            initialdir=self._app.world_folder_var.get() or None,
            filetypes=[("Vintage Story savegame", "*.vcdbs"), ("All", "*.*")])
        if not path:
            return
        label = _savegame_label(path)
        self._paths[label] = path
        self._combo.configure(values=[label] + [lbl for lbl in self._combo["values"]
                                                if lbl != label])
        self._save_var.set(label)
        self._update_info()

    def _selected_path(self) -> str:
        return self._paths.get(self._save_var.get(), "")

    def _update_info(self) -> None:
        path = self._selected_path()
        if not path:
            world = self._app.world_folder_var.get().strip()
            self._info_var.set(
                "No .vcdbs savegame found in the world folder."
                if world else "Set the world folder in SETTINGS first.")
            return
        try:
            stamp = datetime.fromtimestamp(os.path.getmtime(path))
            self._info_var.set(f"{path}\nLast written {stamp:%Y-%m-%d %H:%M}")
        except OSError:
            self._info_var.set(path)

    def open_map(self) -> None:
        path = self._selected_path()
        if not path or not os.path.isfile(path):
            self._app._notify("Pick a savegame first.", level="warn")
            return
        win = self._window
        if win is not None and win.alive:
            if os.path.abspath(win.path) == os.path.abspath(path):
                win.deiconify()
                win.lift()
                win.focus_force()
                return
            if not win.close():
                return
        self._window = WorldMapWindow(self._app, path)


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0




class _Layer:
    """One map image: its grid, the full-size photo, and the canvas item
    showing the visible part of it."""

    def __init__(self, window: tk.Toplevel, canvas: tk.Canvas):
        self.grid = None
        self.index = None
        self.photo: tk.PhotoImage | None = None
        self.view = tk.PhotoImage(master=window)
        self.item = canvas.create_image(0, 0, anchor=tk.NW, image=self.view,
                                        state="hidden")


# ----------------------------------------------------------------------
# Map window
# ----------------------------------------------------------------------
class WorldMapWindow(tk.Toplevel):

    def __init__(self, app, path: str):
        super().__init__(app)
        self._app = app
        self.path = path
        self.alive = True
        self.title(f"World Map — {os.path.basename(path)}")
        self.configure(bg=Theme.BG_DARK)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = int(sw * 0.8), int(sh * 0.8)
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")
        self.minsize(640, 420)

        self._meta: dict = {}
        self._loaded_mtime = 0.0

        # View: chunk coordinate at the canvas' top-left corner + zoom.
        self._ox = 0.0
        self._oz = 0.0
        self._zoom_exp = 0

        self._sel = ChunkSelection()
        self._sel_area = 0            # selected chunk cells, existing or not
        self._sel_estimate = 0        # selected existing columns (maybe ≈)
        self._mode = "pan"
        self._drag: dict | None = None
        self._busy: str | None = None           # "load" | "delete" | "render"
        self._cancel = False
        self._stipple_ok = self.tk.call("tk", "windowingsystem") != "aqua"

        # Detail tile loading (runs alongside normal use).
        self._detail_rect: tuple | None = None
        self._detail_gen = 0
        self._detail_thread: threading.Thread | None = None
        self._detail_job = None

        self._grid_var = tk.BooleanVar(value=True)
        self._players_var = tk.BooleanVar(value=True)
        self._players: list[dict] = []
        self._goto_var = tk.StringVar(value="Go to player…")
        self._hover_var = tk.StringVar(value="")
        self._sel_var = tk.StringVar(value="Nothing selected.")
        self._world_var = tk.StringVar(value="Loading…")
        self._note_var = tk.StringVar(value="")

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(50, self._load)

    # ---------------------------------------------------------------- build
    def _build(self):
        app = self._app
        bar = tk.Frame(self, bg=Theme.BG_PANEL)
        bar.pack(fill=tk.X, padx=8, pady=(8, 0))
        small = dict(font_spec=app.F_SMALL, padx=8, pady=4)
        self._mode_buttons = {
            "pan": TermButton(bar, "✋ Pan", lambda: self._set_mode("pan"), **small),
            "select": TermButton(bar, "▭ Select", lambda: self._set_mode("select"), **small),
            "deselect": TermButton(bar, "▭ Deselect", lambda: self._set_mode("deselect"), **small),
        }
        self._sel_buttons = [
            TermButton(bar, "All", self._select_all, **small),
            TermButton(bar, "Invert", self._invert, **small),
            TermButton(bar, "Clear", self._clear_selection, **small),
            TermButton(bar, "Keep centre…", self._keep_centre, **small),
            TermButton(bar, "Keep near players…", self._keep_near_players,
                       **small),
        ]
        view_buttons = [
            TermButton(bar, "−", lambda: self._zoom_step(-1), **small),
            TermButton(bar, "+", lambda: self._zoom_step(1), **small),
            TermButton(bar, "Fit", self._fit, **small),
        ]
        self._reload_btn = TermButton(bar, "↻ Reload", self._load, **small)
        grid_chk = TermCheckbutton(bar, "Grid", self._grid_var,
                                   font_spec=app.F_SMALL, command=self._redraw)
        players_chk = TermCheckbutton(bar, "Players", self._players_var,
                                      font_spec=app.F_SMALL, command=self._redraw)
        self._goto_combo = ttk.Combobox(bar, textvariable=self._goto_var,
                                        state="readonly", width=18,
                                        style="Term.TCombobox", font=app.F_SMALL)
        self._goto_combo.bind("<<ComboboxSelected>>", self._goto_player)
        app._install_wrapping_row(
            bar, [*self._mode_buttons.values(), *self._sel_buttons,
                  *view_buttons, self._reload_btn, grid_chk, players_chk,
                  self._goto_combo],
            spacing=6)
        self._set_mode("pan")

        self._canvas = tk.Canvas(self, bg=Theme.BG_INPUT, highlightthickness=1,
                                 highlightbackground=Theme.BORDER, bd=0,
                                 cursor="fleur")
        self._canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self._base = _Layer(self, self._canvas)
        self._detail = _Layer(self, self._canvas)
        self._msg_item = self._canvas.create_text(
            20, 20, anchor=tk.NW, text="Loading…", fill=Theme.AMBER,
            font=app.F_NORMAL)

        c = self._canvas
        c.bind("<Configure>", lambda _e: self._on_view_changed())
        c.bind("<Motion>", self._on_motion)
        c.bind("<Leave>", lambda _e: self._hover_var.set(""))
        c.bind("<ButtonPress-1>", lambda e: self._on_press(e, self._mode))
        c.bind("<Shift-ButtonPress-1>", lambda e: self._on_press(e, "deselect"))
        for b in ("2", "3"):
            c.bind(f"<ButtonPress-{b}>", lambda e: self._on_press(e, "pan"))
        for b in ("1", "2", "3"):
            c.bind(f"<B{b}-Motion>", self._on_drag)
            c.bind(f"<ButtonRelease-{b}>", self._on_release)
        c.bind("<MouseWheel>", self._on_wheel)                 # Windows / macOS
        c.bind("<Button-4>", lambda e: self._zoom_at(e.x, e.y, 1))   # X11
        c.bind("<Button-5>", lambda e: self._zoom_at(e.x, e.y, -1))
        self.bind("<Escape>", self._cancel_drag)
        self.bind("<plus>", lambda _e: self._zoom_step(1))
        self.bind("<equal>", lambda _e: self._zoom_step(1))
        self.bind("<minus>", lambda _e: self._zoom_step(-1))

        status = tk.Frame(self, bg=Theme.BG_PANEL)
        status.pack(fill=tk.X, padx=8, pady=(0, 8))
        right = tk.Frame(status, bg=Theme.BG_PANEL)
        right.pack(side=tk.RIGHT, padx=(8, 0))
        self._delete_btn = TermButton(right, "🗑 Delete selected…",
                                      self._on_delete_clicked, variant="stop",
                                      font_spec=app.F_BTN, padx=12, pady=6)
        self._delete_btn.pack(side=tk.RIGHT)
        left = tk.Frame(status, bg=Theme.BG_PANEL)
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)
        for var, color in ((self._world_var, Theme.AMBER_DIM),
                           (self._sel_var, Theme.AMBER),
                           (self._note_var, Theme.CYAN),
                           (self._hover_var, Theme.MUTED)):
            tk.Label(left, textvariable=var, fg=color, bg=Theme.BG_PANEL,
                     font=app.F_SMALL, anchor=tk.W).pack(fill=tk.X)
        self._update_controls()

    # --------------------------------------------------------------- close
    def close(self) -> bool:
        """Close the window. Refused (returns False) mid-delete."""
        if self._busy == "delete":
            messagebox.showinfo(
                "World Map", "The savegame is still being modified — the "
                "window can be closed once that finishes.", parent=self)
            return False
        self._cancel = True
        self._detail_gen += 1
        self.alive = False
        try:
            self.destroy()
        except tk.TclError:
            pass
        return True

    def _post(self, fn, *args) -> None:
        """Run fn(*args) on the Tk thread if the window still exists.
        Safe to call from worker threads."""
        def run():
            if self.alive:
                fn(*args)
        try:
            self._app.after(0, run)
        except (RuntimeError, tk.TclError):
            pass

    # ----------------------------------------------------------------- load
    def _load(self) -> None:
        if self._busy:
            return
        self._busy = "load"
        self._cancel = False
        self._drop_detail()
        self._update_controls()
        self._show_message(f"Scanning {os.path.basename(self.path)}…")
        background = Theme.BG_INPUT

        def work():
            started = time.time()
            try:
                mtime = os.path.getmtime(self.path)
                meta = read_world_meta(self.path)
                grid, index, skipped = load_overview(
                    self.path, meta,
                    progress=lambda f: self._post(
                        self._show_message, f"Reading the map… {f:.0%}"),
                    cancel=lambda: self._cancel)
                self._post(self._show_message, "Rendering…")
                ppm = render_ppm(grid, meta["sea_level"], meta["map_size_y"],
                                 background=background,
                                 cancel=lambda: self._cancel)
                try:
                    players, _unreadable = read_players(self.path)
                except (WorldDbError, OSError):
                    players = []
            except InterruptedError:
                return
            except (WorldDbError, OSError, ValueError) as e:
                self._post(self._on_load_failed, str(e))
                return
            except Exception as e:                     # pragma: no cover
                LOG.exception("world map load failed")
                self._post(self._on_load_failed, f"{type(e).__name__}: {e}")
                return
            self._post(self._on_loaded, meta, grid, index, skipped, ppm,
                       mtime, time.time() - started, players)

        threading.Thread(target=work, daemon=True).start()

    def _on_load_failed(self, message: str) -> None:
        self._busy = None
        self._update_controls()
        self._world_var.set("Could not load the savegame.")
        self._show_message(f"⚠ {message}")

    def _on_loaded(self, meta, grid, index, skipped, ppm, mtime, elapsed,
                   players=()):
        self._busy = None
        self._meta = meta
        self._set_players(players)
        first_load = self._base.grid is None
        self._base.grid = grid
        self._base.index = index
        self._base.photo = tk.PhotoImage(master=self, data=ppm, format="PPM")
        self._loaded_mtime = mtime
        self._show_message(None)
        if first_load:
            self._fit()
        else:
            self._on_view_changed()
        self._update_world_info(skipped)
        self._refresh_selection()
        LOG.info("world map: %s from %s in %.1fs (step %d)",
                 f"{index.estimated_total:,} columns", self.path, elapsed,
                 index.step)

    def _update_world_info(self, skipped: int = 0) -> None:
        g, idx = self._base.grid, self._base.index
        blocks_x = (g.max_cx - g.min_cx + 1) * CHUNK_SIZE
        blocks_z = (g.max_cz - g.min_cz + 1) * CHUNK_SIZE
        approx = "≈" if idx.step > 1 else ""
        text = (f"{approx}{idx.estimated_total:,} chunk columns · explored "
                f"extent {blocks_x:,} × {blocks_z:,} blocks · "
                f"{fmt_size(self._meta.get('file_size', 0))} · "
                f"sea level {self._meta['sea_level']}")
        if idx.step > 1:
            text += (f" · overview samples 1 of every {idx.step}×{idx.step} "
                     "chunks — zoom in for full detail")
        if skipped:
            text += f" · {skipped} unreadable rows skipped"
        if self._players:
            n = len(self._players)
            text += f" · {n} player{'s' if n != 1 else ''} (last saved positions)"
        self._world_var.set(text)

    def _show_message(self, text: str | None) -> None:
        c = self._canvas
        if text:
            c.itemconfigure(self._msg_item, text=text, state="normal")
            c.tag_raise(self._msg_item)
        else:
            c.itemconfigure(self._msg_item, state="hidden")

    def _extent(self) -> tuple[int, int, int, int]:
        g = self._base.grid
        return g.min_cx, g.min_cz, g.max_cx, g.max_cz

    # --------------------------------------------------------- detail tile
    def _drop_detail(self) -> None:
        self._detail_gen += 1
        d = self._detail
        d.grid = d.index = d.photo = None
        self._detail_rect = None
        self._canvas.itemconfigure(d.item, state="hidden")

    def _detail_wanted(self) -> bool:
        g = self._base.grid
        return g is not None and self._scale() > g.px_per_chunk

    def _visible_rect(self):
        g = self._base.grid
        c = self._canvas
        x0, z0 = self._chunk_at(0, 0)
        x1, z1 = self._chunk_at(c.winfo_width(), c.winfo_height())
        x0, z0 = max(x0, g.min_cx), max(z0, g.min_cz)
        x1, z1 = min(x1, g.max_cx), min(z1, g.max_cz)
        if x1 < x0 or z1 < z0:
            return None
        return x0, z0, x1, z1

    def _schedule_detail(self) -> None:
        if self._detail_job is not None:
            self.after_cancel(self._detail_job)
        self._detail_job = self.after(300, self._maybe_load_detail)

    def _maybe_load_detail(self) -> None:
        self._detail_job = None
        if self._base.grid is None or self._busy is not None:
            return
        if not self._detail_wanted():
            self._note_var.set("")
            return
        vis = self._visible_rect()
        if vis is None:
            return
        x0, z0, x1, z1 = vis
        cols = (x1 - x0 + 1) * (z1 - z0 + 1)
        if cols > _DETAIL_MAX_COLUMNS:
            if self._base.index.step > 1:
                self._note_var.set("Zoom in further to load this area at "
                                   "full detail.")
            return
        # Pad by a quarter screen each way while the budget allows.
        mx, mz = (x1 - x0 + 1) // 4, (z1 - z0 + 1) // 4
        if (x1 - x0 + 1 + 2 * mx) * (z1 - z0 + 1 + 2 * mz) <= _DETAIL_MAX_COLUMNS:
            g = self._base.grid
            x0, z0 = max(g.min_cx, x0 - mx), max(g.min_cz, z0 - mz)
            x1, z1 = min(g.max_cx, x1 + mx), min(g.max_cz, z1 + mz)
            cols = (x1 - x0 + 1) * (z1 - z0 + 1)
        samples = min(8, max(1, int(self._scale())))
        while samples > 1 and cols * samples * samples > _DETAIL_MAX_PIXELS:
            samples //= 2
        cur = self._detail_rect
        if cur is not None:
            cx0, cz0, cx1, cz1, cs = cur
            if (cx0 <= vis[0] and cz0 <= vis[1] and cx1 >= vis[2]
                    and cz1 >= vis[3] and cs >= samples):
                return                              # already covered
        self._detail_gen += 1
        gen = self._detail_gen
        rect = (x0, z0, x1, z1)
        meta, path = self._meta, self.path
        background = Theme.BG_INPUT
        self._note_var.set("Loading full detail for this area…")

        def work():
            stale = lambda: gen != self._detail_gen or not self.alive  # noqa: E731
            try:
                grid, index, _ = load_detail(path, meta, rect, samples,
                                             cancel=stale)
                ppm = render_ppm(grid, meta["sea_level"], meta["map_size_y"],
                                 background=background, cancel=stale)
            except InterruptedError:
                return
            except Exception as e:
                LOG.warning("world map detail load failed: %s", e)
                self._post(self._on_detail_failed, gen, str(e))
                return
            self._post(self._on_detail_loaded, gen, rect, samples, grid,
                       index, ppm)

        self._detail_thread = threading.Thread(target=work, daemon=True)
        self._detail_thread.start()

    def _on_detail_failed(self, gen: int, message: str) -> None:
        if gen == self._detail_gen:
            self._note_var.set(f"Detail load failed: {message}")

    def _on_detail_loaded(self, gen, rect, samples, grid, index, ppm) -> None:
        if gen != self._detail_gen:
            return
        d = self._detail
        d.grid, d.index = grid, index
        d.photo = tk.PhotoImage(master=self, data=ppm, format="PPM")
        self._detail_rect = (*rect, samples)
        self._note_var.set("")
        self._redraw()
        self._refresh_selection(redraw=False)

    # -------------------------------------------------------------- players
    @staticmethod
    def _player_label(p: dict) -> str:
        return p.get("name") or (p.get("uid") or "?")[:10]

    def _set_players(self, players) -> None:
        self._players = list(players)
        labels = [self._player_label(p) for p in self._players
                  if p.get("dimension", 0) == 0]
        self._goto_combo.configure(values=labels)
        self._goto_combo.configure(state="readonly" if labels else "disabled")
        self._goto_var.set("Go to player…" if labels else "No players saved")

    def _player_chunk(self, p: dict) -> tuple[float, float]:
        return p["x"] / CHUNK_SIZE, p["z"] / CHUNK_SIZE

    def _goto_player(self, _event=None) -> None:
        name = self._goto_var.get()
        for p in self._players:
            if self._player_label(p) == name and p.get("dimension", 0) == 0:
                c = self._canvas
                self._zoom_exp = max(self._zoom_exp, 3)      # 8 px per chunk
                s = self._scale()
                fx, fz = self._player_chunk(p)
                self._ox = fx - c.winfo_width() / (2 * s)
                self._oz = fz - c.winfo_height() / (2 * s)
                self._players_var.set(True)
                self._on_view_changed()
                break
        self.after(10, lambda: self._goto_var.set("Go to player…"))

    def _players_in_selection(self) -> list[dict]:
        return [p for p in self._players if p.get("dimension", 0) == 0
                and self._sel.contains(math.floor(p["x"] / CHUNK_SIZE),
                                       math.floor(p["z"] / CHUNK_SIZE))]

    def _player_near(self, sx: float, sy: float, radius: float = 9.0):
        best, best_d = None, radius
        for p in self._players:
            if p.get("dimension", 0) != 0:
                continue
            px, py = self._to_screen(*self._player_chunk(p))
            d = math.hypot(px - sx, py - sy)
            if d <= best_d:
                best, best_d = p, d
        return best

    def _draw_players(self, cw: int, ch: int) -> None:
        c = self._canvas
        shown = [p for p in self._players if p.get("dimension", 0) == 0]
        # Labels on every marker would bury the map on busy servers.
        labels = len(shown) <= 40 or self._scale() >= 4
        for p in shown:
            x, y = self._to_screen(*self._player_chunk(p))
            if not (-40 <= x <= cw + 40 and -20 <= y <= ch + 20):
                continue
            c.create_oval(x - 5, y - 5, x + 5, y + 5, fill=Theme.CYAN,
                          outline="#000000", width=2, tags="overlay")
            if labels:
                name = self._player_label(p)
                for dx, dy, color in ((1, 1, "#000000"), (0, 0, Theme.CYAN)):
                    c.create_text(x + 9 + dx, y + dy, text=name, anchor=tk.W,
                                  fill=color, font=self._app.F_SMALL,
                                  tags="overlay")

    # ----------------------------------------------------------------- view
    def _scale(self) -> float:
        return 2.0 ** self._zoom_exp

    def _to_screen(self, cx: float, cz: float) -> tuple[float, float]:
        s = self._scale()
        return (cx - self._ox) * s, (cz - self._oz) * s

    def _chunk_at(self, sx: float, sy: float) -> tuple[int, int]:
        s = self._scale()
        return math.floor(self._ox + sx / s), math.floor(self._oz + sy / s)

    def _fit(self) -> None:
        g = self._base.grid
        if g is None:
            return
        c = self._canvas
        cw, ch = c.winfo_width(), c.winfo_height()
        if cw <= 1 or ch <= 1:                  # not laid out yet
            self.after(100, self._fit)
            return
        span_x = g.max_cx - g.min_cx + 1
        span_z = g.max_cz - g.min_cz + 1
        exp = _MAX_ZOOM_EXP
        while exp > _MIN_ZOOM_EXP and (span_x * 2.0 ** exp > cw
                                       or span_z * 2.0 ** exp > ch):
            exp -= 1
        self._zoom_exp = exp
        s = self._scale()
        self._ox = g.min_cx + span_x / 2 - cw / (2 * s)
        self._oz = g.min_cz + span_z / 2 - ch / (2 * s)
        self._on_view_changed()

    def _zoom_step(self, direction: int) -> None:
        c = self._canvas
        self._zoom_at(c.winfo_width() / 2, c.winfo_height() / 2, direction)

    def _zoom_at(self, sx: float, sy: float, direction: int) -> None:
        if self._base.grid is None:
            return
        exp = max(_MIN_ZOOM_EXP, min(_MAX_ZOOM_EXP, self._zoom_exp + direction))
        if exp == self._zoom_exp:
            return
        s = self._scale()
        cx, cz = self._ox + sx / s, self._oz + sy / s
        self._zoom_exp = exp
        s = self._scale()
        self._ox, self._oz = cx - sx / s, cz - sy / s
        self._on_view_changed()

    def _on_wheel(self, event) -> None:
        if event.delta:
            self._zoom_at(event.x, event.y, 1 if event.delta > 0 else -1)

    def _on_view_changed(self) -> None:
        self._redraw()
        self._schedule_detail()

    def _redraw(self) -> None:
        c = self._canvas
        c.delete("overlay")
        if self._base.grid is None:
            return
        cw, ch = max(1, c.winfo_width()), max(1, c.winfo_height())
        self._draw_layer(self._base, cw, ch)
        if self._detail.photo is not None and self._detail_wanted():
            self._draw_layer(self._detail, cw, ch)
        else:
            c.itemconfigure(self._detail.item, state="hidden")
        self._draw_overlays(cw, ch)

    def _draw_layer(self, layer: _Layer, cw: int, ch: int) -> None:
        """Copy the visible part of layer.photo into its view image at the
        current zoom (always a power-of-two ratio → Tk zoom/subsample)."""
        c = self._canvas
        g = layer.grid
        ppc = g.px_per_chunk
        f = self._scale() / ppc
        bx = (self._ox - g.min_cx) * ppc          # image px at canvas (0, 0)
        by = (self._oz - g.min_cz) * ppc
        if f >= 1:
            n = int(round(f))
            x0, y0 = max(0, math.floor(bx)), max(0, math.floor(by))
            x1 = min(g.width, math.ceil(bx + cw / n) + 1)
            y1 = min(g.height, math.ceil(by + ch / n) + 1)
            opts = ("-zoom", n, n)
            out_w, out_h = (x1 - x0) * n, (y1 - y0) * n
            pos = ((x0 - bx) * n, (y0 - by) * n)
        else:
            d = int(round(1 / f))
            # Align to the subsample stride so panning doesn't shimmer.
            x0, y0 = max(0, math.floor(bx / d) * d), max(0, math.floor(by / d) * d)
            x1 = min(g.width, math.ceil(bx + cw * d) + d)
            y1 = min(g.height, math.ceil(by + ch * d) + d)
            opts = ("-subsample", d, d)
            out_w, out_h = -(-(x1 - x0) // d), -(-(y1 - y0) // d)
            pos = ((x0 - bx) / d, (y0 - by) / d)
        if x1 <= x0 or y1 <= y0:
            c.itemconfigure(layer.item, state="hidden")
            return
        layer.view.blank()
        layer.view.configure(width=out_w, height=out_h)
        self.tk.call(layer.view, "copy", layer.photo,
                     "-from", x0, y0, x1, y1, *opts)
        c.coords(layer.item, *pos)
        c.itemconfigure(layer.item, state="normal")
        c.tag_raise(layer.item)

    def _draw_overlays(self, cw: int, ch: int) -> None:
        c = self._canvas
        chunk_px = self._scale()

        def clip_x(v):
            return max(-4.0, min(cw + 4.0, v))

        def clip_y(v):
            return max(-4.0, min(ch + 4.0, v))

        if self._grid_var.get():
            # Chunk lines once chunks are 10 px wide, region lines
            # (every 16 chunks) once regions are 24 px wide.
            for step, min_gap, color, dash in (
                    (1, 10, "#000000", (1, 3)),
                    (REGION_CHUNKS, 24, Theme.AMBER_DIM, (4, 4))):
                if chunk_px * step < min_gap:
                    continue
                cx0, cz0 = self._chunk_at(0, 0)
                cx1, cz1 = self._chunk_at(cw, ch)
                for cx in range(cx0 - cx0 % step, cx1 + 1, step):
                    x, _ = self._to_screen(cx, 0)
                    c.create_line(x, 0, x, ch, fill=color, dash=dash,
                                  tags="overlay")
                for cz in range(cz0 - cz0 % step, cz1 + 1, step):
                    _, y = self._to_screen(0, cz)
                    c.create_line(0, y, cw, y, fill=color, dash=dash,
                                  tags="overlay")

        def rect(x0, z0, x1, z1, **opts):
            xa, ya = self._to_screen(x0, z0)
            xb, yb = self._to_screen(x1 + 1, z1 + 1)
            if xb < 0 or yb < 0 or xa > cw or ya > ch:
                return
            c.create_rectangle(clip_x(xa), clip_y(ya), clip_x(xb), clip_y(yb),
                               tags="overlay", **opts)

        # Selection fill: one rectangle per (row band, x-run). Without
        # stipple support (macOS) the bands are outlined instead.
        if self._stipple_ok:
            band_opts = {"fill": Theme.RED, "stipple": "gray50", "outline": ""}
        else:
            band_opts = {"fill": "", "outline": Theme.RED, "width": 2}
        for z0, z1, runs in self._sel.bands(*self._extent()):
            for a, b in runs:
                rect(a, z0, b, z1, **band_opts)
        if self._stipple_ok:
            # Outline the drawn rectangles: red = selected, amber = kept.
            for add, x0, z0, x1, z1 in self._sel.ops:
                if add:
                    rect(x0, z0, x1, z1, outline=Theme.RED, width=1)
                else:
                    rect(x0, z0, x1, z1, outline=Theme.AMBER_GLOW, width=1,
                         dash=(4, 3))

        if self._detail_rect is not None and self._detail_wanted():
            x0, z0, x1, z1, _s = self._detail_rect
            rect(x0, z0, x1, z1, outline=Theme.CYAN, width=1, dash=(2, 4))

        if self._players_var.get():
            self._draw_players(cw, ch)

        # Map centre — in-game coordinates are shown relative to it.
        mx = self._meta.get("map_size_x", 0) / 2 / CHUNK_SIZE
        mz = self._meta.get("map_size_z", 0) / 2 / CHUNK_SIZE
        x, y = self._to_screen(mx, mz)
        if -20 <= x <= cw + 20 and -20 <= y <= ch + 20:
            c.create_line(x - 9, y, x + 10, y, fill=Theme.AMBER_GLOW, width=2,
                          tags="overlay")
            c.create_line(x, y - 9, x, y + 10, fill=Theme.AMBER_GLOW, width=2,
                          tags="overlay")
            c.create_text(x + 12, y - 12, text="0, 0", anchor=tk.SW,
                          fill=Theme.AMBER_GLOW, font=self._app.F_SMALL,
                          tags="overlay")
        c.tag_raise(self._msg_item)

    # ---------------------------------------------------------------- mouse
    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        for name, btn in self._mode_buttons.items():
            if not hasattr(btn, "_orig_bg"):
                btn._orig_bg = btn._bg
            btn._bg = Theme.BG_SELECT if name == mode else btn._orig_bg
            btn.configure(bg=btn._bg,
                          fg=Theme.AMBER_GLOW if name == mode else btn._fg)
        if hasattr(self, "_canvas"):
            self._canvas.configure(cursor="fleur" if mode == "pan" else "crosshair")

    def _on_press(self, event, kind: str) -> None:
        if self._base.grid is None:
            return
        self._canvas.focus_set()
        if kind != "pan" and self._busy == "delete":
            return
        self._drag = {"kind": kind, "sx": event.x, "sy": event.y,
                      "ox": self._ox, "oz": self._oz,
                      "chunk": self._chunk_at(event.x, event.y)}

    def _on_drag(self, event) -> None:
        d = self._drag
        if d is None:
            return
        if d["kind"] == "pan":
            s = self._scale()
            self._ox = d["ox"] - (event.x - d["sx"]) / s
            self._oz = d["oz"] - (event.y - d["sy"]) / s
            self._on_view_changed()
            return
        cx0, cz0 = d["chunk"]
        cx1, cz1 = self._chunk_at(event.x, event.y)
        xa, ya = self._to_screen(min(cx0, cx1), min(cz0, cz1))
        xb, yb = self._to_screen(max(cx0, cx1) + 1, max(cz0, cz1) + 1)
        c = self._canvas
        c.delete("drag")
        color = Theme.RED if d["kind"] == "select" else Theme.AMBER_GLOW
        c.create_rectangle(xa, ya, xb, yb, outline=color, width=2, dash=(4, 2),
                           tags=("overlay", "drag"))
        self._on_motion(event)

    def _on_release(self, event) -> None:
        d, self._drag = self._drag, None
        if d is None or d["kind"] == "pan":
            return
        cx0, cz0 = d["chunk"]
        cx1, cz1 = self._chunk_at(event.x, event.y)
        if d["kind"] == "select":
            self._sel.add(cx0, cz0, cx1, cz1)
        else:
            self._sel.subtract(cx0, cz0, cx1, cz1)
        self._refresh_selection()

    def _cancel_drag(self, _event=None) -> None:
        self._drag = None
        self._canvas.delete("drag")

    def _on_motion(self, event) -> None:
        g = self._base.grid
        if g is None:
            return
        s = self._scale()
        fx, fz = self._ox + event.x / s, self._oz + event.y / s
        cx, cz = math.floor(fx), math.floor(fz)
        bx, bz = math.floor(fx * CHUNK_SIZE), math.floor(fz * CHUNK_SIZE)
        rel_x = bx - self._meta.get("map_size_x", 0) // 2
        rel_z = bz - self._meta.get("map_size_z", 0) // 2
        d = self._detail
        if d.grid is not None and self._detail_wanted() and \
                d.grid.min_cx <= cx <= d.grid.max_cx and \
                d.grid.min_cz <= cz <= d.grid.max_cz:
            layer, exact = d, True
        else:
            layer, exact = self._base, self._base.index.step == 1
        lg = layer.grid
        v = lg.value_at_px(math.floor((fx - lg.min_cx) * lg.px_per_chunk),
                           math.floor((fz - lg.min_cz) * lg.px_per_chunk))
        if exact and not layer.index.contains(cx, cz):
            what = "not generated"
        elif v == MISSING:
            what = "not generated" if exact else "no sample here"
        elif v < 0:
            what = f"water, {-v} deep"
        else:
            what = f"surface y={v}"
        player = self._player_near(event.x, event.y) \
            if self._players_var.get() else None
        if player is not None:
            what += (f"   ·   player {self._player_label(player)} "
                     f"(y={player['y']:.0f})")
        self._hover_var.set(
            f"X {rel_x:,}  Z {rel_z:,}   ·   chunk {cx}, {cz}   ·   "
            f"region {cx // REGION_CHUNKS}, {cz // REGION_CHUNKS}   ·   {what}")

    # ------------------------------------------------------------ selection
    def _select_all(self) -> None:
        self._sel.clear()
        self._sel.base_all = True
        self._refresh_selection()

    def _invert(self) -> None:
        self._sel.invert()
        self._refresh_selection()

    def _clear_selection(self) -> None:
        self._sel.clear()
        self._refresh_selection()

    def _keep_centre(self) -> None:
        if self._base.grid is None:
            return
        radius = simpledialog.askinteger(
            "Keep centre",
            "Select everything EXCEPT a square around the map centre "
            "(in-game 0, 0).\n\nHow many blocks from the centre should "
            "be kept?",
            parent=self, initialvalue=5000, minvalue=0, maxvalue=10_000_000)
        if radius is None:
            return
        r = -(-radius // CHUNK_SIZE)
        mcx = self._meta["map_size_x"] // 2 // CHUNK_SIZE
        mcz = self._meta["map_size_z"] // 2 // CHUNK_SIZE
        self._sel.clear()
        self._sel.base_all = True
        self._sel.subtract(mcx - r, mcz - r, mcx + r - 1, mcz + r - 1)
        self._refresh_selection()

    def _keep_near_players(self) -> None:
        if self._base.grid is None:
            return
        players = [p for p in self._players if p.get("dimension", 0) == 0]
        if not players:
            messagebox.showinfo("Keep near players",
                                "No player positions are saved in this "
                                "savegame.", parent=self)
            return
        combine = not self._sel.is_empty()
        radius = simpledialog.askinteger(
            "Keep near players",
            f"Keep a square around each of the {len(players)} players' last "
            "saved positions; everything else gets selected"
            + (" (added to what the current selection already keeps)."
               if combine else ".")
            + "\n\nHow many blocks around each player should be kept?",
            parent=self, initialvalue=1000, minvalue=0, maxvalue=1_000_000)
        if radius is None:
            return
        r = -(-radius // CHUNK_SIZE)
        if not combine:
            self._sel.clear()
            self._sel.base_all = True
        for p in players:
            cx = math.floor(p["x"] / CHUNK_SIZE)
            cz = math.floor(p["z"] / CHUNK_SIZE)
            self._sel.subtract(cx - r, cz - r, cx + r, cz + r)
        self._players_var.set(True)
        self._refresh_selection()

    def _count_selection(self) -> tuple[int, int, bool]:
        """(area in chunks, existing columns, exact?) for the selection."""
        ext = self._extent()
        bands = self._sel.bands(*ext)
        area = sum((z1 - z0 + 1) * sum(b - a + 1 for a, b in runs)
                   for z0, z1, runs in bands)
        if not area:
            return 0, 0, True
        d = self._detail
        if d.index is not None and self._detail_rect is not None:
            dx0, dz0, dx1, dz1, _s = self._detail_rect
            zs = [z for z0, z1, _r in bands for z in (z0, z1)]
            xs = [x for _z0, _z1, runs in bands for a, b in runs for x in (a, b)]
            if dx0 <= min(xs) and max(xs) <= dx1 and dz0 <= min(zs) and max(zs) <= dz1:
                return area, d.index.count(self._sel, ext), True
        idx = self._base.index
        return area, idx.count(self._sel, ext) * idx.step * idx.step, idx.step == 1

    def _estimated_bytes(self, columns: int) -> int:
        total = self._base.index.estimated_total if self._base.index else 0
        if not total:
            return 0
        return int(self._meta.get("file_size", 0) * min(1.0, columns / total))

    def _refresh_selection(self, redraw: bool = True) -> None:
        if self._base.grid is None:
            return
        area, columns, exact = self._count_selection()
        self._sel_area = area
        self._sel_estimate = columns
        if not area:
            self._sel_var.set("Nothing selected — use ▭ Select (or Shift+drag "
                              "to deselect).")
        else:
            approx = "" if exact else "≈"
            self._sel_var.set(
                f"Selected: {approx}{columns:,} generated chunk columns in "
                f"{area:,} chunks of map · ≈ "
                f"{fmt_size(self._estimated_bytes(columns))} of the savegame")
        self._update_controls()
        if redraw:
            self._redraw()

    def _update_controls(self) -> None:
        self._delete_btn.set_enabled(self._busy is None and self._sel_area > 0)
        self._reload_btn.set_enabled(self._busy is None)
        for btn in self._sel_buttons:
            btn.set_enabled(self._busy != "delete")

    # --------------------------------------------------------------- delete
    def _on_delete_clicked(self, _waited: int = 0) -> None:
        app = self._app
        if self._busy or not self._sel_area:
            return
        if self._detail_thread is not None and self._detail_thread.is_alive():
            # Let the detail reader close the file first.
            self._detail_gen += 1
            if _waited < 50:
                self.after(100, lambda: self._on_delete_clicked(_waited + 1))
            else:
                self._sel_var.set("Still reading map detail — try again in "
                                  "a moment.")
            return
        if app.is_running or getattr(app, "_shutdown_in_progress", False):
            messagebox.showwarning(
                "Server running",
                "Stop the server before deleting chunks.\n\nThe server keeps "
                "loaded chunks in memory and would write them straight back "
                "(or crash on the missing data).", parent=self)
            return
        if app._backup_manager.in_progress:
            messagebox.showwarning("Backup running",
                                   "Wait for the running backup to finish.",
                                   parent=self)
            return
        if _safe_mtime(self.path) != self._loaded_mtime:
            if messagebox.askyesno(
                    "Savegame changed",
                    "The savegame was modified after the map was loaded. "
                    "Reload the map now? (Your selection is kept.)",
                    parent=self):
                self._load()
            return
        locked, _sidecars = app._savegame_lock_status()
        if any(os.path.abspath(p) == os.path.abspath(self.path) for p in locked):
            messagebox.showerror(
                "Savegame in use",
                "Another process has the savegame open. Make sure no server "
                "is using it.", parent=self)
            return
        opts = self._confirm_delete_dialog()
        if not opts:
            return
        self._busy = "delete"
        self._cancel = False
        self._drop_detail()
        app._world_edit_in_progress = True
        self._update_controls()
        if opts["mode"] == "inplace" and opts["zip_backup"]:
            self._sel_var.set("Backing up the world before deleting…")
            app._start_async_backup(
                silent=False, reason="manual",
                on_done=lambda ok: self._after_backup(ok, opts))
        else:
            self._run_delete(opts)

    def _zip_backup_possible(self) -> tuple[bool, str]:
        app = self._app
        world = app.get_world_folder().strip()
        backup_dir = app.get_backup_dir().strip()
        if not world or not os.path.isdir(world):
            return False, "no world folder configured"
        if not backup_dir:
            return False, "no backup folder configured (SETTINGS)"
        if os.path.dirname(os.path.abspath(self.path)) != os.path.abspath(world):
            return False, "this savegame is outside the world folder"
        need = self._meta.get("file_size", 0)
        free = _free_space(_existing_parent(backup_dir))
        if free < need:
            return False, (f"backup folder has {fmt_size(free)} free, the "
                           f"world needs up to {fmt_size(need)}")
        return True, ""

    def _original_backup_path(self) -> str:
        """Where the rewrite keeps the original: the server's Backups
        folder when it's on the same drive (so the move is instant and
        world-folder zips don't pick it up), else next to the save."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"{os.path.basename(self.path)}.pre-prune-{stamp}"
        save_dir = os.path.dirname(os.path.abspath(self.path))
        backups = self._app.get_server_backups_dir()
        try:
            if backups and os.stat(_existing_parent(backups)).st_dev == \
                    os.stat(save_dir).st_dev:
                os.makedirs(backups, exist_ok=True)
                return os.path.join(backups, name)
        except OSError:
            pass
        return os.path.join(save_dir, name)

    def _confirm_delete_dialog(self) -> dict | None:
        app = self._app
        columns = self._sel_estimate
        area = self._sel_area
        file_size = self._meta.get("file_size", 0)
        est = self._estimated_bytes(columns)
        keep = max(0, file_size - est)
        save_dir = os.path.dirname(os.path.abspath(self.path))
        free = _free_space(save_dir)
        can_rewrite = free > keep * 1.05 + 64 * 1024 * 1024
        can_zip, why_no_zip = self._zip_backup_possible()
        fraction = est / file_size if file_size else 0

        dlg = tk.Toplevel(self)
        dlg.title("Delete chunks")
        dlg.configure(bg=Theme.BG_PANEL)
        dlg.transient(self)
        dlg.resizable(False, False)
        body = tk.Frame(dlg, bg=Theme.BG_PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=14)
        approx = "" if self._base.index.step == 1 else "≈"
        tk.Label(body, text=f"Delete {approx}{columns:,} chunk columns?",
                 fg=Theme.RED, bg=Theme.BG_PANEL, font=app.F_HDR,
                 anchor=tk.W).pack(fill=tk.X)
        _note(body, f"{area:,} chunks of map selected · roughly {fmt_size(est)} "
                    f"of the {fmt_size(file_size)} savegame.", Theme.AMBER, app)
        inside = self._players_in_selection()
        if inside:
            names = ", ".join(self._player_label(p) for p in inside[:8])
            more = f" and {len(inside) - 8} more" if len(inside) > 8 else ""
            _note(body, f"⚠ {len(inside)} player(s) last saved inside the "
                        f"selection: {names}{more}. They'll log back in to "
                        "regenerated terrain, possibly underground.",
                  Theme.RED, app)

        mode_var = tk.StringVar(
            value="rewrite" if can_rewrite and (fraction >= 0.2 or file_size < 2**31)
            else "inplace")
        keep_orig_var = tk.BooleanVar(value=True)
        zip_var = tk.BooleanVar(value=can_zip)
        regions_var = tk.BooleanVar(value=True)

        def radio(text, value, enabled=True):
            rb = tk.Radiobutton(body, text=text, variable=mode_var, value=value,
                                fg=Theme.AMBER_GLOW, bg=Theme.BG_PANEL,
                                activeforeground=Theme.AMBER_GLOW,
                                activebackground=Theme.BG_PANEL,
                                selectcolor=Theme.BG_INPUT, font=app.F_SMALL,
                                disabledforeground=Theme.MUTED,
                                highlightthickness=0, anchor=tk.W, command=sync)
            rb.pack(fill=tk.X, pady=(10, 0))
            if not enabled:
                rb.configure(state=tk.DISABLED)

        def sync():
            rewrite = mode_var.get() == "rewrite"
            keep_chk.configure(state=tk.NORMAL if rewrite else tk.DISABLED)
            zip_chk.configure(state=tk.NORMAL if not rewrite and can_zip
                              else tk.DISABLED)

        radio("Rewrite into a smaller savegame", "rewrite", can_rewrite)
        _note(body, f"   Copies the ≈{fmt_size(keep)} you keep into a new file "
                    "and swaps it in — the file actually shrinks.", Theme.AMBER, app)
        _note(body, f"   Needs ≈{fmt_size(keep)} free on the savegame's drive "
                    f"({fmt_size(free)} free).",
              Theme.MUTED if can_rewrite else Theme.RED, app)
        keep_chk = TermCheckbutton(
            body, "   Keep the original file as a backup (instant — it's "
                  "moved, not copied)", keep_orig_var, font_spec=app.F_SMALL)
        keep_chk.configure(disabledforeground=Theme.MUTED)
        keep_chk.pack(fill=tk.X)

        radio("Delete in place", "inplace")
        _note(body, "   Quicker when removing a small part of a big world. The "
                    "file keeps its size; the server reuses the space for new "
                    "terrain.", Theme.AMBER, app)
        zip_chk = TermCheckbutton(
            body, f"   Zip the world folder into the backup folder first "
                  f"(≈{fmt_size(file_size)} to zip)", zip_var,
            font_spec=app.F_SMALL)
        zip_chk.configure(disabledforeground=Theme.MUTED)
        zip_chk.pack(fill=tk.X)
        if not can_zip:
            _note(body, f"      Unavailable: {why_no_zip}.", Theme.RED, app)

        TermCheckbutton(body, "Also delete map regions left empty (climate/"
                              "ore maps regenerate from the seed)",
                        regions_var, font_spec=app.F_SMALL).pack(fill=tk.X, pady=(10, 0))
        tk.Label(body, justify=tk.LEFT, anchor=tk.W, wraplength=560,
                 fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL, font=app.F_SMALL,
                 text=("• Everything built, stored or tamed in these areas is "
                       "lost; the terrain regenerates from the world seed when "
                       "a player next goes there.\n"
                       "• Players who logged out inside the area may log back "
                       "in underground. Land claims are kept.\n"
                       "• Story locations inside the area may not come back.")
                 ).pack(fill=tk.X, pady=(10, 0))
        sync()

        result: dict = {}

        def confirm():
            rewrite = mode_var.get() == "rewrite"
            has_backup = keep_orig_var.get() if rewrite else zip_var.get() and can_zip
            if not has_backup and not messagebox.askyesno(
                    "No backup", "Delete without a backup? This can't be "
                    "undone.", icon="warning", parent=dlg):
                return
            result.update(mode=mode_var.get(), regions=regions_var.get(),
                          keep_original=rewrite and keep_orig_var.get(),
                          zip_backup=not rewrite and zip_var.get() and can_zip,
                          expected_size=keep)
            dlg.destroy()

        btns = tk.Frame(body, bg=Theme.BG_PANEL)
        btns.pack(fill=tk.X, pady=(14, 0))
        TermButton(btns, "Cancel", dlg.destroy, variant="amber",
                   font_spec=app.F_SMALL, padx=12, pady=5).pack(side=tk.RIGHT)
        TermButton(btns, "🗑 Delete", confirm, variant="stop",
                   font_spec=app.F_SMALL, padx=12, pady=5
                   ).pack(side=tk.RIGHT, padx=(0, 8))
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_reqheight()) // 3
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()
        dlg.focus_set()
        self.wait_window(dlg)
        return result or None

    def _after_backup(self, ok: bool, opts: dict) -> None:
        if not self.alive:
            self._app._world_edit_in_progress = False
            return
        if not ok:
            self._finish_delete()
            self._sel_var.set("Backup failed — nothing was deleted.")
            messagebox.showerror("Backup failed",
                                 "The backup did not complete, so nothing was "
                                 "deleted. See the console for details.",
                                 parent=self)
            return
        self._run_delete(opts)

    def _run_delete(self, opts: dict) -> None:
        extent = self._extent()
        sel = ChunkSelection()
        sel.base_all, sel.ops = self._sel.base_all, list(self._sel.ops)
        path = self.path
        map_size_y = self._meta["map_size_y"]
        rewrite = opts["mode"] == "rewrite"
        backup_path = self._original_backup_path() if opts["keep_original"] else None
        self._delete_btn.configure(text="■ CANCEL")
        self._delete_btn.set_enabled(True)
        self._delete_btn._command = self._request_cancel
        verb = "rewriting" if rewrite else "deleting from"
        self._app.append_console(
            f"World map: {verb} {os.path.basename(path)} "
            f"({self._sel_estimate:,} chunk columns selected)…", "system")
        label = "Writing the new savegame" if rewrite else "Deleting"

        def on_progress(f):
            self._post(self._sel_var.set, f"{label}… {f:.0%}")

        def work():
            try:
                if rewrite:
                    stats = rewrite_savegame(
                        path, sel, extent, map_size_y=map_size_y,
                        backup_path=backup_path,
                        delete_empty_regions=opts["regions"],
                        expected_size=opts["expected_size"],
                        progress=on_progress, cancel=lambda: self._cancel)
                else:
                    stats = delete_chunk_columns(
                        path, sel, extent, map_size_y=map_size_y,
                        delete_empty_regions=opts["regions"],
                        progress=on_progress, cancel=lambda: self._cancel)
            except InterruptedError:
                self._post(self._on_delete_done, None, None, sel, rewrite)
                return
            except Exception as e:
                LOG.exception("chunk deletion failed")
                self._post(self._on_delete_done, None, e, sel, rewrite)
                return
            self._post(self._on_delete_done, stats, None, sel, rewrite)

        threading.Thread(target=work, daemon=True).start()

    def _request_cancel(self) -> None:
        self._cancel = True
        self._sel_var.set("Cancelling…")

    def _finish_delete(self) -> None:
        self._busy = None
        self._app._world_edit_in_progress = False
        self._delete_btn.configure(text="🗑 DELETE SELECTED…")
        self._delete_btn._command = self._on_delete_clicked
        self._update_controls()

    def _on_delete_done(self, stats, error, sel, rewrite) -> None:
        app = self._app
        self._finish_delete()
        if error is not None:
            msg = str(error)
            app.append_console(f"World map: failed — {msg}", "error")
            self._sel_var.set(f"Failed: {msg}")
            detail = ("The original savegame was not changed." if rewrite else
                      "The unfinished batch was rolled back; no chunk column "
                      "is left half-deleted.")
            messagebox.showerror("Delete failed", f"{msg}\n\n{detail}", parent=self)
            return
        if stats is None:                      # rewrite cancelled
            app.append_console("World map: cancelled — the savegame was not "
                               "changed.", "system")
            self._refresh_selection()
            return

        # Mirror the deletion in the loaded map.
        ext = self._extent()
        for cx, cz in self._base.index.remove(sel, ext):
            self._base.grid.clear_chunk(cx, cz)
        self._sel.clear()
        self._meta["file_size"] = _safe_size(self.path)
        self._loaded_mtime = _safe_mtime(self.path)
        if rewrite:
            summary = (f"Savegame rewritten: {fmt_size(stats['size_before'])} → "
                       f"{fmt_size(stats['size_after'])}, "
                       f"{stats['mapregions']:,} empty map regions removed")
            if stats.get("backup_path"):
                summary += f". Original kept at {stats['backup_path']}"
        else:
            summary = (f"Deleted {stats['mapchunks']:,} chunk columns "
                       f"({stats['chunks']:,} chunks, {stats['mapregions']:,} "
                       "map regions)")
            if stats["cancelled"]:
                summary += " before you cancelled — the rest was left as is"
        app.append_console(f"World map: {summary}.", "success")
        app._notify(summary.split(". ")[0], level="success", duration_ms=6000)
        self._refresh_selection()
        self._sel_var.set(summary + ".")
        self._update_world_info()
        self._rerender()

    def _rerender(self) -> None:
        """Re-render the overview after the grid changed in place."""
        grid, meta = self._base.grid, self._meta
        background = Theme.BG_INPUT
        self._busy = "render"
        self._update_controls()

        def work():
            try:
                ppm = render_ppm(grid, meta["sea_level"], meta["map_size_y"],
                                 background=background)
            except Exception:
                LOG.exception("world map re-render failed")
                self._post(self._rerender_done, None)
                return
            self._post(self._rerender_done, ppm)

        threading.Thread(target=work, daemon=True).start()

    def _rerender_done(self, ppm) -> None:
        self._busy = None
        if ppm is not None:
            self._base.photo = tk.PhotoImage(master=self, data=ppm, format="PPM")
        self._update_controls()
        self._on_view_changed()


def _note(parent, text: str, color: str, app) -> None:
    tk.Label(parent, text=text, fg=color, bg=Theme.BG_PANEL, font=app.F_SMALL,
             anchor=tk.W, justify=tk.LEFT, wraplength=560).pack(fill=tk.X)


def _existing_parent(path: str) -> str:
    path = os.path.abspath(path)
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def _free_space(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
