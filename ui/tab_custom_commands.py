"""
ui/tab_custom_commands.py — "CUSTOM CMDS" tab builder.

Lets admins define rules of the form:
  "When a player says <trigger> in chat, run <console command(s)>.
   Optionally restrict by player role, set a per-rule cooldown,
   and capture positional arguments after the trigger."

Features:
  - Add / Edit / Delete / Duplicate rules
  - Enable / disable individual rules without deleting them
  - Roles multi-select (blank = any role)
  - Multi-line response field with placeholder reference popup
  - {player} {role} {1}-{9} {target} {args} placeholders
  - Per-rule cooldown (seconds)
  - Destructive-action opt-in checkbox for /stop /ban /op …
  - Live preview that runs the dispatcher against a sample player
  - Import / export rule sets as JSON
  - Audit-log panel showing recent triggers (fired + skipped)
  - Per-profile rules (inherited from settings layer)
"""
from __future__ import annotations

import copy
import json
import os
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from .theme import Theme
from .widgets import (TermButton, TermEntry, TermCheckbutton, TermText,
                      ScrollableFrame, themed_frame, panel_header,
                      collapsible_section)
from core.custom_commands import (make_empty_rule, normalize_rule,
                                   validate_rule, ChatCommandDispatcher,
                                   AuditRecord)

KNOWN_ROLES = ["suplayer", "suadmin", "admin", "operator", "guest"]


class CustomCommandsTab:
    """Builds and owns the Custom Commands tab content.

    Parameters
    ----------
    parent  : tk.Frame  — the notebook frame to pack into
    app     : ServerManagerApp  — for fonts, notify, save_settings, etc.
    """

    def __init__(self, parent: tk.Frame, app):
        self._parent = parent
        self._app = app
        self._selected_index: int | None = None
        # Recent audit records (most recent first)
        self._audit_log: list[AuditRecord] = []
        self._max_audit = 80
        self._build(parent)
        self._refresh_list()
        # If there are existing rules, auto-select the first one so the
        # editor opens populated rather than empty.
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
        """Return the live rule list (called by ChatCommandDispatcher)."""
        from core.settings import load_custom_commands
        return load_custom_commands(self._app._settings)

    def reload_from_settings(self) -> None:
        """Re-render the list after an external settings change."""
        self._refresh_list()

    def record_audit(self, audit: AuditRecord) -> None:
        """Called by the host on every dispatch. Updates the audit panel."""
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

        # Header + global action row
        head = tk.Frame(outer, bg=Theme.BG_PANEL)
        head.pack(fill=tk.X)
        panel_header(head, "Custom Chat Commands",
                     font_spec=self._app.F_HDR)
        actions = tk.Frame(head, bg=Theme.BG_PANEL)
        actions.pack(fill=tk.X, padx=6, pady=(0, 4))
        TermButton(actions, "📥 Import…", self._on_import,
                   variant="amber", font_spec=self._app.F_SMALL,
                   padx=8, pady=3).pack(side=tk.LEFT, padx=(0, 4))
        TermButton(actions, "📤 Export…", self._on_export,
                   variant="amber", font_spec=self._app.F_SMALL,
                   padx=8, pady=3).pack(side=tk.LEFT, padx=(0, 4))
        tk.Label(actions,
                 text="(rules are per-profile; switch profile in Settings)",
                 fg=Theme.MUTED, bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL).pack(side=tk.LEFT, padx=(8, 0))

        # IMPORTANT: Pack bottom-anchored items FIRST so they reserve
        # their space before the expanding paned window above them.
        # Otherwise narrow windows clip the help strip and audit panel.
        # Order is bottom-up (last packed sits closest to the paned).

        # Help strip (very bottom)
        help_row = tk.Frame(outer, bg=Theme.BG_DARK)
        help_row.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=(2, 4))
        tk.Label(
            help_row,
            text=(
                "Placeholders:  {player} {role} {target}={1}  {1}–{9}  {args}.   "
                "One console command per line.  Empty role list = anyone."
            ),
            fg=Theme.MUTED, bg=Theme.BG_DARK,
            font=self._app.F_SMALL, wraplength=900, justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=6)

        # Audit log strip (just above help)
        self._build_audit_panel(outer)

        # Split: list (left) + editor (right) — fills remaining space
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
    # Left panel: rule list
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
    # Right panel: rule editor
    # ------------------------------------------------------------------
    def _build_editor_panel(self, parent: tk.Frame) -> None:
        panel_header(parent, "Rule Editor", font_spec=self._app.F_HDR)

        sf = ScrollableFrame(parent, bg=Theme.BG_PANEL)
        sf.pack(fill=tk.BOTH, expand=True)
        body = sf.body
        pad = 8

        # ── Enabled toggle ──────────────────────────────────────────────
        self._enabled_var = tk.BooleanVar(value=True)
        TermCheckbutton(body, "Rule enabled",
                        self._enabled_var,
                        font_spec=self._app.F_NORMAL,
                        command=self._on_field_change
                        ).pack(anchor=tk.W, padx=pad, pady=(8, 2))

        # ── Trigger ─────────────────────────────────────────────────────
        tk.Label(body, text="TRIGGER  (what the player types in chat):",
                 fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL).pack(anchor=tk.W, padx=pad,
                                              pady=(6, 1))
        self._trigger_var = tk.StringVar()
        self._trigger_var.trace_add("write",
                                     lambda *_: self._on_field_change())
        TermEntry(body, textvariable=self._trigger_var,
                  font_spec=self._app.F_NORMAL).pack(
            fill=tk.X, padx=pad, ipady=3)
        tk.Label(body,
                 text="Prefix with ! for commands (e.g. !warp, !home). "
                      "Matched at the start of the message; arguments "
                      "after the trigger are captured as {1}, {2}, …",
                 fg=Theme.MUTED, bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL, wraplength=380, justify=tk.LEFT,
                 ).pack(anchor=tk.W, padx=pad, pady=(1, 4))

        # ── Roles ───────────────────────────────────────────────────────
        tk.Label(body, text="ALLOWED ROLES  (blank = any role):",
                 fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL).pack(anchor=tk.W, padx=pad,
                                              pady=(4, 1))
        roles_chip_frame = tk.Frame(body, bg=Theme.BG_PANEL)
        roles_chip_frame.pack(fill=tk.X, padx=pad, pady=(0, 2))
        self._role_vars: dict[str, tk.BooleanVar] = {}
        for role in KNOWN_ROLES:
            var = tk.BooleanVar(value=False)
            self._role_vars[role] = var
            cb = tk.Checkbutton(
                roles_chip_frame, text=role,
                variable=var,
                fg=Theme.AMBER, bg=Theme.BG_PANEL,
                activeforeground=Theme.AMBER_GLOW,
                activebackground=Theme.BG_PANEL,
                selectcolor=Theme.BG_INPUT,
                font=self._app.F_SMALL,
                command=self._on_field_change,
            )
            cb.pack(side=tk.LEFT, padx=2)
        tk.Label(body, text="EXTRA ROLES  (comma-separated):",
                 fg=Theme.MUTED, bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL).pack(anchor=tk.W, padx=pad,
                                              pady=(4, 1))
        self._extra_roles_var = tk.StringVar()
        self._extra_roles_var.trace_add("write",
                                         lambda *_: self._on_field_change())
        TermEntry(body, textvariable=self._extra_roles_var,
                  font_spec=self._app.F_SMALL).pack(
            fill=tk.X, padx=pad, ipady=2)

        # ── Cooldown ────────────────────────────────────────────────────
        cd_row = tk.Frame(body, bg=Theme.BG_PANEL)
        cd_row.pack(fill=tk.X, padx=pad, pady=(8, 2))
        tk.Label(cd_row, text="COOLDOWN (seconds, 0 = none):",
                 fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL).pack(side=tk.LEFT)
        self._cooldown_var = tk.StringVar(value="0")
        self._cooldown_var.trace_add("write",
                                      lambda *_: self._on_field_change())
        TermEntry(cd_row, textvariable=self._cooldown_var, width=8,
                  font_spec=self._app.F_SMALL).pack(side=tk.LEFT, padx=8,
                                                    ipady=2)
        tk.Label(cd_row, text="(per player, per rule)",
                 fg=Theme.MUTED, bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL).pack(side=tk.LEFT)

        # ── Response ────────────────────────────────────────────────────
        tk.Label(body,
                 text="RESPONSE  (one console command per line):",
                 fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL).pack(anchor=tk.W, padx=pad,
                                              pady=(8, 1))
        resp_wrap = tk.Frame(body, bg=Theme.BORDER)
        resp_wrap.pack(fill=tk.X, padx=pad, pady=(0, 4))
        resp_inner = tk.Frame(resp_wrap, bg=Theme.BG_INPUT)
        resp_inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self._response_text = tk.Text(
            resp_inner, height=5, bg=Theme.BG_INPUT, fg=Theme.AMBER,
            insertbackground=Theme.AMBER_GLOW,
            font=self._app.F_CONSOLE, bd=0, highlightthickness=0,
            wrap=tk.WORD, padx=6, pady=4,
        )
        self._response_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rsb = ttk.Scrollbar(resp_inner, orient=tk.VERTICAL,
                             style="Term.Vertical.TScrollbar",
                             command=self._response_text.yview)
        rsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._response_text.configure(yscrollcommand=rsb.set)
        self._response_text.bind("<<Modified>>", self._on_text_modified)

        # ── Destructive opt-in ──────────────────────────────────────────
        self._destructive_var = tk.BooleanVar(value=False)
        TermCheckbutton(
            body,
            "I understand this rule contains a destructive command "
            "(/stop, /ban, /op, …) and want to allow it",
            self._destructive_var,
            font_spec=self._app.F_SMALL,
            command=self._on_field_change,
        ).pack(anchor=tk.W, padx=pad, pady=(2, 4))

        # ── Save / Discard / Test ───────────────────────────────────────
        btn_row = tk.Frame(body, bg=Theme.BG_PANEL)
        btn_row.pack(fill=tk.X, padx=pad, pady=(4, 6))
        TermButton(btn_row, "💾 Save", self._on_save,
                   variant="start", font_spec=self._app.F_BTN,
                   padx=10, pady=4).pack(side=tk.LEFT, padx=(0, 4))
        TermButton(btn_row, "↺ Discard", self._on_discard,
                   variant="amber", font_spec=self._app.F_SMALL,
                   padx=10, pady=4).pack(side=tk.LEFT)

        # ── Live test panel ─────────────────────────────────────────────
        _, test_body = collapsible_section(
            body, "Live trigger test", font_spec=self._app.F_HDR,
            start_collapsed=False)
        # Sample player + role + message
        row = tk.Frame(test_body, bg=Theme.BG_PANEL)
        row.pack(fill=tk.X, padx=pad, pady=4)
        tk.Label(row, text="As player:", fg=Theme.AMBER_DIM,
                 bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL).pack(side=tk.LEFT)
        self._test_player_var = tk.StringVar(value="Steve")
        self._test_player_var.trace_add("write",
                                         lambda *_: self._update_preview())
        TermEntry(row, textvariable=self._test_player_var, width=12,
                  font_spec=self._app.F_SMALL).pack(side=tk.LEFT, padx=4,
                                                    ipady=2)
        tk.Label(row, text="role:", fg=Theme.AMBER_DIM,
                 bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL).pack(side=tk.LEFT, padx=(6, 0))
        self._test_role_var = tk.StringVar(value="admin")
        self._test_role_var.trace_add("write",
                                       lambda *_: self._update_preview())
        TermEntry(row, textvariable=self._test_role_var, width=10,
                  font_spec=self._app.F_SMALL).pack(side=tk.LEFT, padx=4,
                                                    ipady=2)
        msg_row = tk.Frame(test_body, bg=Theme.BG_PANEL)
        msg_row.pack(fill=tk.X, padx=pad, pady=2)
        tk.Label(msg_row, text="says:", fg=Theme.AMBER_DIM,
                 bg=Theme.BG_PANEL,
                 font=self._app.F_SMALL).pack(side=tk.LEFT)
        self._test_msg_var = tk.StringVar()
        self._test_msg_var.trace_add("write",
                                      lambda *_: self._update_preview())
        TermEntry(msg_row, textvariable=self._test_msg_var,
                  font_spec=self._app.F_SMALL).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=4, ipady=2)

        # Preview output
        self._preview_label = tk.Label(
            test_body, text="(edit the rule to see preview)",
            fg=Theme.AMBER_DIM, bg=Theme.BG_INPUT,
            font=self._app.F_CONSOLE, justify=tk.LEFT,
            anchor=tk.NW, padx=8, pady=4, wraplength=520,
        )
        self._preview_label.pack(fill=tk.X, padx=pad, pady=(2, 4))

        self._set_editor_enabled(False)

    # ------------------------------------------------------------------
    # Audit log panel
    # ------------------------------------------------------------------
    def _build_audit_panel(self, outer: tk.Frame) -> None:
        _, body = collapsible_section(
            outer, "Recent triggers (audit)",
            font_spec=self._app.F_HDR, start_collapsed=True,
            side=tk.BOTTOM)
        wrap = tk.Frame(body, bg=Theme.BORDER)
        wrap.pack(fill=tk.X, padx=6, pady=4)
        inner = tk.Frame(wrap, bg=Theme.BG_INPUT)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self._audit_text = tk.Text(
            inner, height=6, bg=Theme.BG_INPUT, fg=Theme.AMBER,
            font=self._app.F_CONSOLE, bd=0, highlightthickness=0,
            wrap=tk.NONE, state=tk.DISABLED, padx=6, pady=4,
        )
        self._audit_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        asb = ttk.Scrollbar(inner, orient=tk.VERTICAL,
                             style="Term.Vertical.TScrollbar",
                             command=self._audit_text.yview)
        asb.pack(side=tk.RIGHT, fill=tk.Y)
        self._audit_text.configure(yscrollcommand=asb.set)
        # Tag colours
        self._audit_text.tag_configure("fired",   foreground=Theme.AMBER_GLOW)
        self._audit_text.tag_configure("blocked", foreground=Theme.MUTED)
        self._audit_text.tag_configure("denied",  foreground=Theme.RED_DIM)
        self._render_audit()

    def _render_audit(self) -> None:
        if not hasattr(self, "_audit_text"):
            return
        self._audit_text.configure(state=tk.NORMAL)
        self._audit_text.delete("1.0", tk.END)
        if not self._audit_log:
            self._audit_text.insert(
                tk.END, "  (no triggers yet — they will appear here)\n",
                "blocked")
        else:
            for a in self._audit_log:
                ts = datetime.fromtimestamp(a.timestamp).strftime("%H:%M:%S")
                if a.skipped_reason is None:
                    head = f"[{ts}] FIRED  {a.player}({a.role}): {a.trigger}"
                    self._audit_text.insert(tk.END, head + "\n", "fired")
                    for c in a.commands:
                        self._audit_text.insert(tk.END,
                                                 f"          → {c}\n", "fired")
                elif a.skipped_reason == "cooldown":
                    self._audit_text.insert(
                        tk.END,
                        f"[{ts}] COOLDOWN  {a.player}: {a.trigger} "
                        f"({a.cooldown_remaining:.1f}s left)\n",
                        "blocked")
                elif a.skipped_reason == "role":
                    self._audit_text.insert(
                        tk.END,
                        f"[{ts}] DENIED    {a.player}({a.role}): "
                        f"{a.trigger} — role not allowed\n",
                        "denied")
                elif a.skipped_reason == "disabled":
                    self._audit_text.insert(
                        tk.END,
                        f"[{ts}] DISABLED  {a.player}: {a.trigger}\n",
                        "blocked")
                elif a.skipped_reason == "unconfirmed_destructive":
                    self._audit_text.insert(
                        tk.END,
                        f"[{ts}] BLOCKED   {a.player}: {a.trigger} "
                        f"— destructive (unconfirmed)\n",
                        "denied")
        self._audit_text.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Listbox helpers
    # ------------------------------------------------------------------
    def _refresh_list(self) -> None:
        rules = self._load_rules()
        self._listbox.delete(0, tk.END)
        for rule in rules:
            trigger = rule.get("trigger") or "(no trigger)"
            enabled = rule.get("enabled", True)
            roles = rule.get("roles") or []
            role_str = ",".join(roles) if roles else "any"
            cd = rule.get("cooldown_secs", 0)
            cd_str = f" {cd}s" if cd else ""
            icon = "✓" if enabled else "○"
            self._listbox.insert(
                tk.END,
                f" {icon}  {trigger}  [{role_str}]{cd_str}")
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
            self._set_editor_enabled(True)

    # ------------------------------------------------------------------
    # Editor helpers
    # ------------------------------------------------------------------
    def _load_rule_into_editor(self, rule: dict) -> None:
        normalize_rule(rule)
        self._enabled_var.set(rule.get("enabled", True))
        self._trigger_var.set(rule.get("trigger") or "")

        roles = rule.get("roles") or []
        known_set = set(roles) & set(KNOWN_ROLES)
        extras = [r for r in roles if r not in KNOWN_ROLES]
        for role, var in self._role_vars.items():
            var.set(role in known_set)
        self._extra_roles_var.set(", ".join(extras))

        self._cooldown_var.set(str(rule.get("cooldown_secs", 0)))
        self._destructive_var.set(bool(rule.get("confirmed_destructive")))

        self._response_text.delete("1.0", tk.END)
        self._response_text.insert("1.0", rule.get("response") or "")
        self._response_text.edit_modified(False)
        self._update_preview()

    def _collect_rule_from_editor(self) -> dict:
        trigger = self._trigger_var.get().strip()
        response = self._response_text.get("1.0", tk.END).strip()
        roles = [r for r, v in self._role_vars.items() if v.get()]
        extra = [r.strip() for r in self._extra_roles_var.get().split(",")
                 if r.strip()]
        roles += extra
        try:
            cd = float(self._cooldown_var.get() or "0")
        except ValueError:
            cd = 0
        return {
            "trigger":               trigger,
            "response":              response,
            "roles":                 roles,
            "enabled":               self._enabled_var.get(),
            "cooldown_secs":         cd,
            "confirmed_destructive": self._destructive_var.get(),
        }

    def _set_editor_enabled(self, enabled: bool) -> None:
        # No-op kept for callsite compatibility. Earlier versions of
        # this tab disabled the response Text widget when no rule was
        # selected, which made the editor look broken on first launch
        # (you couldn't type anything until a rule existed AND was
        # picked from the list). The Save button already warns when
        # nothing is selected, so leaving the fields editable is
        # safer and clearer.
        return

    def _on_field_change(self) -> None:
        self._update_preview()

    def _on_text_modified(self, _event=None) -> None:
        if self._response_text.edit_modified():
            self._update_preview()
            self._response_text.edit_modified(False)

    # ------------------------------------------------------------------
    # Live preview using a real dispatcher
    # ------------------------------------------------------------------
    def _update_preview(self) -> None:
        try:
            rule = self._collect_rule_from_editor()
        except Exception:
            return
        sample_player = (self._test_player_var.get() or "Steve").strip()
        sample_role = (self._test_role_var.get() or "admin").strip().lower()
        sample_msg = self._test_msg_var.get().strip()

        # If the user hasn't typed a test message, default to the trigger.
        if not sample_msg:
            sample_msg = rule.get("trigger") or "(type a test message)"

        # Build a one-rule dispatcher with a fresh cooldown table so the
        # preview never blocks itself.
        rules = [rule]
        d = ChatCommandDispatcher(lambda: rules)
        cmds = d.dispatch(sample_player, sample_role, sample_msg)

        # Validation result
        errors = validate_rule(rule)

        lines: list[str] = []
        if errors:
            lines.append("⚠  " + "  ⚠ ".join(errors))
        roles = rule.get("roles") or []
        role_label = ",".join(roles) if roles else "any role"
        lines.append(
            f"When [{role_label}] says: {rule.get('trigger') or '(trigger)'}")
        cd = rule.get("cooldown_secs", 0)
        if cd:
            lines.append(f"Cooldown: {cd}s per player")
        lines.append("")
        lines.append(f"Test:   {sample_player}({sample_role}) → {sample_msg}")
        if cmds:
            lines.append("Result: FIRES")
            for c in cmds:
                lines.append(f"  → {c}")
        else:
            # Explain why
            args_str = ""
            from core.custom_commands import _extract_args
            extracted = _extract_args(sample_msg, rule.get("trigger") or "")
            if extracted is None:
                why = "trigger doesn't match this message"
            elif not rule.get("enabled", True):
                why = "rule is disabled"
            elif (rule.get("roles") and sample_role
                  not in [r.lower() for r in rule["roles"]]):
                why = f"role '{sample_role}' not in allowed list"
            elif rule.get("response", "").strip() == "":
                why = "response is empty"
            else:
                why = "response produced no commands (missing arguments?)"
            lines.append(f"Result: NO COMMANDS — {why}")
        try:
            colour = Theme.RED_DIM if errors else Theme.AMBER
            self._preview_label.configure(
                text="\n".join(lines), fg=colour)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # CRUD actions
    # ------------------------------------------------------------------
    def _on_add(self) -> None:
        rules = self._load_rules()
        rules.append(make_empty_rule())
        self._save_rules(rules)
        self._selected_index = len(rules) - 1
        self._refresh_list()
        self._load_rule_into_editor(rules[-1])
        self._set_editor_enabled(True)
        self._listbox.selection_clear(0, tk.END)
        self._listbox.selection_set(self._selected_index)
        self._listbox.see(self._selected_index)

    def _on_duplicate(self) -> None:
        if self._selected_index is None:
            self._app._notify("Select a rule to duplicate.", level="warn")
            return
        rules = self._load_rules()
        if self._selected_index >= len(rules):
            return
        dup = copy.deepcopy(rules[self._selected_index])
        dup["trigger"] = (dup.get("trigger", "") + "_copy").strip()
        rules.insert(self._selected_index + 1, dup)
        self._save_rules(rules)
        self._selected_index += 1
        self._refresh_list()

    def _on_delete(self) -> None:
        if self._selected_index is None:
            self._app._notify("Select a rule to delete.", level="warn")
            return
        rules = self._load_rules()
        if self._selected_index >= len(rules):
            return
        trigger = rules[self._selected_index].get("trigger") or "(unnamed)"
        if not messagebox.askyesno(
                "Delete Rule",
                f"Delete rule for trigger '{trigger}'?",
                parent=self._parent):
            return
        rules.pop(self._selected_index)
        self._save_rules(rules)
        if self._selected_index >= len(rules):
            self._selected_index = len(rules) - 1 if rules else None
        self._refresh_list()
        if self._selected_index is not None and rules:
            self._load_rule_into_editor(rules[self._selected_index])
        else:
            self._set_editor_enabled(False)

    def _on_save(self) -> None:
        if self._selected_index is None:
            self._app._notify("No rule selected.", level="warn")
            return
        rule = self._collect_rule_from_editor()
        errors = validate_rule(rule)
        if errors:
            self._app._notify(" | ".join(errors), level="error",
                              duration_ms=5000)
            return
        rules = self._load_rules()
        if self._selected_index < len(rules):
            rules[self._selected_index] = rule
        else:
            rules.append(rule)
        self._save_rules(rules)
        self._refresh_list()
        self._app._notify(
            f"Rule saved: {rule['trigger']}", level="success",
            duration_ms=1800)

    def _on_discard(self) -> None:
        if self._selected_index is None:
            return
        rules = self._load_rules()
        if self._selected_index < len(rules):
            self._load_rule_into_editor(rules[self._selected_index])

    # ------------------------------------------------------------------
    # Import / Export
    # ------------------------------------------------------------------
    def _on_export(self) -> None:
        rules = self._load_rules()
        if not rules:
            self._app._notify("No rules to export.", level="warn")
            return
        from core.custom_commands import export_rules_to_json
        path = filedialog.asksaveasfilename(
            title="Export rules",
            defaultextension=".json",
            initialfile="vserverman_rules.json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(export_rules_to_json(rules))
            self._app._notify(
                f"Exported {len(rules)} rule(s) to {os.path.basename(path)}",
                level="success")
        except OSError as e:
            self._app._notify(f"Export failed: {e}", level="error")

    def _on_import(self) -> None:
        path = filedialog.askopenfilename(
            title="Import rules",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        from core.custom_commands import import_rules_from_json
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = f.read()
            new_rules = import_rules_from_json(payload)
        except (OSError, ValueError) as e:
            self._app._notify(f"Import failed: {e}", level="error",
                              duration_ms=6000)
            return
        existing = self._load_rules()
        choice = messagebox.askyesnocancel(
            "Import rules",
            f"Loaded {len(new_rules)} rule(s) from "
            f"{os.path.basename(path)}.\n\n"
            f"Yes = merge with existing ({len(existing)} rules)\n"
            f"No  = replace all existing\n"
            f"Cancel = abort",
            parent=self._parent,
        )
        if choice is None:
            return
        if choice:
            merged = list(existing) + list(new_rules)
        else:
            merged = list(new_rules)
        self._save_rules(merged)
        self._refresh_list()
        self._app._notify(
            f"Imported {len(new_rules)} rule(s).",
            level="success", duration_ms=2500)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load_rules(self) -> list[dict]:
        from core.settings import load_custom_commands
        return load_custom_commands(self._app._settings)

    def _save_rules(self, rules: list[dict]) -> None:
        from core.settings import save_custom_commands, save_settings
        save_custom_commands(self._app._settings, rules)
        save_settings(self._app._settings)
