"""
ui/widgets.py — Reusable themed widget toolkit.

All widgets adapt to the current Theme class values so a preset change
(even mid-session via apply_preset) is reflected on new widget creation.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from collections import deque

from .theme import Theme


# -----------------------------------------------------------------------
# TermButton — flat label-style button
# -----------------------------------------------------------------------
class TermButton(tk.Label):
    """Flat terminal-style button using tk.Label for reliable colors."""

    _VARIANTS = {
        "start":  lambda: (Theme.GREEN,  Theme.BG_BTN_START,  Theme.BG_BTN_START_HOVER,  Theme.GREEN_DIM),
        "green":  lambda: (Theme.GREEN,  Theme.BG_BTN_START,  Theme.BG_BTN_START_HOVER,  Theme.GREEN_DIM),
        "stop":   lambda: (Theme.RED,    Theme.BG_BTN_STOP,   Theme.BG_BTN_STOP_HOVER,   Theme.RED_DIM),
        "red":    lambda: (Theme.RED,    Theme.BG_BTN_STOP,   Theme.BG_BTN_STOP_HOVER,   Theme.RED_DIM),
        "clear":  lambda: ("#6688aa",    "#0a0a1a",           "#0d0d2a",                  "#223344"),
        "blue":   lambda: ("#6688aa",    "#0a0a1a",           "#0d0d2a",                  "#223344"),
        "amber":  lambda: (Theme.AMBER,  Theme.BG_BTN_AMBER,  Theme.BG_BTN_AMBER_HOVER,  Theme.AMBER_DIM),
    }

    def __init__(self, parent, text, command, variant="amber",
                 font_spec=None, width=None, pady=8, padx=20):
        resolver = self._VARIANTS.get(variant, self._VARIANTS["amber"])
        fg, bg, bg_hover, border = resolver()
        super().__init__(parent,
                         text=text.upper(),
                         fg=fg, bg=bg,
                         font=font_spec,
                         padx=padx, pady=pady,
                         bd=0, highlightthickness=1,
                         highlightbackground=border,
                         highlightcolor=border,
                         cursor="hand2")
        if width is not None:
            self.configure(width=width)
        self._fg = fg
        self._bg = bg
        self._bg_hover = bg_hover
        self._border = border
        self._command = command
        self._enabled = True
        self.bind("<Enter>",    self._on_enter)
        self.bind("<Leave>",    self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def _on_enter(self, _e):
        if self._enabled:
            self.configure(bg=self._bg_hover)

    def _on_leave(self, _e):
        if self._enabled:
            self.configure(bg=self._bg)

    def _on_click(self, _e):
        if self._enabled and self._command:
            self._command()

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        if self._enabled:
            self.configure(fg=self._fg, bg=self._bg, cursor="hand2")
        else:
            self.configure(fg=Theme.MUTED, bg=self._bg, cursor="arrow")


# -----------------------------------------------------------------------
# TermEntry
# -----------------------------------------------------------------------
class TermEntry(tk.Entry):
    def __init__(self, parent, textvariable=None, font_spec=None,
                 width=None, show=None):
        kwargs = dict(
            bg=Theme.BG_INPUT,
            fg=Theme.AMBER,
            insertbackground=Theme.AMBER_GLOW,
            selectbackground=Theme.BG_SELECT,
            selectforeground=Theme.AMBER_GLOW,
            bd=0,
            highlightthickness=1,
            highlightbackground=Theme.BORDER,
            highlightcolor=Theme.AMBER_DIM,
            font=font_spec,
        )
        if textvariable is not None:
            kwargs["textvariable"] = textvariable
        if width is not None:
            kwargs["width"] = width
        if show is not None:
            kwargs["show"] = show
        super().__init__(parent, **kwargs)


# -----------------------------------------------------------------------
# TermText — styled tk.Text
# -----------------------------------------------------------------------
class TermText(tk.Text):
    def __init__(self, parent, **kwargs):
        defaults = dict(
            bg=Theme.BG_INPUT,
            fg=Theme.AMBER,
            insertbackground=Theme.AMBER_GLOW,
            selectbackground=Theme.BG_SELECT,
            selectforeground=Theme.AMBER_GLOW,
            bd=0,
            highlightthickness=0,
        )
        defaults.update(kwargs)
        super().__init__(parent, **defaults)


# -----------------------------------------------------------------------
# TermCheckbutton
# -----------------------------------------------------------------------
class TermCheckbutton(tk.Checkbutton):
    def __init__(self, parent, text, variable, font_spec=None, command=None):
        super().__init__(parent,
                         text=text,
                         variable=variable,
                         font=font_spec,
                         fg=Theme.AMBER,
                         bg=Theme.BG_PANEL,
                         activeforeground=Theme.AMBER_GLOW,
                         activebackground=Theme.BG_PANEL,
                         selectcolor=Theme.BG_INPUT,
                         bd=0, highlightthickness=0,
                         anchor=tk.W,
                         command=command)


# -----------------------------------------------------------------------
# Sparkline — rolling line chart
# -----------------------------------------------------------------------
class Sparkline(tk.Canvas):
    def __init__(self, parent, width=120, height=22, capacity=60,
                 color=None, baseline_color=None, bg=None):
        bg = bg if bg is not None else Theme.BG_INPUT
        super().__init__(parent, width=width, height=height, bg=bg,
                         bd=0, highlightthickness=1,
                         highlightbackground=Theme.BORDER)
        self._default_w = width
        self._default_h = height
        self._cap = capacity
        self._data: deque = deque(maxlen=capacity)
        self._color = color or Theme.AMBER
        self._baseline = baseline_color or Theme.AMBER_FAINT
        self.bind("<Configure>", lambda _e: self._redraw())

    def push(self, value):
        try:
            v = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return
        self._data.append(v)
        self._redraw()

    def clear(self):
        self._data.clear()
        self.delete("all")

    def _redraw(self):
        try:
            self.delete("all")
        except tk.TclError:
            return
        if not self._data:
            return
        try:
            w = max(self._default_w, int(self.winfo_width()))
            h = max(self._default_h, int(self.winfo_height()))
        except tk.TclError:
            return
        if w <= 2:
            w = self._default_w
        if h <= 2:
            h = self._default_h
        try:
            self.create_line(0, h // 2, w, h // 2,
                             fill=self._baseline, dash=(1, 3))
        except tk.TclError:
            return
        n = len(self._data)
        if n < 2:
            v = self._data[0]
            x = w - 2
            y = max(1, h - 2 - int(v * (h - 4)))
            try:
                self.create_oval(x - 2, y - 2, x + 2, y + 2,
                                 outline=self._color, fill=self._color)
            except tk.TclError:
                pass
            return
        step = w / max(1, (n - 1))
        pts = []
        for i, v in enumerate(self._data):
            x = int(i * step)
            y = max(1, h - 2 - int(v * (h - 4)))
            pts.extend((x, y))
        try:
            self.create_line(*pts, fill=self._color, width=2, smooth=False)
        except tk.TclError:
            pass


# -----------------------------------------------------------------------
# ScrollableFrame
# -----------------------------------------------------------------------
class ScrollableFrame(tk.Frame):
    """Vertically scrollable frame; scrollbar appears only when needed."""

    def __init__(self, parent, bg=Theme.BG_PANEL,
                 scrollbar_style="Term.Vertical.TScrollbar",
                 max_height=None):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0)
        self._canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self._sb = ttk.Scrollbar(self, orient=tk.VERTICAL,
                                 style=scrollbar_style,
                                 command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._on_scroll_set)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.body = tk.Frame(self._canvas, bg=bg, highlightthickness=0, bd=0)
        self._body_id = self._canvas.create_window(
            (0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self.bind("<Enter>", self._bind_wheel)
        self.bind("<Leave>", self._unbind_wheel)
        self._sb_packed = False
        self._max_height = max_height

    def _on_body_configure(self, _e):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        if self._max_height is not None:
            try:
                needed = self.body.winfo_reqheight()
            except tk.TclError:
                return
            h = max(1, min(needed, self._max_height))
            try:
                self._canvas.configure(height=h)
            except tk.TclError:
                pass

    def _on_canvas_configure(self, event):
        self._canvas.itemconfigure(self._body_id, width=event.width)

    def _on_scroll_set(self, first, last):
        try:
            f, l = float(first), float(last)
        except ValueError:
            return
        needs_bar = (f > 0.0) or (l < 1.0)
        if needs_bar and not self._sb_packed:
            self._sb.pack(side=tk.RIGHT, fill=tk.Y)
            self._sb_packed = True
        elif not needs_bar and self._sb_packed:
            self._sb.pack_forget()
            self._sb_packed = False
        self._sb.set(first, last)

    def _bind_wheel(self, _e):
        self._canvas.bind_all("<MouseWheel>",  self._on_mousewheel)
        self._canvas.bind_all("<Button-4>",    self._on_wheel_up)
        self._canvas.bind_all("<Button-5>",    self._on_wheel_down)

    def _unbind_wheel(self, _e):
        self._canvas.unbind_all("<MouseWheel>")
        self._canvas.unbind_all("<Button-4>")
        self._canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event):
        if event.delta == 0:
            return
        step = -1 if event.delta > 0 else 1
        self._canvas.yview_scroll(step * 3, "units")

    def _on_wheel_up(self,   _e): self._canvas.yview_scroll(-3, "units")
    def _on_wheel_down(self, _e): self._canvas.yview_scroll( 3, "units")


# -----------------------------------------------------------------------
# TabStrip — wrapping tab row for a ttk.Notebook
# -----------------------------------------------------------------------
class TabStrip(tk.Frame):
    """A row of tab labels that wraps onto more lines, driving a
    ttk.Notebook whose own tab row is hidden (style "Strip.TNotebook").

    ttk.Notebook squeezes tab labels to fit its width, so ten tabs in a
    narrow sidebar were cut down to "CO", "SE", "BA". This strip keeps
    every label whole at any width or text size."""

    def __init__(self, parent, notebook, font_spec, spacing=2):
        super().__init__(parent, bg=Theme.BG_PANEL, height=1,
                         highlightthickness=0, bd=0)
        self._nb = notebook
        self._font = font_spec
        self._spacing = spacing
        self._tabs: list = []            # (frame, label)
        self._job = None
        self.bind("<Configure>", lambda _e: self.relayout())
        notebook.bind("<<NotebookTabChanged>>", lambda _e: self._restyle(),
                      add="+")

    def add(self, frame, text: str) -> None:
        self._nb.add(frame, text=text)
        lbl = tk.Label(self, text=text, font=self._font, padx=10, pady=4,
                       bd=0, highlightthickness=1, cursor="hand2")
        lbl.bind("<Button-1>", lambda _e, f=frame: self._nb.select(f))
        lbl.bind("<Enter>", lambda _e, w=lbl: self._hover(w, True))
        lbl.bind("<Leave>", lambda _e, w=lbl: self._hover(w, False))
        self._tabs.append((frame, lbl))
        self._restyle()
        self.relayout()

    def _selected(self, frame) -> bool:
        try:
            return str(frame) == self._nb.select()
        except tk.TclError:
            return False

    def _restyle(self) -> None:
        for frame, lbl in self._tabs:
            sel = self._selected(frame)
            lbl.configure(bg=Theme.BG_SELECT if sel else Theme.BG_DARK,
                          fg=Theme.AMBER_GLOW if sel else Theme.AMBER_DIM,
                          highlightbackground=Theme.AMBER if sel else Theme.BORDER,
                          highlightcolor=Theme.AMBER if sel else Theme.BORDER)

    def _hover(self, lbl, inside: bool) -> None:
        for frame, other in self._tabs:
            if other is lbl and not self._selected(frame):
                lbl.configure(fg=Theme.AMBER if inside else Theme.AMBER_DIM)

    def relayout(self) -> None:
        """Re-wrap the labels (call after the font size changes)."""
        if self._job is None:
            self._job = self.after_idle(self._reflow)

    def _reflow(self) -> None:
        self._job = None
        width = self.winfo_width()
        if width <= 1:
            return
        x = y = row_h = 0
        for _frame, lbl in self._tabs:
            w, h = lbl.winfo_reqwidth(), lbl.winfo_reqheight()
            if x and x + w > width:
                x = 0
                y += row_h + self._spacing
                row_h = 0
            lbl.place(x=x, y=y)
            x += w + self._spacing
            row_h = max(row_h, h)
        height = max(1, y + row_h)
        if int(self.cget("height")) != height:
            self.configure(height=height)


# -----------------------------------------------------------------------
# Responsive layout — rows that wrap, labels that re-wrap
# -----------------------------------------------------------------------
_FLOW_ROWS: list = []          # (container, reflow) for reflow_all()


def flow_row(container, widgets, spacing=8, pady_between=4):
    """Lay `widgets` (children of `container`) out left to right,
    wrapping onto more lines when the row is too narrow — instead of the
    last ones being cut off in a narrow panel or at a large text size."""
    container.pack_propagate(False)
    try:
        probe = widgets[0]
        probe.update_idletasks()
        container.configure(height=max(24, probe.winfo_reqheight() + 4))
    except Exception:
        container.configure(height=40)

    def _reflow(_event=None):
        try:
            width = container.winfo_width()
        except tk.TclError:
            return
        if width <= 1:
            return
        x = y = row_h = 0
        for w in widgets:
            ww, wh = w.winfo_reqwidth(), w.winfo_reqheight()
            if x > 0 and x + ww > width:
                x = 0
                y += row_h + pady_between
                row_h = 0
            w.place(x=x, y=y)
            x += ww + spacing
            row_h = max(row_h, wh)
        height = max(24, y + row_h + 2)
        if int(container.cget("height")) != height:
            container.configure(height=height)

    container.bind("<Configure>", _reflow, add="+")
    container.after(50, _reflow)
    _FLOW_ROWS.append((container, _reflow))
    return _reflow


def flow_children(container, spacing=6, pady_between=4):
    """flow_row() over every child already packed into `container`."""
    return flow_row(container, list(container.winfo_children()),
                    spacing=spacing, pady_between=pady_between)


def auto_wrap(label, minimum=120):
    """Wrap a label's text to the width it's actually given rather than
    a fixed wraplength (which clips in narrow panels). Pack it with
    fill=tk.X so it follows its parent's width. Returns the label."""
    label.configure(anchor=tk.W, justify=tk.LEFT)

    def _fit(event):
        width = max(minimum, event.width - 4)
        if int(label.cget("wraplength")) != width:
            label.configure(wraplength=width)

    label.bind("<Configure>", _fit, add="+")
    return label


def reflow_all() -> None:
    """Re-run every flow_row (after the text size changed, a row's width
    doesn't change so it gets no <Configure> of its own)."""
    alive = []
    for container, reflow in _FLOW_ROWS:
        try:
            if not container.winfo_exists():
                continue
        except tk.TclError:
            continue
        reflow()
        alive.append((container, reflow))
    _FLOW_ROWS[:] = alive


# -----------------------------------------------------------------------
# Layout helpers
# -----------------------------------------------------------------------
def themed_frame(parent, bg=Theme.BG_PANEL, border=True, border_color=None):
    """Frame with optional 1px border. Returns outer wrapper with .inner."""
    if border:
        outer = tk.Frame(parent, bg=border_color or Theme.BORDER,
                         highlightthickness=0, bd=0)
        inner = tk.Frame(outer, bg=bg, highlightthickness=0, bd=0)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        outer.inner = inner
        return outer
    f = tk.Frame(parent, bg=bg, highlightthickness=0, bd=0)
    f.inner = f
    return f


def panel_header(parent, title, right_text=None, right_var=None,
                 font_spec=None, right_color=None,
                 collapsible=False, body=None, start_collapsed=False):
    """Amber-glow header strip with optional right label and collapse toggle."""
    hdr = tk.Frame(parent, bg=Theme.BG_HEADER, highlightthickness=0, bd=0)
    hdr.pack(fill=tk.X, side=tk.TOP)
    divider = tk.Frame(parent, bg=Theme.BORDER, height=1,
                       highlightthickness=0, bd=0)
    divider.pack(fill=tk.X, side=tk.TOP)

    if collapsible and body is not None:
        prefix = "▾ " if not start_collapsed else "▸ "
    else:
        prefix = "▸ "

    title_label = tk.Label(hdr, text=f"{prefix}{title.upper()}",
                           font=font_spec,
                           fg=Theme.AMBER_GLOW, bg=Theme.BG_HEADER,
                           padx=12, pady=6)
    title_label.pack(side=tk.LEFT)

    right_label = None
    if right_text is not None or right_var is not None:
        right_label = tk.Label(hdr,
                               text=right_text or "",
                               textvariable=right_var,
                               font=font_spec,
                               fg=right_color or Theme.AMBER_DIM,
                               bg=Theme.BG_HEADER,
                               padx=12, pady=6)
        right_label.pack(side=tk.RIGHT)

    if collapsible and body is not None:
        state = {"collapsed": bool(start_collapsed), "title": title.upper()}

        def toggle(_e=None):
            state["collapsed"] = not state["collapsed"]
            if state["collapsed"]:
                body.pack_forget()
                title_label.configure(text=f"▸ {state['title']}")
            else:
                body.pack(fill=tk.X, after=divider)
                title_label.configure(text=f"▾ {state['title']}")

        for w in (hdr, title_label):
            w.bind("<Button-1>", toggle)
            w.configure(cursor="hand2")
        if right_label is not None:
            right_label.configure(cursor="hand2")
            right_label.bind("<Button-1>", toggle)

        if state["collapsed"]:
            body.pack_forget()
        else:
            try:
                body.pack_info()
            except tk.TclError:
                body.pack(fill=tk.X, after=divider)

    return hdr, right_label


def collapsible_section(parent, title, font_spec=None,
                        anchor=tk.W, pady=(0, 6),
                        start_collapsed=False,
                        on_toggle=None,
                        bg=None,
                        right_widget_factory=None,
                        side=tk.TOP):
    """Inline collapsible ▾/▸ section. Returns (header_frame, body_frame).

    `side` controls which edge of `parent` the section is packed against;
    defaults to TOP (the historical behaviour). Pass `side=tk.BOTTOM` to
    anchor the section to the bottom of its parent — useful when other
    widgets above it are `expand=True` and you want this section to
    reserve space first regardless of window height.
    """
    if bg is None:
        bg = Theme.BG_PANEL
    hdr = tk.Frame(parent, bg=bg)
    body = tk.Frame(parent, bg=bg)
    state = {"collapsed": bool(start_collapsed), "title": title}
    prefix = "▾ " if not start_collapsed else "▸ "
    title_label = tk.Label(hdr, text=f"{prefix}{title}",
                           fg=Theme.AMBER_GLOW, bg=bg,
                           font=font_spec, cursor="hand2")
    title_label.pack(side=tk.LEFT)
    if right_widget_factory is not None:
        try:
            right_widget_factory(hdr)
        except Exception:
            pass

    if side == tk.BOTTOM:
        # Pack body first, then header above it, so the visual order
        # is hdr-on-top, body-below — but both anchored at the bottom
        # of the parent.
        body.pack(side=tk.BOTTOM, fill=tk.X)
        hdr.pack(side=tk.BOTTOM, fill=tk.X, anchor=anchor, pady=pady)
        if start_collapsed:
            body.pack_forget()

        def toggle(_e=None):
            state["collapsed"] = not state["collapsed"]
            if state["collapsed"]:
                body.pack_forget()
                title_label.configure(text=f"▸ {state['title']}")
            else:
                body.pack(side=tk.BOTTOM, fill=tk.X, before=hdr)
                title_label.configure(text=f"▾ {state['title']}")
            if on_toggle:
                try: on_toggle(state["collapsed"])
                except Exception: pass
    else:
        hdr.pack(side=side, fill=tk.X, anchor=anchor, pady=pady)
        body.pack(side=side, fill=tk.X, after=hdr)
        if start_collapsed:
            body.pack_forget()

        def toggle(_e=None):
            state["collapsed"] = not state["collapsed"]
            if state["collapsed"]:
                body.pack_forget()
                title_label.configure(text=f"▸ {state['title']}")
            else:
                body.pack(side=side, fill=tk.X, after=hdr)
                title_label.configure(text=f"▾ {state['title']}")
            if on_toggle:
                try: on_toggle(state["collapsed"])
                except Exception: pass

    for w in (hdr, title_label):
        w.bind("<Button-1>", toggle)
    return hdr, body


# -----------------------------------------------------------------------
# Toast notification queue (improvement #12 — stacking toasts)
# -----------------------------------------------------------------------
class ToastQueue:
    """Serialized pop-up notifications that never overlap.

    Previously multiple toasts could fire simultaneously and render on top
    of each other. This queue ensures each toast waits for the previous one
    to expire before appearing.
    """

    def __init__(self, root, font_spec):
        self._root = root
        self._font = font_spec
        self._queue: list[tuple] = []   # (msg, level, duration_ms)
        self._active = False
        self._label: tk.Label | None = None

    def push(self, message: str, level: str = "info", duration_ms: int = 2500):
        self._queue.append((message, level, duration_ms))
        if not self._active:
            self._show_next()

    def _show_next(self):
        if not self._queue:
            self._active = False
            return
        self._active = True
        msg, level, duration_ms = self._queue.pop(0)
        color_map = {
            "info":    Theme.AMBER_DIM,
            "warn":    Theme.AMBER_GLOW,
            "error":   Theme.RED,
            "success": Theme.GREEN,
        }
        fg = color_map.get(level, Theme.AMBER_DIM)
        if self._label is not None:
            try:
                self._label.destroy()
            except Exception:
                pass
        self._label = tk.Label(
            self._root,
            text=f"  {msg}  ",
            fg=fg, bg=Theme.BG_HEADER,
            font=self._font,
            bd=1, relief="solid",
            highlightbackground=fg,
            highlightthickness=1,
        )
        # Place at bottom-right of the root window
        self._label.place(relx=1.0, rely=1.0, anchor="se", x=-10, y=-10)
        self._root.after(duration_ms, self._expire)

    def _expire(self):
        if self._label is not None:
            try:
                self._label.destroy()
            except Exception:
                pass
            self._label = None
        self._root.after(120, self._show_next)
