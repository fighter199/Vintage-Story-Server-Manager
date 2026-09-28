#!/usr/bin/env python3
"""
tests/ui_smoke.py — Headless UI smoke test (needs Tk and a display).

Not part of run_tests.py: run it under a virtual display, e.g.

    xvfb-run -a python tests/ui_smoke.py

It copies the app to a temporary folder (so no settings or logs land in
the checkout), boots it, and fails if:
  * any exception reaches Tk's error handler,
  * any widget in a sidebar tab is cut off horizontally — past the tab's
    edge, squeezed, or hidden for lack of room — at 100 % or 130 % text,
  * the world map can't open a small synthetic savegame or its land
    claim, or "Keep claims…" doesn't keep just the claim,
  * after a live theme change any colour of the previous theme is left
    anywhere (widgets, text tags, canvas items, button hover colours,
    ttk styles).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import traceback

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
TIMEOUT = 120


def make_app_copy() -> str:
    tmp = tempfile.mkdtemp(prefix="vssm-ui-")
    app_dir = os.path.join(tmp, "app")
    shutil.copytree(ROOT, app_dir, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "logs", "Release", "vserverman_settings.json*"))
    return app_dir


def make_world(app_dir: str, folder: str) -> str:
    # Import the helpers from the copy, so `core` is the copy's too and
    # nothing (settings, logs) is ever written into the checkout.
    sys.path[:0] = [app_dir, os.path.join(app_dir, "tests")]
    sys.modules.setdefault("pytest", type(sys)("pytest"))
    import sqlite3
    from test_world_db import claim_blob, f_bytes, f_varint
    from test_world_db import make_world as build   # synthetic .vcdbs
    os.makedirs(folder, exist_ok=True)
    path = build(os.path.join(folder, "smoke.vcdbs"), x0=16000, z0=16000,
                 w=24, h=24)
    # One land claim over chunks 16004..16005, 16004..16005.
    conn = sqlite3.connect(path)
    conn.execute("UPDATE gamedata SET data = ?", (
        f_varint(1, 1024000) + f_varint(2, 256) + f_varint(3, 1024000)
        + f_bytes(27, claim_blob([(512128, 100, 512128, 512191, 120, 512191)],
                                 description="Smoke base")),))
    conn.commit()
    conn.close()
    return path


_TEXT_CLASSES = ("Label", "Button", "Checkbutton", "Radiobutton",
                 "TLabel", "TButton", "TCheckbutton", "TRadiobutton")


def _describe(w) -> str:
    try:
        text = str(w.cget("text"))[:40]
    except Exception:
        text = ""
    return f"{w.winfo_class()} {text!r} ({w})"


def clipped(tab, tolerance: int = 2) -> list[str]:
    """Widgets in `tab` that are cut off horizontally:
      * sticking out past the tab's right edge,
      * text widgets squeezed narrower than their text needs,
      * packed widgets Tk had to hide because the row ran out of room.
    Vertical overflow is fine — tabs scroll or can be resized."""
    right = tab.winfo_rootx() + tab.winfo_width() + tolerance
    bad = []
    stack = [(c, True) for c in tab.winfo_children()]
    while stack:
        w, parent_mapped = stack.pop()
        try:
            mapped = bool(w.winfo_ismapped())
            if parent_mapped and not mapped and w.winfo_manager() == "pack":
                info = w.pack_info()
                if info.get("side") in ("left", "right"):
                    bad.append(f"{_describe(w)} hidden: no room in its row")
                continue
            if not mapped:
                continue
            stack.extend((c, True) for c in w.winfo_children())
            if w.winfo_class() in ("Frame", "Canvas", "Toplevel", "TFrame"):
                continue                     # containers may scroll
            if w.winfo_rootx() + w.winfo_width() > right:
                bad.append(f"{_describe(w)} ends at "
                           f"{w.winfo_rootx() + w.winfo_width()} > {right}")
            elif (w.winfo_class() in _TEXT_CLASSES
                  and w.winfo_width() + tolerance < w.winfo_reqwidth()):
                bad.append(f"{_describe(w)} squeezed to {w.winfo_width()}px "
                           f"of {w.winfo_reqwidth()}px")
        except Exception:
            continue
    return bad


def leftover_colors(app, old_colors: set) -> list[str]:
    """Places still showing a colour of the previous theme."""
    from ui.widgets import (TermButton, _ITEM_COLOR_OPTIONS,
                            _TAG_COLOR_OPTIONS, _WIDGET_COLOR_OPTIONS)
    tcl = app.tk
    found = []

    def check(where, getter, options):
        for opt in options:
            try:
                value = str(getter("-" + opt)).lower()
            except Exception:
                continue
            if value in old_colors:
                found.append(f"{where} -{opt} is still {value}")

    stack = [str(app)]
    while stack:
        path = stack.pop()
        try:
            stack.extend(str(c) for c in tcl.splitlist(
                tcl.call("winfo", "children", path)))
            wclass = str(tcl.call("winfo", "class", path))
        except Exception:
            continue
        try:
            widget = app.nametowidget(path)
        except KeyError:
            widget = None
        if getattr(widget, "_no_retheme", False):
            continue
        check(path, lambda o, p=path: tcl.call(p, "cget", o), _WIDGET_COLOR_OPTIONS)
        if wclass == "Text":
            for tag in tcl.splitlist(tcl.call(path, "tag", "names")):
                check(f"{path} tag {tag}",
                      lambda o, p=path, t=tag: tcl.call(p, "tag", "cget", t, o),
                      _TAG_COLOR_OPTIONS)
        elif wclass == "Canvas":
            for item in tcl.splitlist(tcl.call(path, "find", "all")):
                check(f"{path} item {item}",
                      lambda o, p=path, i=item: tcl.call(p, "itemcget", i, o),
                      _ITEM_COLOR_OPTIONS)
        if isinstance(widget, TermButton):
            for attr in ("_fg", "_bg", "_bg_hover", "_border"):
                if str(getattr(widget, attr)).lower() in old_colors:
                    found.append(f"{path} {attr} is still {getattr(widget, attr)}")
    from tkinter import ttk
    style = ttk.Style(app)
    for name, opt in (("Term.Vertical.TScrollbar", "background"),
                      ("Term.TCombobox", "foreground"),
                      ("Term.TPanedwindow", "background")):
        if str(style.lookup(name, opt)).lower() in old_colors:
            found.append(f"ttk style {name} {opt} unchanged")
    return found


def main() -> int:
    app_dir = make_app_copy()
    world_dir = make_world(app_dir, os.path.join(os.path.dirname(app_dir), "Saves"))
    os.chdir(app_dir)

    import tkinter as tk
    errors: list[str] = []
    tk.Tk.report_callback_exception = (
        lambda self, *exc: errors.append("".join(traceback.format_exception(*exc))))
    import VSSM
    app = VSSM.ServerManagerApp()
    app.geometry("1280x800+0+0")
    app.world_folder_var.set(os.path.dirname(world_dir))
    failures: list[str] = []
    result = {"code": 1}

    def settle(n=6):
        for _ in range(n):
            app.update()
            time.sleep(0.05)

    def check_tabs(scale: float) -> None:
        app._set_text_scale(scale)
        settle()
        for tab_id in app.notebook.tabs():
            app.notebook.select(tab_id)
            settle(3)
            name = app.notebook.tab(tab_id, "text")
            tab = app.nametowidget(tab_id)
            for problem in clipped(tab):
                failures.append(f"[{scale:.0%}] {name}: {problem}")

    def check_theme_change() -> None:
        from ui.theme import palette
        for preset in ("green", "dark", "amber"):
            before = set(palette().values())
            app.theme_preset_var.set(preset)
            app._on_theme_change()
            settle(2)
            stale = before - set(palette().values())
            for problem in leftover_colors(app, stale)[:10]:
                failures.append(f"[theme {preset}] {problem}")

    def run():
        try:
            check_tabs(1.0)
            check_tabs(1.3)
            app._set_text_scale(1.0)
            tab = app._world_map_tab
            tab.refresh_saves()
            tab.open_map()
            started = time.time()
            while time.time() - started < 60:
                settle(2)
                win = tab._window
                if win is not None and win._base.grid is not None and win._busy is None:
                    break
            win = tab._window
            if win is None or win._base.grid is None:
                failures.append("world map did not load the synthetic savegame")
            elif win._base.index.hits != 24 * 24:
                failures.append(f"world map read {win._base.index.hits} columns, "
                                f"expected {24 * 24}")
            elif [c.label for c in win._claims] != ["Smoke base"]:
                failures.append(f"world map claims: {win._claims}")
            else:
                from tkinter import simpledialog
                simpledialog.askinteger = lambda *a, **k: 0
                win._keep_claims()
                settle(2)
                if win._claims_in_selection() or not win._sel.contains(16000, 16000):
                    failures.append("Keep claims… did not keep just the claim")
            # With the map window open, so its canvas is covered too.
            check_theme_change()
        except Exception:
            failures.append(traceback.format_exc())
        failures.extend(f"Tk callback error:\n{e}" for e in errors)
        for f in failures:
            print("FAIL", f)
        print(f"UI smoke test: {'FAILED' if failures else 'passed'} "
              f"({len(failures)} problem(s))")
        result["code"] = 1 if failures else 0
        app.after(0, app.destroy)

    app.after(3500, run)
    app.after(TIMEOUT * 1000, lambda: (print("FAIL timeout"), os._exit(1)))
    app.mainloop()
    return result["code"]


if __name__ == "__main__":
    sys.exit(main())
