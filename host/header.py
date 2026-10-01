"""
host/header.py — The collapsible header: title, setup warnings, hotkeys, and the
GitHub / update-check row.

Methods of ServerManagerApp (VSSM.py), moved here unchanged.
"""
from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import messagebox

# ── Local packages ─────────────────────────────────────────────────────
from core.constants import (APP_VERSION, LOG)
from core.updates import (PROJECT_URL, UpdateCheckError, fetch_latest_release,
                          is_newer)
from core.settings import (save_settings)
from ui.theme import (Theme)
from ui.widgets import (TermButton, TermCheckbutton, flow_row, flow_children, auto_wrap)


class HeaderMixin:

    # ------------------------------------------------------------------
    # Collapsible top header
    # ------------------------------------------------------------------
    def _build_header_toolbar(self, parent):
        """The strip that's always visible at the very top of the
        window. Carries the collapse toggle, the inline title, and a
        compact setup-warning indicator."""
        # Toggle button (▾ when expanded, ▸ when collapsed). Bound to
        # _toggle_header_collapsed so a click flips and persists state.
        self._header_toggle_btn = tk.Label(
            parent,
            text=("▸" if self._header_collapsed else "▾"),
            fg=Theme.AMBER_GLOW, bg=Theme.BG_DARK,
            font=self.F_HDR, cursor="hand2",
            padx=8, pady=2,
        )
        self._header_toggle_btn.pack(side=tk.LEFT)
        self._header_toggle_btn.bind(
            "<Button-1>", lambda _e: self._toggle_header_collapsed())

        # Inline compact title. Whole-line clickable so users don't
        # have to aim at the small ▾/▸ glyph.
        title = tk.Label(
            parent,
            text=f"VSSM v{APP_VERSION} — Vintage Story Server Manager",
            fg=Theme.AMBER, bg=Theme.BG_DARK,
            font=self.F_HDR, cursor="hand2",
            padx=4, pady=2,
        )
        self._header_title = title          # ProfileBar adds the profile
        title.pack(side=tk.LEFT, padx=(0, 8))
        title.bind(
            "<Button-1>", lambda _e: self._toggle_header_collapsed())

        # Compact warning indicator on the right. Hidden when there
        # are no setup warnings; shows a single-line summary like
        # "⚠ 2 setup issues" when there are. Clicking it expands the
        # header so the user can read the full warning text.
        self._header_warn_label = tk.Label(
            parent, text="", fg=Theme.RED, bg=Theme.BG_DARK,
            font=self.F_SMALL, cursor="hand2", padx=8,
        )
        self._header_warn_label.pack(side=tk.RIGHT)
        self._header_warn_label.bind(
            "<Button-1>",
            lambda _e: self._set_header_collapsed(False))


    def _build_header_body(self, parent):
        """The original 3-column header layout — title, version,
        hotkeys cheat sheet, full setup-warning text. Lives inside
        a frame that pack_forget() can hide."""
        header = tk.Frame(parent, bg=Theme.BG_DARK)
        header.pack(fill=tk.X)
        header.columnconfigure(0, weight=1, uniform="hdr")
        header.columnconfigure(1, weight=2, uniform="hdr")
        header.columnconfigure(2, weight=1, uniform="hdr")

        left_col = tk.Frame(header, bg=Theme.BG_DARK)
        left_col.grid(row=0, column=0, rowspan=2, sticky="new", padx=(0, 8))
        self._build_project_row(left_col)
        # setup_warning_var was created in _build_ui (before this body
        # was built) so the toolbar's compact indicator can bind to it.
        self.setup_warning_label = tk.Label(
            left_col, textvariable=self.setup_warning_var,
            fg=Theme.RED, bg=Theme.BG_DARK,
            font=self.F_SMALL, justify=tk.LEFT, anchor="nw", wraplength=320)
        self.setup_warning_label.pack(anchor="nw")

        center_col = tk.Frame(header, bg=Theme.BG_DARK)
        center_col.grid(row=0, column=1, rowspan=2, sticky="n")
        self.title_label = tk.Label(
            center_col, text="⛏  VINTAGE STORY SERVER MANAGER  ⛏",
            fg=Theme.AMBER_GLOW, bg=Theme.BG_DARK, font=self.F_TITLE)
        self.title_label.pack()
        tk.Label(center_col, text=f"[ VSERVERMAN v{APP_VERSION} ]",
                 fg=Theme.AMBER_DIM, bg=Theme.BG_DARK,
                 font=self.F_SUB).pack(pady=(2, 0))

        right_col = tk.Frame(header, bg=Theme.BG_DARK)
        right_col.grid(row=0, column=2, rowspan=2, sticky="ne", padx=(8, 0))
        tk.Label(right_col, text="HOTKEYS", fg=Theme.AMBER_GLOW,
                 bg=Theme.BG_DARK, font=self.F_SMALL).pack(anchor="ne")
        tk.Label(right_col,
                 text="Ctrl+L        clear console\n"
                      "Ctrl+Enter    send command\n"
                      "↑ / ↓         history\n"
                      "Right-click   copy / player actions\n"
                      "Ctrl + =      larger text\n"
                      "Ctrl + −      smaller text\n"
                      "Ctrl + 0      reset text size",
                 fg=Theme.AMBER_DIM, bg=Theme.BG_DARK,
                 font=self.F_SMALL, justify=tk.RIGHT).pack(anchor="ne", pady=(2, 0))


    # ------------------------------------------------------------------
    # Project page + update check (header, far left)
    # ------------------------------------------------------------------
    def _build_project_row(self, parent):
        row = tk.Frame(parent, bg=Theme.BG_DARK)
        row.pack(fill=tk.X, pady=(0, 4))
        TermButton(row, "⌂ GitHub", self._open_project_page, variant="amber",
                   font_spec=self.F_SMALL, padx=8, pady=2).pack(side=tk.LEFT)
        self._update_btn = TermButton(
            row, "⟳ Check for updates", lambda: self._check_for_updates(manual=True),
            variant="amber", font_spec=self.F_SMALL, padx=8, pady=2)
        self._update_btn.pack(side=tk.LEFT, padx=(6, 0))
        flow_children(row)
        self.auto_update_check_var = tk.BooleanVar(
            value=bool(self._settings.get("auto_check_updates", False)))
        chk = TermCheckbutton(parent, "Check for updates on startup",
                              self.auto_update_check_var,
                              font_spec=self.F_SMALL,
                              command=self._on_auto_update_toggle)
        chk.configure(bg=Theme.BG_DARK, activebackground=Theme.BG_DARK)
        auto_wrap(chk).pack(fill=tk.X, pady=(0, 6))
        self._update_check_running = False


    def _open_project_page(self, url: str = PROJECT_URL):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as e:
            LOG.exception("webbrowser.open failed for %s", url)
            self._notify(f"Could not open browser: {e}", level="error")


    def _on_auto_update_toggle(self):
        self._settings["auto_check_updates"] = bool(self.auto_update_check_var.get())
        save_settings(self._settings)


    def _check_for_updates(self, manual: bool = False):
        """Ask GitHub for the latest release in the background. A manual
        check always reports back; the startup check only speaks up
        when there is something newer."""
        if self._update_check_running:
            return
        self._update_check_running = True
        if manual:
            self._notify("Checking for updates…", level="info")

        def work():
            info = error = None
            try:
                info = fetch_latest_release()
            except UpdateCheckError as e:
                error = str(e)
            except Exception as e:               # pragma: no cover
                LOG.exception("update check failed")
                error = f"{type(e).__name__}: {e}"
            try:
                self.after(0, self._on_update_checked, info, error, manual)
            except (RuntimeError, tk.TclError):
                pass                             # app closed meanwhile

        threading.Thread(target=work, daemon=True).start()


    def _on_update_checked(self, info, error, manual: bool):
        self._update_check_running = False
        if error:
            LOG.info("update check: %s", error)
            if manual:
                self._notify(f"Update check failed: {error}", level="error",
                             duration_ms=5000)
            return
        if not is_newer(info["version"]):
            if manual:
                self._notify(f"You're up to date (VSSM {APP_VERSION}).",
                             level="success")
            return
        version, url = info["version"], info["url"]
        self._update_btn.configure(text=f"⬆ GET VSSM {version}")
        self._update_btn._command = lambda: self._open_project_page(url)
        self.append_console(
            f"VSSM {version} is available (you have {APP_VERSION}): {url}",
            "success")
        if manual:
            if messagebox.askyesno(
                    "Update available",
                    f"VSSM {version} is available — you have {APP_VERSION}."
                    "\n\nOpen the download page?", parent=self):
                self._open_project_page(url)
        else:
            self._notify(f"VSSM {version} is available — see the header.",
                         level="success", duration_ms=6000)


    def _toggle_header_collapsed(self):
        """Flip the header's collapsed state and persist."""
        self._set_header_collapsed(not self._header_collapsed)


    def _set_header_collapsed(self, collapsed: bool, persist: bool = True):
        """Apply a specific collapsed state. Idempotent — re-applying
        the same state is a cheap no-op. persist=False shows/hides it
        for now without changing the saved preference."""
        collapsed = bool(collapsed)
        if collapsed == self._header_collapsed:
            return
        self._header_collapsed = collapsed
        # Update the toggle glyph
        try:
            self._header_toggle_btn.configure(
                text=("▸" if collapsed else "▾"))
        except (AttributeError, tk.TclError):
            pass
        # Show or hide the body.
        try:
            if collapsed:
                self._header_body.pack_forget()
            else:
                # Re-pack just above the separator. Using `before=` keeps
                # the relative order stable across multiple toggles.
                self._header_body.pack(
                    fill=tk.X, pady=(2, 8),
                    before=self._header_separator)
        except (AttributeError, tk.TclError):
            pass
        if not persist:
            return
        try:
            self._settings["header_collapsed"] = collapsed
            save_settings(self._settings)
        except Exception:
            LOG.exception("save header_collapsed failed")


    def _recompute_setup_warning(self):
        if not hasattr(self, "setup_warning_var"):
            return
        issues = []
        exe = (self.server_path_var.get() or "").strip()
        if not exe:
            issues.append("⚠ SERVER EXECUTABLE NOT SET")
        elif not os.path.isfile(exe):
            issues.append("⚠ EXECUTABLE PATH INVALID")
        mods = (self.mods_folder_var.get() or "").strip()
        if not mods:
            issues.append("⚠ MODS FOLDER NOT SET")
        elif not os.path.isdir(mods):
            issues.append("⚠ MODS FOLDER INVALID")
        new_text = "\n".join(issues)
        prev_text = ""
        try:
            prev_text = self.setup_warning_var.get() or ""
        except Exception:
            pass
        self.setup_warning_var.set(new_text)

        # Update the toolbar's compact indicator and auto-expand the
        # header when a NEW warning appears (so the user can't miss an
        # "executable not set" notice while the header is collapsed).
        # We only auto-expand when the warning text changed from empty
        # to non-empty — collapsing-after-acknowledging is respected.
        try:
            warn_lbl = getattr(self, "_header_warn_label", None)
            if warn_lbl is not None:
                if issues:
                    n = len(issues)
                    warn_lbl.configure(
                        text=f"⚠ {n} setup issue{'s' if n != 1 else ''}")
                else:
                    warn_lbl.configure(text="")
        except (AttributeError, tk.TclError):
            pass
        # Only for warnings that appear mid-session: while the window is
        # being built the executable path isn't loaded yet, so every
        # launch used to "discover" a warning, re-open the header and
        # save it as open. The toolbar badge covers startup issues.
        if new_text and not prev_text and getattr(self, "_startup_complete", False):
            try:
                self._set_header_collapsed(False, persist=False)
            except Exception:
                pass


    def _install_wrapping_row(self, container, widgets, spacing=8, pady_between=4):
        flow_row(container, widgets, spacing=spacing, pady_between=pady_between)
