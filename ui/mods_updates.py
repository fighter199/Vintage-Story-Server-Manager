"""
ui/mods_updates.py — The mod update picker dialog and the bulk update
runner behind MODS → Check Updates. Moved from ui/tab_mods.py unchanged.
"""
from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING
import tkinter as tk
from tkinter import ttk

from core.utils import clean_mod_filename
from .theme import Theme
from .widgets import (TermButton, ScrollableFrame, collapsible_section)

if TYPE_CHECKING:
    pass  # for type hints only


# Mod update picker (added by apply_mod_update_picker_patch.py)
# ──────────────────────────────────────────────────────────────────────
# Replaces the old all-or-nothing messagebox flow in _show_update_report.
# Lets the user tick exactly which outdated mods to update, with smart
# defaults: server/universal pre-ticked, client-only un-ticked.
#
# Implementation notes:
#   * Uses a Tk Toplevel rather than messagebox so we can pack arbitrary
#     widgets (checkboxes, badges, scrollable lists).
#   * Keeps the dialog modal via grab_set() + wait_window so the caller
#     blocks until the user picks something or cancels — matches the
#     blocking shape of the messagebox call it replaces.
#   * Hands the chosen subset directly to _bulk_update_worker (the
#     download loop), bypassing the old _bulk_update front-door which
#     had its own client-side re-prompt that's now redundant.
#   * Up-to-date / errored / no-modid sections are rendered as
#     collapsible read-only blocks for users who want to see the full
#     report without scrolling past dozens of fine mods.
class _ModUpdatePickerDialog(tk.Toplevel):
    """Modal dialog for picking which outdated mods to update.

    Construct, show, and wait_window in one go. After it closes, read
    .selected_reports — a list of report dicts (same shape that
    _bulk_update_worker expects), or [] if the user cancelled.
    """

    def __init__(self, app, outdated, current, errors, no_id,
                 no_compat=None):
        super().__init__(app)
        self._app = app
        self._outdated = outdated
        self._no_compat = no_compat or []
        # Cancellation sentinel — only set to True on the explicit
        # "Update selected" button. Closing the window via [X] or
        # Cancel leaves selected_reports empty.
        self.selected_reports: list = []
        # Bulk-update state — owned here so the in-dialog progress
        # widgets and the runner share it. _BulkUpdateRunner writes
        # to these via after-callbacks; we only read them.
        self._runner = None
        self._cancel_requested = False
        # Widgets we hide once the run starts, kept on self so we
        # can remove them with .pack_forget().
        self._picker_widgets: list = []
        self._progress_panel = None

        self.title("Mod Update Report")
        self.configure(bg=Theme.BG_PANEL)
        # Make the dialog modal relative to the main app window.
        try:
            self.transient(app)
        except tk.TclError:
            pass
        try:
            self.grab_set()
        except tk.TclError:
            pass
        # Sensible default size; user can resize.
        self.geometry("720x600")
        self.minsize(560, 400)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui(current, errors, no_id)
        self._refresh_count_label()

        # Centre over the parent window once geometry is realised.
        self.after_idle(self._centre_over_parent)

    # ── Layout ───────────────────────────────────────────────────────
    def _build_ui(self, current, errors, no_id):
        # `outer` is the mount point both for the picker widgets and
        # for the progress panel that replaces them on confirm. We
        # keep a reference on self so _enter_progress_mode can attach
        # the panel to the same parent.
        outer = tk.Frame(self, bg=Theme.BG_PANEL)
        outer.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)
        self._progress_outer = outer

        # Header summary line
        n_out = len(self._outdated)
        n_cur = len(current)
        n_err = len(errors)
        n_no = len(no_id)
        header_txt = (
            f"{n_out} outdated  ·  {n_cur} up-to-date"
            f"  ·  {n_err} error{'s' if n_err != 1 else ''}"
            f"  ·  {n_no} unreadable"
        )
        header_label = tk.Label(outer, text=header_txt,
                                fg=Theme.AMBER, bg=Theme.BG_PANEL,
                                font=self._app.F_HDR, anchor=tk.W)
        header_label.pack(fill=tk.X, pady=(0, 4))
        self._picker_widgets.append(header_label)

        help_label = tk.Label(
            outer,
            text=("Tick the outdated mods you want to update, then "
                  "click Update. Client-only mods are off by default."),
            fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
            font=self._app.F_SMALL, anchor=tk.W, justify=tk.LEFT,
            wraplength=680,
        )
        help_label.pack(fill=tk.X, pady=(0, 8))
        self._picker_widgets.append(help_label)

        # Quick-select toolbar
        bar = tk.Frame(outer, bg=Theme.BG_PANEL)
        bar.pack(fill=tk.X, pady=(0, 6))
        self._picker_widgets.append(bar)
        TermButton(bar, "Select all",   self._select_all,
                   variant="amber", font_spec=self._app.F_SMALL,
                   padx=6, pady=2).pack(side=tk.LEFT, padx=(0, 4))
        TermButton(bar, "None",         self._select_none,
                   variant="amber", font_spec=self._app.F_SMALL,
                   padx=6, pady=2).pack(side=tk.LEFT, padx=(0, 4))
        TermButton(bar, "Server + Universal only",
                   self._select_server_universal,
                   variant="amber", font_spec=self._app.F_SMALL,
                   padx=6, pady=2).pack(side=tk.LEFT, padx=(0, 4))
        TermButton(bar, "Client only",  self._select_client,
                   variant="amber", font_spec=self._app.F_SMALL,
                   padx=6, pady=2).pack(side=tk.LEFT)

        # Scrollable list of outdated mods with checkboxes
        list_frame = tk.Frame(outer, bg=Theme.BG_PANEL,
                              highlightbackground=Theme.BORDER,
                              highlightthickness=1)
        list_frame.pack(fill=tk.BOTH, expand=True)
        self._picker_widgets.append(list_frame)
        sf = ScrollableFrame(list_frame, bg=Theme.BG_INPUT)
        sf.pack(fill=tk.BOTH, expand=True)
        body = sf.body

        # One BooleanVar per outdated mod, defaulted by side.
        self._vars: list[tuple[tk.BooleanVar, dict]] = []
        if not self._outdated:
            tk.Label(body,
                     text="No outdated mods. You're all caught up.",
                     fg=Theme.AMBER_DIM, bg=Theme.BG_INPUT,
                     font=self._app.F_NORMAL,
                     padx=12, pady=12).pack(anchor=tk.W)
        else:
            for r in self._outdated:
                self._add_row(body, r)

        # Read-only collapsible sections for the other categories
        no_compat = self._no_compat
        if current or errors or no_id or no_compat:
            ro_frame = tk.Frame(outer, bg=Theme.BG_PANEL)
            ro_frame.pack(fill=tk.X, pady=(8, 0))
            self._picker_widgets.append(ro_frame)
            if current:
                self._add_collapsible(
                    ro_frame, f"Up-to-date ({len(current)})",
                    [
                        f"{r['info'].get('name')} ({r.get('local_ver')})"
                        for r in current
                    ])
            if no_compat:
                self._add_collapsible(
                    ro_frame,
                    f"No release for selected game version ({len(no_compat)})",
                    [r['info'].get('name') for r in no_compat])
            if errors:
                self._add_collapsible(
                    ro_frame, f"Failed to check ({len(errors)})",
                    [
                        f"{r['info'].get('name')}: {r.get('error')}"
                        for r in errors
                    ])
            if no_id:
                self._add_collapsible(
                    ro_frame, f"No modid, skipped ({len(no_id)})",
                    [r['info'].get('name') for r in no_id])

        # Footer: Cancel + Update buttons
        footer = tk.Frame(outer, bg=Theme.BG_PANEL)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._picker_widgets.append(footer)
        TermButton(footer, "Cancel", self._on_cancel,
                   variant="stop", font_spec=self._app.F_BTN,
                   padx=14, pady=6).pack(side=tk.LEFT)
        # The "Update" button label updates live as the user toggles.
        self._update_btn = TermButton(
            footer, "Update 0 selected mod(s)", self._on_confirm,
            variant="start", font_spec=self._app.F_BTN,
            padx=14, pady=6)
        self._update_btn.pack(side=tk.RIGHT)

    def _add_row(self, parent, report):
        """Add one checkbox row for a single outdated mod."""
        info = report.get("info") or {}
        detail = report.get("detail") or {}
        side = self._app._normalize_side(detail.get("side"))
        # Default: server/universal checked, client and unknown OFF.
        default_on = side in ("server", "universal")
        var = tk.BooleanVar(value=default_on)
        # Trace toggles → live-update the count label.
        var.trace_add("write", lambda *_: self._refresh_count_label())
        self._vars.append((var, report))

        row = tk.Frame(parent, bg=Theme.BG_INPUT)
        row.pack(fill=tk.X, padx=6, pady=2, anchor=tk.W)

        cb = tk.Checkbutton(
            row, variable=var,
            fg=Theme.AMBER, activeforeground=Theme.AMBER_GLOW,
            bg=Theme.BG_INPUT, activebackground=Theme.BG_INPUT,
            selectcolor=Theme.BG_INPUT,
            highlightthickness=0, bd=0,
        )
        cb.pack(side=tk.LEFT)

        # Side badge — colour-coded.
        badge_tag, badge_text = self._app._side_badge(side)
        # # [picker-dialog-fixes-v1] Theme.OK / Theme.ERR don't exist; use
        # the canonical GREEN/RED constants used everywhere else.
        badge_fg = {
            "side_srv":  Theme.GREEN,
            "side_both": Theme.AMBER,
            "side_cli":  Theme.RED,
        }.get(badge_tag, Theme.AMBER_DIM)
        tk.Label(row, text=f"[{badge_text}]",
                 fg=badge_fg, bg=Theme.BG_INPUT,
                 font=self._app.F_SMALL).pack(
            side=tk.LEFT, padx=(2, 6))

        # Name + version transition
        name = info.get("name") or "(unnamed)"
        local_v = report.get("local_ver") or "?"
        latest_v = report.get("latest_ver") or "?"
        text = f"{name}    {local_v}  →  {latest_v}"
        tk.Label(row, text=text,
                 fg=Theme.AMBER, bg=Theme.BG_INPUT,
                 font=self._app.F_NORMAL, anchor=tk.W).pack(
            side=tk.LEFT, fill=tk.X, expand=True)

    def _add_collapsible(self, parent, title, items):
        """Render a list of strings inside a collapsible block."""
        # # [picker-dialog-fixes-v1] collapsible_section returns (header, body)
        # and uses `start_collapsed=`, not `expanded=`. The buckets in
        # this dialog are read-only summaries, so collapse by default.
        _hdr, body = collapsible_section(
            parent, title, font_spec=self._app.F_SMALL,
            start_collapsed=True)
        for line in items:
            tk.Label(body, text=f"  • {line}",
                     fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
                     font=self._app.F_SMALL,
                     anchor=tk.W, justify=tk.LEFT).pack(
                anchor=tk.W, padx=4, pady=1)

    # ── Selection helpers ────────────────────────────────────────────
    def _select_all(self):
        for var, _r in self._vars:
            var.set(True)

    def _select_none(self):
        for var, _r in self._vars:
            var.set(False)

    def _select_server_universal(self):
        for var, r in self._vars:
            side = self._app._normalize_side(
                (r.get("detail") or {}).get("side"))
            var.set(side in ("server", "universal"))

    def _select_client(self):
        for var, r in self._vars:
            side = self._app._normalize_side(
                (r.get("detail") or {}).get("side"))
            var.set(side == "client")

    # ── Live count label ─────────────────────────────────────────────
    def _refresh_count_label(self):
        n = sum(1 for var, _r in self._vars if var.get())
        try:
            self._update_btn.configure(
                text=f"Update {n} selected mod(s)")
            # Disable when nothing is ticked. TermButton may not expose
            # a plain `state` directly — we fall back to a no-op if so.
            try:
                self._update_btn.configure(
                    state=(tk.NORMAL if n > 0 else tk.DISABLED))
            except tk.TclError:
                pass
        except (AttributeError, tk.TclError):
            pass

    # ── Lifecycle ────────────────────────────────────────────────────
    def _centre_over_parent(self):
        try:
            self.update_idletasks()
            pw = self._app.winfo_width()
            ph = self._app.winfo_height()
            px = self._app.winfo_rootx()
            py = self._app.winfo_rooty()
            w = self.winfo_width()
            h = self.winfo_height()
            x = px + max(0, (pw - w) // 2)
            y = py + max(0, (ph - h) // 3)
            self.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass

    def _on_confirm(self):
        # Capture which reports the user picked, then swap the
        # picker UI for a progress panel and kick off the runner.
        self.selected_reports = [
            r for var, r in self._vars if var.get()]
        if not self.selected_reports:
            return  # Update button shouldn't be reachable, but be safe.
        # Hide the picker widgets and show a progress panel in the
        # same dialog. We don't destroy() yet — the runner needs a
        # live window to schedule after-callbacks against.
        self._enter_progress_mode()
        self._runner = _BulkUpdateRunner(self, self.selected_reports)
        self._runner.start()

    def _on_close(self):
        """WM_DELETE_WINDOW handler.

        While a run is active, the X button requests a cancel rather
        than tearing down the dialog mid-download (which would orphan
        the worker thread's after-callbacks). When idle, behave like
        Cancel.
        """
        if self._runner is not None and self._runner.is_active():
            self._request_cancel()
            return
        self._on_cancel()

    def _request_cancel(self):
        """Mark the runner as cancelled. The runner picks this up
        between files (and inside the streaming download via the
        existing cancel_flag hook in moddb.download_file)."""
        self._cancel_requested = True
        if hasattr(self, "_progress_status"):
            try:
                self._progress_status.configure(
                    text="Cancelling… finishing current file.")
            except tk.TclError:
                pass
        if hasattr(self, "_progress_cancel_btn"):
            try:
                self._progress_cancel_btn.configure(
                    state=tk.DISABLED)
            except tk.TclError:
                pass

    def _enter_progress_mode(self):
        """Hide the picker widgets and reveal a progress panel in
        their place. Called from _on_confirm."""
        for w in self._picker_widgets:
            try:
                w.pack_forget()
            except tk.TclError:
                pass

        outer = self._progress_outer
        panel = tk.Frame(outer, bg=Theme.BG_PANEL)
        panel.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)
        self._progress_panel = panel

        tk.Label(panel,
                 text=f"Updating {len(self.selected_reports)} mod(s)…",
                 fg=Theme.AMBER, bg=Theme.BG_PANEL,
                 font=self._app.F_HDR, anchor=tk.W).pack(
            fill=tk.X, pady=(0, 6))

        # Per-file label
        self._progress_status = tk.Label(
            panel, text="Starting…",
            fg=Theme.AMBER, bg=Theme.BG_PANEL,
            font=self._app.F_NORMAL, anchor=tk.W,
            justify=tk.LEFT, wraplength=680)
        self._progress_status.pack(fill=tk.X, pady=(0, 8))

        # Bytes-of-current-file progress bar (reuses the same look as
        # the BROWSE-tab download bar).
        bar_bg = tk.Frame(panel, bg=Theme.BORDER, height=10,
                          highlightthickness=0, bd=0)
        bar_bg.pack(fill=tk.X, pady=(0, 4))
        bar_inner = tk.Frame(bar_bg, bg=Theme.BG_INPUT,
                             highlightthickness=0, bd=0)
        bar_inner.place(relx=0, rely=0, relwidth=1, relheight=1,
                        x=1, y=1, width=-2, height=-2)
        self._progress_fill = tk.Frame(
            bar_inner,
            bg=getattr(Theme, "GREEN", Theme.AMBER),
            highlightthickness=0, bd=0)
        self._progress_fill.place(relx=0, rely=0, relwidth=0,
                                   relheight=1)

        # Overall counter
        self._progress_counter = tk.Label(
            panel, text="0 / {}".format(len(self.selected_reports)),
            fg=Theme.AMBER_DIM, bg=Theme.BG_PANEL,
            font=self._app.F_SMALL, anchor=tk.W)
        self._progress_counter.pack(fill=tk.X, pady=(2, 8))

        # Inline log of completed files — shows a tail of progress
        # so the user can see what already finished.
        log_frame = tk.Frame(panel, bg=Theme.BG_INPUT,
                             highlightbackground=Theme.BORDER,
                             highlightthickness=1)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        self._progress_log = tk.Text(
            log_frame, bg=Theme.BG_INPUT, fg=Theme.AMBER,
            font=self._app.F_SMALL, wrap=tk.NONE, state=tk.DISABLED,
            relief=tk.FLAT, bd=0, height=8)
        self._progress_log.pack(side=tk.LEFT, fill=tk.BOTH,
                                 expand=True)
        try:
            sb = ttk.Scrollbar(log_frame, orient=tk.VERTICAL,
                               command=self._progress_log.yview)
            sb.pack(side=tk.RIGHT, fill=tk.Y)
            self._progress_log.configure(yscrollcommand=sb.set)
        except tk.TclError:
            pass

        # Cancel button (renamed Close after completion)
        footer = tk.Frame(panel, bg=Theme.BG_PANEL)
        footer.pack(fill=tk.X)
        self._progress_cancel_btn = TermButton(
            footer, "Cancel", self._request_cancel,
            variant="stop", font_spec=self._app.F_BTN,
            padx=14, pady=6)
        self._progress_cancel_btn.pack(side=tk.RIGHT)

    def _append_progress_log(self, text, level="info"):
        """Append a single line to the in-dialog progress log."""
        try:
            self._progress_log.configure(state=tk.NORMAL)
            self._progress_log.insert(tk.END, text + "\n")
            self._progress_log.see(tk.END)
            self._progress_log.configure(state=tk.DISABLED)
        except (tk.TclError, AttributeError):
            pass

    def _set_progress_status(self, text):
        try:
            self._progress_status.configure(text=text)
        except (tk.TclError, AttributeError):
            pass

    def _set_progress_fill(self, fraction):
        """Set the per-file progress bar to the given 0..1 fraction."""
        f = max(0.0, min(1.0, float(fraction)))
        try:
            self._progress_fill.place_configure(relwidth=f)
        except (tk.TclError, AttributeError):
            pass

    def _set_progress_counter(self, done_n, total_n):
        try:
            self._progress_counter.configure(
                text=f"{done_n} / {total_n}")
        except (tk.TclError, AttributeError):
            pass

    def _show_summary(self, successes, failures, cancelled):
        """Called by the runner when the bulk update finishes.

        Replaces the Cancel button with a Close button and shows a
        terminal summary line. The dialog stays open until the user
        clicks Close — that way the in-dialog log is readable.
        """
        if cancelled:
            line = (f"Cancelled — {successes} succeeded, "
                    f"{len(failures)} failed before cancel.")
            colour = Theme.AMBER
        elif failures:
            line = (f"Done — {successes} updated, "
                    f"{len(failures)} failed.")
            colour = Theme.AMBER
        else:
            line = f"Done — {successes} updated."
            colour = getattr(Theme, "GREEN", Theme.AMBER)
        self._set_progress_status(line)
        try:
            self._progress_status.configure(fg=colour)
        except tk.TclError:
            pass
        try:
            self._progress_cancel_btn.configure(
                text="Close",
                state=tk.NORMAL,
                command=self._on_cancel)
        except tk.TclError:
            pass

    def _on_cancel(self):
        self.selected_reports = []
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


# ──────────────────────────────────────────────────────────────────────
# Bulk update runner (added by apply_mod_updater_polish_v2_patch.py)
# ──────────────────────────────────────────────────────────────────────
class _BulkUpdateRunner:
    """Runs the bulk update in a worker thread, with cancel + per-file
    progress, and reports back to the picker dialog via after().

    Lifecycle:
        runner = _BulkUpdateRunner(dialog, reports)
        runner.start()                # spawns thread, returns immediately
        # … runner schedules dialog._set_progress_*() / append calls …
        # … on completion, runner schedules dialog._show_summary(…)
        runner.is_active() → True until the summary has been shown
    """

    def __init__(self, dialog, reports):
        self._dlg = dialog
        self._app = dialog._app
        self._reports = list(reports)
        self._thread = None
        self._done = False
        self._mods_dir = self._app.mods_folder_var.get()

    def is_active(self):
        return self._thread is not None and not self._done

    def start(self):
        self._thread = threading.Thread(
            target=self._run, daemon=True)
        self._thread.start()

    # The actual work — runs on the worker thread. Every UI mutation
    # is marshalled back to the Tk thread via app.after(0, …).
    def _run(self):
        successes = 0
        failures = []
        cancelled = False
        total = len(self._reports)
        for i, r in enumerate(self._reports, 1):
            # Check for cancel between files. The per-byte cancel hook
            # below also picks this up mid-download.
            if self._dlg._cancel_requested:
                cancelled = True
                break

            info = r["info"]
            latest = r.get("latest") or {}
            name = info.get("name") or "(unnamed)"

            self._app.after(
                0, self._dlg._set_progress_status,
                f"[{i}/{total}] Updating {name}…")
            self._app.after(
                0, self._dlg._set_progress_counter, i - 1, total)
            self._app.after(0, self._dlg._set_progress_fill, 0.0)

            url = latest.get("mainfile") or ""
            if url and not url.startswith("http"):
                url = self._app.moddb.SITE_BASE + "/" + url.lstrip("/")
            if not url or not self._app.moddb.is_trusted_url(url):
                failures.append((name, "no/untrusted URL"))
                self._append_log(f"✗ {name}: no/untrusted URL", "error")
                continue
            try:
                expected_size = int(latest.get("filesize") or 0) or None
            except (ValueError, TypeError):
                expected_size = None
            filename = clean_mod_filename(
                url=url,
                declared=latest.get("filename"),
                modid=info.get("modid"),
                version=latest.get("modversion") or latest.get("version"),
                name=info.get("name"),
            )
            dest = os.path.join(self._mods_dir, filename)
            old_path = info.get("path")

            def progress_cb(got, t, _name=name, _expected=expected_size):
                # Use the API's expected_size when the server omits
                # Content-Length. The runner doesn't care which one
                # is right, only that the bar moves.
                t_eff = t if t and t > 0 else (_expected or 0)
                if t_eff > 0:
                    frac = got / t_eff
                else:
                    frac = 0.0
                self._app.after(
                    0, self._dlg._set_progress_fill, frac)

            def cancel_cb():
                return self._dlg._cancel_requested

            try:
                self._app.moddb.download_file(
                    url, dest,
                    progress_cb=progress_cb,
                    cancel_flag=cancel_cb,
                    expected_size=expected_size)
                # Old file cleanup if we moved to a new filename.
                if (old_path and os.path.exists(old_path)
                        and os.path.abspath(old_path)
                            != os.path.abspath(dest)):
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass
                successes += 1
                self._append_log(f"✓ {name}", "success")
            except Exception as e:
                msg = str(e)
                # The cancel signal surfaces as a RuntimeError from
                # moddb.download_file — recognise it so we don't
                # mis-classify as a failure.
                if "cancel" in msg.lower() and self._dlg._cancel_requested:
                    cancelled = True
                    break
                failures.append((name, msg))
                self._append_log(f"✗ {name}: {msg}", "error")

        # Final UI update on the main thread.
        self._app.after(
            0, self._finish, successes, failures, cancelled)

    def _append_log(self, text, level):
        self._app.after(0, self._dlg._append_progress_log, text, level)

    def _finish(self, successes, failures, cancelled):
        # Refresh the installed-mods list so the user sees the new
        # files immediately when they look at the tab.
        try:
            self._app.load_mods()
        except Exception:
            pass
        # Status bar + toast — same shape the old _finalize_bulk_update
        # produced, so console listeners and notification queues see
        # the familiar messages.
        if cancelled:
            summary = (f"{successes} updated, "
                       f"{len(failures)} failed (cancelled)")
            level = "warn"
        elif failures:
            summary = f"{successes} updated, {len(failures)} failed"
            level = "warn"
            self._app.append_console("Bulk update failures:", "warn")
            for name, err in failures:
                self._app.append_console(f"  • {name}: {err}", "error")
        else:
            summary = f"{successes} updated."
            level = "success"
        self._app._set_moddb_status(summary)
        self._app._notify(f"Bulk update: {summary}", level=level)
        # Hand off to the dialog to show its terminal summary and
        # swap Cancel → Close.
        self._dlg._show_summary(successes, failures, cancelled)
        self._done = True
