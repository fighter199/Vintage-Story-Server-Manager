"""
host/console.py — The server console panel: output, filter, copy, right-click menu,
and appending lines.

Methods of ServerManagerApp (VSSM.py), moved here unchanged.
"""
from __future__ import annotations

import tkinter as tk
from datetime import datetime
from tkinter import ttk

# ── Local packages ─────────────────────────────────────────────────────
from ui.theme import (Theme)
from ui.widgets import (TermButton, TermEntry, themed_frame,
                         panel_header)


class ConsoleMixin:

    # ------------------------------------------------------------------
    # Console panel
    # ------------------------------------------------------------------
    def _build_console(self, parent):
        panel = themed_frame(parent)
        panel.pack(fill=tk.BOTH, expand=True)
        inner = panel.inner
        _, self.console_status_label = panel_header(
            inner, "Server Console", right_text="Idle", font_spec=self.F_HDR)

        # Filter row
        filter_row = tk.Frame(inner, bg=Theme.BG_PANEL)
        filter_row.pack(fill=tk.X, padx=10, pady=(8, 6))
        tk.Label(filter_row, text="FILTER:", fg=Theme.AMBER_DIM,
                 bg=Theme.BG_PANEL, font=self.F_SMALL).pack(side=tk.LEFT)
        TermEntry(filter_row, textvariable=self.log_filter_var,
                  font_spec=self.F_NORMAL).pack(side=tk.LEFT, fill=tk.X,
                                                expand=True, padx=8, ipady=2)
        TermButton(filter_row, "Copy", self._copy_console_view,
                   variant="amber", font_spec=self.F_SMALL,
                   padx=8, pady=2).pack(side=tk.LEFT, padx=(0, 4))
        self.log_filter_var.trace_add('write', lambda *_: self.update_console_display())

        # Console text widget
        console_wrap = tk.Frame(inner, bg=Theme.BORDER)
        console_wrap.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        console_inner = tk.Frame(console_wrap, bg=Theme.BG_INPUT)
        console_inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self.console_text = tk.Text(
            console_inner, bg=Theme.BG_INPUT, fg=Theme.AMBER,
            insertbackground=Theme.AMBER_GLOW,
            selectbackground=Theme.BG_SELECT,
            selectforeground=Theme.AMBER_GLOW,
            font=self.F_CONSOLE, bd=0, highlightthickness=0,
            wrap=tk.WORD, state='disabled', padx=8, pady=6)
        self.console_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(console_inner, orient=tk.VERTICAL,
                           style="Term.Vertical.TScrollbar",
                           command=self.console_text.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y, before=self.console_text)
        self.console_text.configure(yscrollcommand=sb.set)
        c = self.console_text
        c.tag_configure("info",      foreground=Theme.AMBER)
        c.tag_configure("warn",      foreground=Theme.AMBER_GLOW)
        c.tag_configure("error",     foreground=Theme.RED)
        c.tag_configure("success",   foreground=Theme.GREEN)
        c.tag_configure("system",    foreground=Theme.CYAN)
        c.tag_configure("player",    foreground=Theme.PURPLE)
        c.tag_configure("chat",      foreground=Theme.AMBER_GLOW)
        c.tag_configure("echo",      foreground=Theme.AMBER_DIM)
        c.tag_configure("timestamp", foreground=Theme.MUTED)

        # Command input row
        cmd_wrap = tk.Frame(inner, bg=Theme.BORDER)
        cmd_wrap.pack(fill=tk.X, padx=10, pady=(0, 10))
        cmd_inner = tk.Frame(cmd_wrap, bg=Theme.BG_INPUT)
        cmd_inner.pack(fill=tk.X, padx=1, pady=1)
        cmd_inner.columnconfigure(1, weight=1)
        self.prompt_label = tk.Label(cmd_inner, text=" ❯ ",
                                     fg=Theme.AMBER_GLOW, bg=Theme.BG_INPUT,
                                     font=self.F_HDR)
        self.prompt_label.grid(row=0, column=0, padx=(6, 0), sticky="w")
        self.command_entry = tk.Entry(
            cmd_inner, textvariable=self.command_var,
            bg=Theme.BG_INPUT, fg=Theme.AMBER_GLOW,
            insertbackground=Theme.AMBER_GLOW,
            selectbackground=Theme.BG_SELECT, selectforeground=Theme.AMBER_GLOW,
            font=self.F_NORMAL, bd=0, highlightthickness=0, width=1,
            state='disabled',
            disabledbackground=Theme.BG_INPUT, disabledforeground=Theme.AMBER_FAINT)
        self.command_entry.grid(row=0, column=1, sticky="ew", padx=4, ipady=8)
        self.command_entry.bind("<Return>", lambda e: self.send_command())
        self.btn_send = TermButton(cmd_inner, "Send", self.send_command,
                                   variant="amber", font_spec=self.F_SMALL,
                                   padx=14, pady=4)
        self.btn_send.grid(row=0, column=2, padx=4, pady=4, sticky="e")
        self.btn_send.set_enabled(False)

        # Broadcast row
        chat_wrap = tk.Frame(inner, bg=Theme.BORDER)
        chat_wrap.pack(fill=tk.X, padx=10, pady=(0, 10))
        chat_inner = tk.Frame(chat_wrap, bg=Theme.BG_INPUT)
        chat_inner.pack(fill=tk.X, padx=1, pady=1)
        chat_inner.columnconfigure(1, weight=1)
        tk.Label(chat_inner, text=" 📢 ", fg=Theme.CYAN,
                 bg=Theme.BG_INPUT, font=self.F_NORMAL
                 ).grid(row=0, column=0, padx=(6, 0), sticky="w")
        self.chat_var = tk.StringVar()

        def _do_chat(_e=None):
            msg = self.chat_var.get().strip()
            if msg and self.is_running:
                if self.broadcast(msg):
                    self.chat_var.set("")
            elif not self.is_running:
                self._notify("Server not running.", level="warn")

        self.chat_entry = tk.Entry(
            chat_inner, textvariable=self.chat_var,
            bg=Theme.BG_INPUT, fg=Theme.AMBER_GLOW,
            insertbackground=Theme.AMBER_GLOW,
            selectbackground=Theme.BG_SELECT, selectforeground=Theme.AMBER_GLOW,
            font=self.F_NORMAL, bd=0, highlightthickness=0, width=1)
        self.chat_entry.grid(row=0, column=1, sticky="ew", padx=4, ipady=6)
        self.chat_entry.bind("<Return>", _do_chat)
        self.btn_broadcast = TermButton(chat_inner, "Say", _do_chat,
                                        variant="amber", font_spec=self.F_SMALL,
                                        padx=14, pady=4)
        self.btn_broadcast.grid(row=0, column=2, padx=4, pady=4, sticky="e")


    # ------------------------------------------------------------------
    # Console right-click (improvement #10)
    # ------------------------------------------------------------------
    def _console_right_click(self, event):
        try:
            idx = self.console_text.index(f"@{event.x},{event.y}")
            line_start = idx.split('.')[0] + ".0"
            line_end   = idx.split('.')[0] + ".end"
            line_text  = self.console_text.get(line_start, line_end).strip()
        except Exception:
            return
        m = tk.Menu(self, tearoff=0, bg=Theme.BG_PANEL, fg=Theme.AMBER,
                    activebackground=Theme.BG_SELECT,
                    activeforeground=Theme.AMBER_GLOW,
                    bd=0, font=self.F_SMALL)
        m.add_command(label="Copy this line",
                      command=lambda t=line_text: self._copy_to_clipboard(t))
        m.add_command(label="Copy all visible",
                      command=self._copy_console_view)
        m.add_separator()
        m.add_command(label="Clear console", command=self.clear_console)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()


    # ------------------------------------------------------------------
    # Console
    # ------------------------------------------------------------------
    def append_console(self, text: str, tag: str = "info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.all_output_lines.append((timestamp, text, tag))
        # Bound the scrollback so a long-running server can't grow
        # memory without limit.
        overflow = len(self.all_output_lines) - self.MAX_CONSOLE_LINES
        if overflow > 0:
            del self.all_output_lines[:overflow]
        filt = self.log_filter_var.get().lower()
        if filt and filt not in text.lower():
            return
        self._append_to_console_widget(timestamp, text, tag)


    def _append_to_console_widget(self, timestamp, text, tag):
        self.console_text.configure(state='normal')
        self.console_text.insert(tk.END, f"[{timestamp}] ", ("timestamp",))
        self.console_text.insert(tk.END, text + "\n", (tag,))
        # Trim the widget to the same cap as all_output_lines. Cheap:
        # one index query per append, deletion only on overflow.
        try:
            line_count = int(
                self.console_text.index("end-1c").split(".")[0])
            if line_count > self.MAX_CONSOLE_LINES:
                self.console_text.delete(
                    "1.0", f"{line_count - self.MAX_CONSOLE_LINES + 1}.0")
        except (ValueError, tk.TclError):
            pass
        self.console_text.see(tk.END)
        self.console_text.configure(state='disabled')


    def update_console_display(self):
        filt = self.log_filter_var.get().lower()
        self.console_text.configure(state='normal')
        self.console_text.delete("1.0", tk.END)
        for ts, text, tag in self.all_output_lines:
            if filt and filt not in text.lower():
                continue
            self.console_text.insert(tk.END, f"[{ts}] ", ("timestamp",))
            self.console_text.insert(tk.END, text + "\n", (tag,))
        self.console_text.see(tk.END)
        self.console_text.configure(state='disabled')


    def clear_console(self):
        self.all_output_lines.clear()
        self.console_text.configure(state='normal')
        self.console_text.delete("1.0", tk.END)
        self.console_text.configure(state='disabled')
        self.append_console("Console cleared.", "system")


    def _copy_console_view(self):
        filt = self.log_filter_var.get().lower()
        lines = [f"[{ts}] {text}" for ts, text, _ in self.all_output_lines
                 if not filt or filt in text.lower()]
        if not lines:
            self._notify("Nothing to copy.", level="info", duration_ms=1200)
            return
        try:
            self.clipboard_clear()
            self.clipboard_append("\n".join(lines))
            self._notify(f"Copied {len(lines)} lines.", level="success", duration_ms=1800)
        except Exception:
            pass
