"""
ui/settings_io.py — SETTINGS → Export settings… / Import settings…

Moves every profile, the app preferences and your own COMMANDS entries
to another install as one JSON file (see core/settings_transfer.py).
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from tkinter import filedialog, messagebox

from core.command_files import USER_FILE, read_command_file
from core.constants import LOG, script_dir
from core.profiles import active_profile_name
from core.settings import save_settings
from core.settings_transfer import (TransferError, clashing_profiles,
                                    export_bundle, import_preferences,
                                    import_profiles, merge_user_commands,
                                    read_bundle, write_bundle)


def _user_commands_path() -> str:
    return os.path.join(script_dir(), USER_FILE)


def export_settings(app) -> str | None:
    if app._profile_has_unsaved_changes() and messagebox.askyesno(
            "Export settings", "Some settings were changed but not saved. "
            "Save them first so the export includes them?", parent=app):
        app._save_profile_settings()
    try:
        user_cmds = read_command_file(_user_commands_path())
    except (OSError, ValueError) as e:
        LOG.warning("user commands not exported: %s", e)
        user_cmds = {}
    path = filedialog.asksaveasfilename(
        parent=app, title="Export VSSM settings", defaultextension=".json",
        filetypes=[("VSSM settings", "*.json")],
        initialfile=f"vssm-settings-{datetime.now():%Y%m%d}.json")
    if not path:
        return None
    try:
        write_bundle(path, export_bundle(app._settings, user_cmds))
    except OSError as e:
        messagebox.showerror("Export settings", f"Couldn't save: {e}", parent=app)
        return None
    n = len(app._settings.get("profiles") or {})
    app._notify(f"Exported {n} profile(s) to {os.path.basename(path)}",
                level="success")
    app.append_console(f"Settings exported to {path}", "system")
    return path


def import_settings(app, path: str | None = None) -> bool:
    bar = getattr(app, "_profile_bar", None)
    if bar is not None and not bar._check_idle("Import settings"):
        return False
    if path is None:
        path = filedialog.askopenfilename(
            parent=app, title="Import VSSM settings",
            filetypes=[("VSSM settings", "*.json"), ("All files", "*.*")])
        if not path:
            return False
    try:
        bundle = read_bundle(path)
    except TransferError as e:
        messagebox.showerror("Import settings", str(e), parent=app)
        return False

    settings = app._settings
    names = list(bundle["profiles"])
    clashes = clashing_profiles(settings, bundle)
    replace = False
    if clashes:
        choice = messagebox.askyesnocancel(
            "Import settings",
            f"The file has {len(names)} profile(s). These already exist "
            f"here: {', '.join(clashes)}.\n\nYes — replace them with the "
            "imported ones\nNo — keep yours and import theirs as copies",
            parent=app)
        if choice is None:
            return False
        replace = choice
    elif not messagebox.askokcancel(
            "Import settings",
            f"Import {len(names)} profile(s): {', '.join(names[:8])}"
            + ("…" if len(names) > 8 else "") + "?", parent=app):
        return False
    prefs = bool(bundle.get("preferences")) and messagebox.askyesno(
        "Import settings", "Also use the file's app preferences (theme, "
        "text size, crash-loop limits, player poll, update check)?", parent=app)
    theirs = bundle.get("user_commands") or {}
    n_cmds = sum(len(v) for v in theirs.values() if isinstance(v, dict))
    cmds = n_cmds > 0 and messagebox.askyesno(
        "Import settings", f"Also add the file's {n_cmds} custom COMMANDS "
        f"entries to your {USER_FILE}? (Same-named entries are replaced; "
        "your current file is kept as a .bak.)", parent=app)

    if app._profile_has_unsaved_changes():
        app._save_profile_settings()
    active = active_profile_name(settings)
    done = import_profiles(settings, bundle, replace)
    applied = import_preferences(settings, bundle) if prefs else []
    save_settings(settings)

    if cmds:
        _merge_commands_file(app, theirs)
    # Reload whatever the import touched.
    if bar is not None:
        if any(target == active for _n, target, how in done if how == "replaced"):
            bar._activate(active)
        else:
            bar.refresh()
    if applied:
        _apply_preferences(app, applied)

    renamed = [f"{n} → {t}" for n, t, how in done if how == "renamed"]
    summary = f"Imported {len(done)} profile(s)"
    if renamed:
        summary += f" ({'; '.join(renamed)})"
    if applied:
        summary += ", app preferences"
    if cmds:
        summary += f", {n_cmds} custom command(s)"
    app._notify(summary + ".", level="success", duration_ms=6000)
    app.append_console(f"Settings imported from {path}: {summary}.", "system")
    return True


def _merge_commands_file(app, theirs: dict) -> None:
    path = _user_commands_path()
    try:
        mine = read_command_file(path)
        if os.path.isfile(path):
            shutil.copy2(path, path + ".bak")
        merged, _n = merge_user_commands(mine, theirs)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except (OSError, ValueError) as e:
        LOG.exception("merging imported commands failed")
        app._notify(f"Custom commands not imported: {e}", level="error")
        return
    app._reload_commands_json()


def _apply_preferences(app, applied: list[str]) -> None:
    s = app._settings
    try:
        if "theme_preset" in applied or "custom_theme_colors" in applied:
            preset = s.get("theme_preset", "amber")
            app.theme_preset_var.set(preset)
            app._apply_theme(preset)
        if "ui_scale_override" in applied:
            app._set_text_scale(float(s["ui_scale_override"]))
        if "crash_limit" in applied:
            app.CRASH_LIMIT = int(s["crash_limit"])
            app._crash_limit_var.set(str(s["crash_limit"]))
        if "crash_window_secs" in applied:
            app.CRASH_WINDOW_SECS = int(s["crash_window_secs"])
            app._crash_window_var.set(str(s["crash_window_secs"]))
        if "player_count_poll_secs" in applied:
            app._player_poll_var.set(str(s["player_count_poll_secs"]))
            app._reschedule_player_count_poll()
        if "auto_check_updates" in applied:
            app.auto_update_check_var.set(bool(s["auto_check_updates"]))
    except (AttributeError, TypeError, ValueError):
        LOG.exception("applying imported preferences failed")
