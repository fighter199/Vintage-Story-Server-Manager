"""
ui/tab_autorun.py — "AUTORUN" tab.

Rules fire console commands at fixed intervals while the server is up.
Mirrors the structure of tab_custom_commands.py: a rule list on the
left, an editor on the right, an audit strip across the bottom.

Each rule has:
    - name             — human-readable label, also used as the dedup key
    - enabled          — toggle without deleting
    - interval         — value + unit (seconds / minutes / hours)
    - commands         — multi-line text, one console command per line,
                         lines starting with `#` are treated as comments
    - run_on_start     — fire once at server start (then on interval)
    - pause_when_empty — skip ticks when 0 players are online

Rules are per-profile, persisted alongside custom commands by the
settings layer. The scheduler that actually runs them lives on
ServerManagerApp; this tab only edits the rule list.
"""
from __future__ import annotations

import os
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from .theme import Theme
from .widgets import (TermButton, TermEntry, TermCheckbutton, TermText,
                      ScrollableFrame, panel_header)
from core.autorun import (AutorunAudit, expand_commands, make_empty_rule,
                          normalize_rule, validate_rule)


# Unit choices and their multipliers to seconds
_UNITS = [("seconds", 1), ("minutes", 60), ("hours", 3600)]
_UNIT_NAMES = [u[0] for u in _UNITS]
_UNIT_FACTOR = dict(_UNITS)


def _split_interval(secs: int) -> tuple[int, str]:
    """Pick the largest unit that divides cleanly. Falls back to seconds."""
    if secs <= 0:
        return 1, "seconds"
    if secs % 3600 == 0:
        return secs // 3600, "hours"
    if secs % 60 == 0:
        return secs // 60, "minutes"
    return secs, "seconds"


class AutorunTab:
    """Builds and owns the Autorun tab content.

    The host (ServerManagerApp) is responsible for owning the
    AutorunScheduler instance and ticking it; this tab only edits
    rules and renders audit output.
    """

    def __init__(self, parent: tk.Frame, app):
        self._parent = parent
        self._app = app
        self._selected_index: int | None = None
        self._audit_log: list[AutorunAudit] = []
        self._max_audit = 80
        self._build(parent)
        self._refresh_list()
        rules = self._load_rules()
        if rules:
            self._selected_index = 0
            try:
                self._listbox.selection_clear(0, tk.END)
                self._listbox.selection_set(0)
                self._listbox.see(0)
            except Exception:
                pass
            self._load_rule_into_editor(rules[0])

    # ------------------------------------------------------------------
    # Public API used by the host app
    # ------------------------------------------------------------------
    def get_rules(self) -> list[dict]:
        """Provider for the AutorunScheduler — called once per tick."""
        return self._load_rules()

    def reload_from_settings(self) -> None:
        """Re-render the list after an external settings change
        (e.g. profile switch)."""
        self._refresh_list()

    def record_audit(self, audit: AutorunAudit) -> None:
        """Called by the host on every scheduler decision."""
        self._audit_log.insert(0, audit)
        if len(self._audit_log) > self._max_audit:
            self._audit_log = self._audit_log[: self._max_audit]
        try:
            self._render_audit()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------
    def _build(self, parent: tk.Frame) -> None:
        outer = tk.Frame(parent, bg=Theme.BG_PANEL)
        outer.pack(fill=tk.BOTH, expand=True)

        # Header
        head = tk.Frame(outer, bg=Theme.BG_PANEL)
        head.pack(fill=tk.X)
        panel_header(head, "Autorun (interval-based commands)",
                     font_spec=self._app.F_HDR)
        sub = tk.Label(
            head,
            text=("Each rule fires its commands every N seconds while "
                  "the server is running. Rules are per-profile."),
            fg=Theme.MUTED, bg=Theme.BG_PANEL,
            font=self._app.F_SMALL, justify=tk.LEFT,
        )
        sub.pack(anchor=tk.W, padx=6, pady=(0, 4))

        # Bottom-up packing so narrow windows don't clip the audit strip.
        help_row = tk.Frame(outer, bg=Theme.BG_DARK)
        help_row.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=(2, 4))
        tk.Label(
            help_row,
            text=("One console command per line.  Lines starting with # "
                  "are comments.  Interval counts from server start, "
                  "not from when the rule was edited."),
            fg=Theme.MUTED, bg=Theme.BG_DARK,
            font=self._app.F_SMALL, wraplength=900, justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=6)

        self._build_audit_panel(outer)

        # Split
        paned = ttk.PanedWindow(outer, orient=tk.HORIZONTAL,
                                 style="Term.TPanedwindow")
        paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        list_frame = tk.Frame(paned, bg=Theme.BG_PANEL)
        paned.add(list_frame, weight=1)
        self._build_list_panel(list_frame)

        editor_frame = tk.Frame(paned, bg=Theme.BG_PANEL)
        paned.add(editor_frame, weight=2)
        self._build_editor_panel(editor_frame)

    # ------------------------------------------------------------------
    def _build_list_panel(self, parent: tk.Frame) -> None:
        panel_header(parent, "Rules", font_spec=self._app.F_HDR)

        btn_row = tk.Frame(parent, bg=Theme.BG_PANEL)
        btn_row.pack(fill=tk.X, padx=6, pady=(6, 2))
        TermButton(btn_row, "+ Add", self._on_add,
                   variant="start", font_spec=self._app.F_SMALL,
                   padx=8, pady=3).pack(side=tk.LEFT, padx=(0, 4))
        TermButton(btn_row, "Dup", self._on_duplicate,
                   variant="amber", font_spec=self._app.F_SMALL,
                   padx=8, pady=3).pack(side=tk.LEFT, padx=(0, 4))
        TermButton(btn_row, "Delete", self._on_delete,
                   variant="stop", font_spec=self._app.F_SMALL,
                   padx=8, pady=3).pack(side=tk.LEFT)

        list_wrap = tk.Frame(parent, bg=Theme.BORDER)
        list_wrap.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        list_inner = tk.Frame(list_wrap, bg=Theme.BG_INPUT)
        list_inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        self._listbox = tk.Listbox(
            list_inner,
            bg=Theme.BG_INPUT,
            fg=Theme.AMBER,
            selectbackground=Theme.BG_SELECT,
            selectforeground=Theme.AMBER_GLOW,
            activestyle="none",
            font=self._app.F_NORMAL,
            bd=0,
            highlightthickness=0,
        )
        self._listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lsb = ttk.Scrollbar(list_inner, orient=tk.VERTICAL,
                             style="Term.Vertical.TScrollbar",
                             command=self._listbox.yview)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._listbox.configure(yscrollcommand=lsb.set)
        self._listbox.bind("<<ListboxSelect>>", self._on_list_select)

    # ------------------------------------------------------------------
    def _build_editor_panel(self, parent: tk.Frame) -> None:
        panel_header(parent, "Rule Editor", font_spec=self._app.F_HDR)
        sf = ScrollableFrame(parent, bg=Theme.BG_PANEL)
        sf.pack(fill=tk.BOTH, expand=True)
        body = sf.body
        pad = 8

        # Enabled toggle
        self._enabled_var = tk.BooleanVar(value=True)
        TermCheckbutton(body, "Rule enabled",
                        self._enabled_var,
                        font_spec=self._app.F_NORMAL,
                        command=self._on_field_change
                        ).pack(anchor=tk.W, padx=pad, pady=(8, 2))

        # Name
        tk.Label(body, text="NAME  (label shown in the list):",
                 fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL).pack(anchor=tk.W, padx=pad,
                                              pady=(6, 1))
        self._name_var = tk.StringVar()
        self._name_var.trace_add("write",
                                  lambda *_: self._on_field_change())
        TermEntry(body, textvariable=self._name_var,
                  font_spec=self._app.F_NORMAL).pack(
            fill=tk.X, padx=pad, ipady=3)

        # Interval value + unit
        int_row = tk.Frame(body, bg=Theme.BG_PANEL)
        int_row.pack(fill=tk.X, padx=pad, pady=(10, 2))
        tk.Label(int_row, text="INTERVAL:",
                 fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL).pack(side=tk.LEFT)
        self._interval_var = tk.StringVar(value="5")
        TermEntry(int_row, textvariable=self._interval_var, width=8,
                  font_spec=self._app.F_NORMAL).pack(side=tk.LEFT,
                                                     padx=6, ipady=2)
        self._unit_var = tk.StringVar(value="minutes")
        unit_menu = ttk.Combobox(int_row, textvariable=self._unit_var,
                                  values=_UNIT_NAMES, state="readonly",
                                  width=10, font=self._app.F_SMALL)
        unit_menu.pack(side=tk.LEFT, padx=2)
        tk.Label(int_row,
                 text="(time between fires; minimum 1 second)",
                 fg=Theme.MUTED, bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL).pack(side=tk.LEFT, padx=(8, 0))

        # Run-on-start
        self._run_on_start_var = tk.BooleanVar(value=False)
        TermCheckbutton(body, "Run once when the server starts "
                              "(then continue on interval)",
                        self._run_on_start_var,
                        font_spec=self._app.F_SMALL,
                        command=self._on_field_change
                        ).pack(anchor=tk.W, padx=pad, pady=(8, 2))

        # Pause when empty
        self._pause_when_empty_var = tk.BooleanVar(value=False)
        TermCheckbutton(body, "Pause when 0 players are online "
                              "(skip the tick, don't double-fire later)",
                        self._pause_when_empty_var,
                        font_spec=self._app.F_SMALL,
                        command=self._on_field_change
                        ).pack(anchor=tk.W, padx=pad, pady=(2, 6))

        # Commands
        tk.Label(body, text="COMMANDS  (one per line):",
                 fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL).pack(anchor=tk.W, padx=pad,
                                              pady=(6, 1))
        cmd_wrap = tk.Frame(body, bg=Theme.BORDER)
        cmd_wrap.pack(fill=tk.BOTH, expand=True, padx=pad, pady=(0, 6))
        cmd_inner = tk.Frame(cmd_wrap, bg=Theme.BG_INPUT)
        cmd_inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self._cmd_text = tk.Text(
            cmd_inner, height=8,
            bg=Theme.BG_INPUT, fg=Theme.AMBER_GLOW,
            insertbackground=Theme.AMBER_GLOW,
            selectbackground=Theme.BG_SELECT,
            selectforeground=Theme.AMBER_GLOW,
            font=self._app.F_NORMAL,
            bd=0, highlightthickness=0, wrap=tk.NONE,
        )
        self._cmd_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        cmd_sb = ttk.Scrollbar(cmd_inner, orient=tk.VERTICAL,
                                style="Term.Vertical.TScrollbar",
                                command=self._cmd_text.yview)
        cmd_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._cmd_text.configure(yscrollcommand=cmd_sb.set)

        # Status line + buttons
        self._status_var = tk.StringVar(value="")
        tk.Label(body, textvariable=self._status_var,
                 fg=Theme.MUTED, bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL,
                 anchor=tk.W).pack(fill=tk.X, padx=pad, pady=(0, 4))

        btn_row = tk.Frame(body, bg=Theme.BG_PANEL)
        btn_row.pack(fill=tk.X, padx=pad, pady=(2, 12))
        TermButton(btn_row, "💾 Save", self._on_save,
                   variant="start", font_spec=self._app.F_BTN,
                   padx=10, pady=4).pack(side=tk.LEFT, padx=(0, 4))
        TermButton(btn_row, "↺ Discard", self._on_discard,
                   variant="amber", font_spec=self._app.F_SMALL,
                   padx=10, pady=4).pack(side=tk.LEFT)

    # ------------------------------------------------------------------
    def _build_audit_panel(self, parent: tk.Frame) -> None:
        wrap = tk.Frame(parent, bg=Theme.BORDER)
        wrap.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=(2, 0))
        inner = tk.Frame(wrap, bg=Theme.BG_INPUT)
        inner.pack(fill=tk.X, padx=1, pady=1)
        tk.Label(inner, text="Recent fires", fg=Theme.AMBER_DIM,
                 bg=Theme.BG_INPUT,
                 font=self._app.F_SMALL,
                 anchor=tk.W).pack(fill=tk.X, padx=4, pady=(2, 0))
        self._audit_text = tk.Text(
            inner, height=4,
            bg=Theme.BG_INPUT, fg=Theme.AMBER,
            font=self._app.F_SMALL,
            bd=0, highlightthickness=0, wrap=tk.NONE,
            state=tk.DISABLED,
        )
        self._audit_text.pack(fill=tk.X, padx=4, pady=2)
        self._audit_text.tag_configure("fired",   foreground=Theme.GREEN)
        self._audit_text.tag_configure("blocked", foreground=Theme.AMBER_DIM)

    def _render_audit(self) -> None:
        self._audit_text.configure(state=tk.NORMAL)
        self._audit_text.delete("1.0", tk.END)
        for a in self._audit_log[:30]:
            ts = datetime.fromtimestamp(a.timestamp).strftime("%H:%M:%S")
            if a.fired:
                head = f"[{ts}] FIRED  {a.rule_name}"
                self._audit_text.insert(tk.END, head + "\n", "fired")
                for c in a.commands:
                    self._audit_text.insert(tk.END,
                                             f"          → {c}\n", "fired")
            else:
                reason = a.skipped_reason or "skipped"
                self._audit_text.insert(
                    tk.END,
                    f"[{ts}] {reason.upper():<14} {a.rule_name}\n",
                    "blocked")
        self._audit_text.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Listbox
    # ------------------------------------------------------------------
    def _refresh_list(self) -> None:
        rules = self._load_rules()
        self._listbox.delete(0, tk.END)
        for rule in rules:
            normalize_rule(rule)
            name = rule.get("name") or "(unnamed)"
            enabled = rule.get("enabled", True)
            secs = int(rule.get("interval_secs", 0))
            n, unit = _split_interval(secs)
            icon = "✓" if enabled else "○"
            badges = []
            if rule.get("run_on_start"):     badges.append("@start")
            if rule.get("pause_when_empty"): badges.append("paused-if-empty")
            badge_str = ("  " + " ".join(badges)) if badges else ""
            self._listbox.insert(
                tk.END,
                f" {icon}  {name}  every {n} {unit}{badge_str}")
        if self._selected_index is not None:
            count = self._listbox.size()
            if count > 0:
                idx = min(self._selected_index, count - 1)
                self._listbox.selection_set(idx)
                self._listbox.see(idx)

    def _on_list_select(self, _event=None) -> None:
        sel = self._listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        self._selected_index = idx
        rules = self._load_rules()
        if idx < len(rules):
            self._load_rule_into_editor(rules[idx])

    # ------------------------------------------------------------------
    # Editor field <-> rule dict
    # ------------------------------------------------------------------
    def _load_rule_into_editor(self, rule: dict) -> None:
        normalize_rule(rule)
        self._enabled_var.set(rule.get("enabled", True))
        self._name_var.set(rule.get("name") or "")
        n, unit = _split_interval(int(rule.get("interval_secs", 300)))
        self._interval_var.set(str(n))
        self._unit_var.set(unit)
        self._run_on_start_var.set(bool(rule.get("run_on_start")))
        self._pause_when_empty_var.set(bool(rule.get("pause_when_empty")))
        self._cmd_text.delete("1.0", tk.END)
        self._cmd_text.insert("1.0", rule.get("commands") or "")
        self._status_var.set("")

    def _collect_rule_from_editor(self) -> dict:
        try:
            n = int(self._interval_var.get() or "0")
        except ValueError:
            n = 0
        unit = self._unit_var.get() or "seconds"
        secs = n * _UNIT_FACTOR.get(unit, 1)
        return {
            "name":             self._name_var.get().strip(),
            "enabled":          self._enabled_var.get(),
            "interval_secs":    secs,
            "commands":         self._cmd_text.get("1.0", tk.END).rstrip(),
            "run_on_start":     self._run_on_start_var.get(),
            "pause_when_empty": self._pause_when_empty_var.get(),
        }

    def _on_field_change(self) -> None:
        # Lightweight live validation just for the status line.
        rule = self._collect_rule_from_editor()
        ok, reason = validate_rule(rule)
        if ok:
            self._status_var.set("✓ ready to save")
        else:
            self._status_var.set(f"⚠ {reason}")

    # ------------------------------------------------------------------
    # Toolbar actions
    # ------------------------------------------------------------------
    def _on_add(self) -> None:
        rules = self._load_rules()
        new_rule = make_empty_rule()
        new_rule["name"] = self._unique_name(rules, "New rule")
        rules.append(new_rule)
        self._save_rules(rules)
        self._selected_index = len(rules) - 1
        self._refresh_list()
        self._load_rule_into_editor(rules[-1])
        self._notify_scheduler_changed()

    def _on_duplicate(self) -> None:
        if self._selected_index is None:
            self._app._notify("Pick a rule to duplicate.", level="warn")
            return
        rules = self._load_rules()
        if self._selected_index >= len(rules):
            return
        clone = dict(rules[self._selected_index])
        clone["name"] = self._unique_name(rules,
                                           clone.get("name") or "rule")
        rules.insert(self._selected_index + 1, clone)
        self._save_rules(rules)
        self._selected_index += 1
        self._refresh_list()
        self._load_rule_into_editor(clone)
        self._notify_scheduler_changed()

    def _on_delete(self) -> None:
        if self._selected_index is None:
            self._app._notify("Pick a rule to delete.", level="warn")
            return
        rules = self._load_rules()
        if self._selected_index >= len(rules):
            return
        target = rules[self._selected_index]
        ok = messagebox.askokcancel(
            "Delete rule",
            f"Delete the rule '{target.get('name') or '(unnamed)'}'?",
            parent=self._parent)
        if not ok:
            return
        del rules[self._selected_index]
        self._save_rules(rules)
        if rules:
            self._selected_index = min(self._selected_index, len(rules) - 1)
        else:
            self._selected_index = None
        self._refresh_list()
        if rules and self._selected_index is not None:
            self._load_rule_into_editor(rules[self._selected_index])
        self._notify_scheduler_changed()

    def _on_save(self) -> None:
        if self._selected_index is None:
            self._app._notify("Pick a rule first, or click + Add.",
                              level="warn")
            return
        rule = self._collect_rule_from_editor()
        ok, reason = validate_rule(rule)
        if not ok:
            self._app._notify(f"Can't save: {reason}", level="error")
            return
        rules = self._load_rules()
        if self._selected_index >= len(rules):
            return
        rules[self._selected_index] = rule
        self._save_rules(rules)
        self._refresh_list()
        self._app._notify("Autorun rule saved.", level="success",
                          duration_ms=1800)
        self._notify_scheduler_changed()

    def _on_discard(self) -> None:
        if self._selected_index is None:
            return
        rules = self._load_rules()
        if self._selected_index < len(rules):
            self._load_rule_into_editor(rules[self._selected_index])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _unique_name(rules: list[dict], base: str) -> str:
        existing = {(r.get("name") or "").strip().lower() for r in rules}
        if base.strip().lower() not in existing:
            return base
        for i in range(2, 1000):
            cand = f"{base} ({i})"
            if cand.strip().lower() not in existing:
                return cand
        return base

    def _notify_scheduler_changed(self) -> None:
        """Tell the host to re-arm the scheduler. Best-effort — if the
        host doesn't expose the hook the change is still picked up on
        the next tick anyway, since the scheduler reads via the
        provider every tick."""
        fn = getattr(self._app, "_autorun_rules_changed", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load_rules(self) -> list[dict]:
        from core.settings import load_autorun_rules
        return load_autorun_rules(self._app._settings)

    def _save_rules(self, rules: list[dict]) -> None:
        from core.settings import save_autorun_rules, save_settings
        save_autorun_rules(self._app._settings, rules)
        save_settings(self._app._settings)
