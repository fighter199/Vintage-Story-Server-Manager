"""
ui/mods_browser.py — The MODS → BROWSE tab's ModDB logic: catalogs, search,
mod details, downloads and installs, and the update check.

Functions take the app first; VSSM.py attaches them as methods
(see _MODS_TAB_METHODS). Moved from ui/tab_mods.py unchanged.
"""
from __future__ import annotations

import os
import re
import threading
from datetime import datetime
from tkinter import messagebox
from typing import TYPE_CHECKING
import tkinter as tk

from core.utils import clean_mod_filename
from core.parsers import version_key, version_is_newer as _vsim_version_is_newer
from mods.inspector import LocalModInspector
from .theme import Theme
from .widgets import (TermButton)

if TYPE_CHECKING:
    from VSSM import ServerManagerApp  # for type hints only




def _sorted_releases(releases: list) -> list:
    """Sort ModDB releases newest-version-first regardless of upload date.

    ModDB returns releases ordered by upload time, which interleaves
    when a maintainer alternates between branches (e.g. shipping a 0.5.x
    bugfix after a 1.0.x release). We sort by `modversion` so the file
    list always reads naturally — 1.0.7, 1.0.6, …, 1.0.0, 0.5.7, 0.5.6, …
    """
    if not releases:
        return releases
    try:
        return sorted(
            releases,
            key=lambda r: version_key(str(r.get("modversion") or "")),
            reverse=True,
        )
    except Exception:
        # Never let a sort bug break the UI — fall back to raw order.
        return list(releases)




def _releases_compatible_with_gv(releases, selected_gv_name):
    """Filter ModDB releases to those tagged for `selected_gv_name`.

    Mirrors the matching rule in `_pick_best_release`: a release is
    compatible if any of its tags is exactly `selected_gv_name`.

    `selected_gv_name` of None / empty / "(any)" disables filtering
    and the input list is returned unchanged. This keeps the default
    behaviour (no GAME VER chosen) identical to the old all-releases
    check, so users who never touch the dropdown see no functional
    difference.
    """
    if not releases:
        return releases
    if not selected_gv_name or selected_gv_name == "(any)":
        return releases
    out = []
    for rel in releases:
        tags = [str(t) for t in (rel.get("tags") or [])]
        if selected_gv_name in tags:
            out.append(rel)
    return out


# ==================================================================
# ModDB Browser — online search, download, install
# ==================================================================
#
# All HTTP runs on worker threads; all UI updates are scheduled back
# onto the Tk main thread via app.after(). We keep a monotonically
# increasing request sequence so late-arriving responses from stale
# searches (user kept typing) get discarded.
#
# The "side" gate on download:
#   * Server, Universal/Both            → download without prompt
#   * Client                            → red confirm dialog required
#   * missing / unrecognized            → amber confirm dialog required
# ==================================================================

def init_moddb_catalogs_async(app: 'ServerManagerApp'):
    """Fetch /tags and /gameversions in the background, then populate
    the tag chips + game-version dropdown. Safe to call multiple times
    — we only actually fetch once per app session."""
    if getattr(app, "_moddb_catalogs_loaded", False):
        return
    app._set_moddb_status("Loading ModDB catalogs…")
    t = threading.Thread(target=app._moddb_catalogs_worker, daemon=True)
    t.start()

def _moddb_catalogs_worker(app: 'ServerManagerApp'):
    error = None
    tags = []
    gvs = []
    try:
        tags = app.moddb.get_tags()
        gvs = app.moddb.get_gameversions()
    except Exception as e:
        error = str(e)
    app.after(0, app._moddb_apply_catalogs, tags, gvs, error)

def _moddb_apply_catalogs(app: 'ServerManagerApp', tags, gvs, error):
    if error:
        app._set_moddb_status(f"ModDB offline: {error}")
        app._moddb_tag_body_note.configure(
            text=f"Could not load tags ({error}). "
                 f"Retry from ↻ Refresh.")
        return

    app._moddb_catalogs_loaded = True

    # Populate the game-version combobox. Prepend "" = any. The API
    # returns game versions newest-first; flip that so the dropdown
    # lists them in the opposite order (oldest first). "(any)" stays
    # pinned at the top since it's a mode, not a version.
    gv_values = [""]
    gv_labels = ["(any)"]
    app._moddb_gv_map = {"": None}   # display_label -> gv id or None
    for gv in reversed(gvs):
        name = str(gv.get("name") or gv.get("displayname") or "")
        gvid = gv.get("tagid") or gv.get("id")
        if name and gvid:
            label = name
            gv_labels.append(label)
            gv_values.append(label)
            app._moddb_gv_map[label] = gvid
    app.moddb_gv_combo.configure(values=gv_labels)
    app.moddb_gv_var.set("(any)")

    # Build tag chips
    for child in list(app.moddb_tag_scroll.body.winfo_children()):
        child.destroy()
    app._moddb_tag_buttons.clear()
    flow = tk.Frame(app.moddb_tag_scroll.body, bg=Theme.BG_INPUT)
    flow.pack(fill=tk.X, padx=4, pady=4)
    # We use a simple wrap-as-you-go layout: every chip is packed to
    # the left; tk's flow isn't auto, so we break rows manually based
    # on an estimated width budget.
    row_frame = tk.Frame(flow, bg=Theme.BG_INPUT)
    row_frame.pack(fill=tk.X, anchor=tk.W)
    row_budget = 0
    # Chip width budget is approximate — good enough for visual flow.
    ROW_MAX = 520
    for tag in tags:
        tagid = tag.get("tagid") or tag.get("id")
        name = str(tag.get("name") or "").strip()
        if not tagid or not name:
            continue
        est = max(60, 14 + 8 * len(name))
        if row_budget + est > ROW_MAX:
            row_frame = tk.Frame(flow, bg=Theme.BG_INPUT)
            row_frame.pack(fill=tk.X, anchor=tk.W)
            row_budget = 0
        btn = TermButton(row_frame, name,
                         lambda tid=tagid: app._toggle_moddb_tag(tid),
                         variant="amber", font_spec=app.F_SMALL,
                         padx=6, pady=1)
        btn.pack(side=tk.LEFT, padx=2, pady=2)
        app._moddb_tag_buttons[tagid] = btn
        row_budget += est + 4

    app._set_moddb_status(
        f"Loaded {len(tags)} tags, {len(gvs)} game versions. Ready.")
    # Kick off the first search automatically so the tab isn't blank.
    app._schedule_moddb_search(immediate=True)

def _toggle_moddb_tag(app: 'ServerManagerApp', tagid):
    if tagid in app._moddb_selected_tagids:
        app._moddb_selected_tagids.discard(tagid)
    else:
        app._moddb_selected_tagids.add(tagid)
    app._refresh_tag_button_styles()
    app._schedule_moddb_search(immediate=True)

def _refresh_tag_button_styles(app: 'ServerManagerApp'):
    # Highlight selected chips by toggling the fg/bg on the Label.
    for tagid, btn in app._moddb_tag_buttons.items():
        if tagid in app._moddb_selected_tagids:
            btn.configure(fg=Theme.AMBER_GLOW, bg=Theme.BG_SELECT)
            btn._bg = Theme.BG_SELECT
            btn._bg_hover = Theme.BG_SELECT
        else:
            btn.configure(fg=Theme.AMBER, bg=Theme.BG_BTN_AMBER)
            btn._bg = Theme.BG_BTN_AMBER
            btn._bg_hover = Theme.BG_BTN_AMBER_HOVER

def _clear_moddb_tags(app: 'ServerManagerApp'):
    if not app._moddb_selected_tagids:
        return
    app._moddb_selected_tagids.clear()
    app._refresh_tag_button_styles()
    app._schedule_moddb_search(immediate=True)

# --- search scheduling -------------------------------------------

def _schedule_moddb_search(app: 'ServerManagerApp', immediate=False):
    """Debounce the search — typing fires this many times a second,
    but we only want to hit the API once the user pauses."""
    if app._moddb_search_job is not None:
        try:
            app.after_cancel(app._moddb_search_job)
        except Exception:
            pass
        app._moddb_search_job = None
    delay = 0 if immediate else 400
    app._moddb_search_job = app.after(delay, app._run_moddb_search)

def _run_moddb_search(app: 'ServerManagerApp'):
    app._moddb_search_job = None
    if not getattr(app, "_moddb_catalogs_loaded", False):
        app.init_moddb_catalogs_async()
        return
    app._moddb_request_seq += 1
    seq = app._moddb_request_seq
    text = app.moddb_search_var.get().strip() or None
    tagids = sorted(app._moddb_selected_tagids) or None
    gv_label = app.moddb_gv_var.get()
    gv_id = app._moddb_gv_map.get(gv_label) if hasattr(app, "_moddb_gv_map") else None
    orderby = app.moddb_sort_var.get() or "trendingpoints"
    app._set_moddb_status("Searching ModDB…")
    t = threading.Thread(
        target=app._moddb_search_worker,
        args=(seq, text, tagids, gv_id, orderby),
        daemon=True)
    t.start()

def _moddb_search_worker(app: 'ServerManagerApp', seq, text, tagids, gv_id, orderby):
    try:
        mods = app.moddb.search_mods(
            text=text, tagids=tagids, gameversion=gv_id,
            orderby=orderby, orderdirection="desc")
        error = None
    except Exception as e:
        mods, error = [], str(e)
    app.after(0, app._moddb_apply_search, seq, mods, error)

def _moddb_apply_search(app: 'ServerManagerApp', seq, mods, error):
    # Ignore stale responses.
    if seq != app._moddb_request_seq:
        return
    if error:
        app._set_moddb_status(f"Search failed: {error}")
        app._moddb_results = []
    else:
        app._moddb_results = mods or []
    app._rerender_moddb_results()

def _rerender_moddb_results(app: 'ServerManagerApp'):
    """Rewrite the results text widget from app._moddb_results,
    honoring the current side-filter setting."""
    t = app.moddb_results_text
    t.configure(state='normal')
    t.delete("1.0", tk.END)
    app._moddb_row_index.clear()
    app._moddb_selected_row = None

    side_mode = app.moddb_side_var.get()
    shown = 0
    hidden_by_side = 0
    for i, mod in enumerate(app._moddb_results):
        side = app._normalize_side(mod.get("side"))
        if side_mode == "server_compat" and side == "client":
            hidden_by_side += 1
            continue
        if side_mode == "server_only" and side != "server":
            hidden_by_side += 1
            continue
        shown += 1

        # Row rendering — one logical row spans two visible lines.
        row_start = t.index("end-1c")
        row_num = int(row_start.split('.')[0])

        side_tag, side_label = app._side_badge(side)
        name = str(mod.get("name") or mod.get("modid") or "(unnamed)")
        t.insert(tk.END, f"  [{side_label:^6}]", (side_tag, "row"))
        t.insert(tk.END, f"  {name}\n", ("row_name", "row"))
        author = str(mod.get("author") or "?")
        downloads = mod.get("downloads") or 0
        summary = str(mod.get("summary") or "").strip()
        if len(summary) > 90:
            summary = summary[:87] + "…"
        meta = f"         by {author} · {downloads} downloads"
        if summary:
            meta += f" · {summary}"
        t.insert(tk.END, meta + "\n", ("row_meta", "row"))
        t.insert(tk.END, "\n", ("row",))

        # Index BOTH lines of the row so a click on either selects.
        for r in (row_num, row_num + 1, row_num + 2):
            app._moddb_row_index[r] = i

    if shown == 0:
        if app._moddb_results:
            t.insert(tk.END,
                     f"\n  All {hidden_by_side} result(s) hidden by "
                     f"side filter ('{side_mode}').\n",
                     ("row_meta",))
        else:
            t.insert(tk.END, "\n  No results.\n", ("row_meta",))

    t.configure(state='disabled')

    # Status strip summary
    if app._moddb_results:
        filt_bits = []
        if side_mode != "all":
            filt_bits.append(f"side={side_mode}")
        gv_label = app.moddb_gv_var.get()
        if gv_label and gv_label != "(any)":
            filt_bits.append(f"gv={gv_label}")
        if app._moddb_selected_tagids:
            filt_bits.append(f"{len(app._moddb_selected_tagids)} tag(s)")
        filt_desc = ", ".join(filt_bits) if filt_bits else "no filters"
        app._set_moddb_status(
            f"{shown} shown / {len(app._moddb_results)} results · {filt_desc}")

def _on_moddb_row_click(app: 'ServerManagerApp', event):
    row = int(app.moddb_results_text.index(f"@{event.x},{event.y}")
              .split('.')[0])
    idx = app._moddb_row_index.get(row)
    if idx is None:
        return
    # Highlight the whole logical row (three lines: header + meta + blank)
    t = app.moddb_results_text
    t.configure(state='normal')
    t.tag_remove("selected", "1.0", tk.END)
    # Find the header line for this idx
    header_row = None
    for r, i in app._moddb_row_index.items():
        if i == idx and (header_row is None or r < header_row):
            header_row = r
    if header_row is not None:
        t.tag_add("selected",
                  f"{header_row}.0", f"{header_row + 2}.end")
    t.configure(state='disabled')
    app._moddb_selected_row = idx
    app._load_mod_details_async(app._moddb_results[idx])

def _load_mod_details_async(app: 'ServerManagerApp', stub):
    mod_id = stub.get("modid") or stub.get("assetid")
    if not mod_id:
        return
    app._set_moddb_status(f"Loading details for {stub.get('name') or mod_id}…")
    seq = app._moddb_request_seq
    t = threading.Thread(
        target=app._mod_detail_worker,
        args=(seq, mod_id, stub),
        daemon=True)
    t.start()

def _mod_detail_worker(app: 'ServerManagerApp', seq, mod_id, stub):
    try:
        detail = app.moddb.get_mod(mod_id)
        error = None
    except Exception as e:
        detail, error = None, str(e)
    app.after(0, app._apply_mod_detail, seq, stub, detail, error)

def _apply_mod_detail(app: 'ServerManagerApp', seq, stub, detail, error):
    if seq != app._moddb_request_seq:
        return
    if error or not detail:
        app._set_moddb_status(f"Detail load failed: {error}")
        app._render_mod_detail(None, fallback_stub=stub, error=error)
        return
    # Sort releases once at the cache boundary so every downstream
    # reader (renderer, click handler, install pipeline, update
    # checker) sees the same true-newest-first order.
    if isinstance(detail.get("releases"), list):
        detail["releases"] = _sorted_releases(detail["releases"])
    app._moddb_current_mod = detail
    app._render_mod_detail(detail)
    app._set_moddb_status(f"Loaded '{detail.get('name') or stub.get('name')}'.")

def _render_mod_detail(app: 'ServerManagerApp', mod, fallback_stub=None, error=None):
    d = app.moddb_details_text
    d.configure(state='normal')
    d.delete("1.0", tk.END)
    if mod is None:
        m = fallback_stub or {}
        d.insert(tk.END, f"{m.get('name') or 'Unknown'}\n", ("title",))
        if error:
            d.insert(tk.END, f"Could not load details: {error}\n",
                     ("meta",))
        else:
            d.insert(tk.END, "No detail available.\n", ("meta",))
        d.configure(state='disabled')
        app._render_mod_files([])
        return

    side = app._normalize_side(mod.get("side"))
    side_tag, side_label = app._side_badge(side)
    name = str(mod.get("name") or "(unnamed)")
    d.insert(tk.END, f"{name}\n", ("title",))

    side_line_tag = side_tag
    d.insert(tk.END, f"SIDE: {side_label}   ",
             (side_line_tag,))
    d.insert(tk.END,
             f"by {mod.get('author') or '?'}   "
             f"downloads: {mod.get('downloads') or 0}   "
             f"follows: {mod.get('follows') or 0}\n",
             ("meta",))
    tags_list = mod.get("tags") or []
    if tags_list:
        d.insert(tk.END, "tags: ", ("meta",))
        d.insert(tk.END, ", ".join(str(t) for t in tags_list) + "\n",
                 ("body",))
    if mod.get("urlalias"):
        d.insert(tk.END, f"url: {app.moddb.SITE_BASE}/{mod['urlalias']}\n",
                 ("meta",))
    elif mod.get("assetid"):
        d.insert(tk.END,
                 f"url: {app.moddb.SITE_BASE}/show/mod/{mod['assetid']}\n",
                 ("meta",))
    d.insert(tk.END, "\n")

    desc = str(mod.get("text") or mod.get("description")
               or mod.get("summary") or "").strip()
    # The API's mod.text may contain HTML from the mod page; for a
    # terminal-style readout we strip tags naively so we never render
    # raw markup.
    desc = re.sub(r"<[^>]+>", "", desc)
    desc = re.sub(r"\s+\n", "\n", desc)
    if desc:
        d.insert(tk.END, desc + "\n", ("body",))
    d.configure(state='disabled')

    releases = mod.get("releases") or []
    app._render_mod_files(releases)

def _render_mod_files(app: 'ServerManagerApp', releases):
    """Render the file-picker. Each release typically has:
        releaseid, mainfile, filename, fileid, downloads,
        tags (game versions), modversion, created
    """
    ft = app.moddb_files_text
    ft.configure(state='normal')
    ft.delete("1.0", tk.END)
    app._moddb_file_rows.clear()
    app._moddb_file_selected_row = None
    app._moddb_current_file = None

    if not releases:
        ft.insert(tk.END, "  (no files)\n", ("file_meta",))
        ft.configure(state='disabled')
        return

    # Pick a "best match" release whose game-version tags include the
    # currently-selected gv label, so we can visually highlight it.
    gv_label = app.moddb_gv_var.get()
    selected_gv_name = gv_label if gv_label and gv_label != "(any)" else None
    best_idx = app._pick_best_release(releases, selected_gv_name)

    for i, rel in enumerate(releases):
        row_start = ft.index("end-1c")
        row_num = int(row_start.split('.')[0])
        filename = str(rel.get("filename") or "(file)")
        modversion = rel.get("modversion") or "?"
        gv_tags = rel.get("tags") or []
        gv_text = ", ".join(str(t) for t in gv_tags) if gv_tags else "?"
        created = rel.get("created") or ""
        try:
            size_bytes = int(rel.get("filesize") or 0)
        except (ValueError, TypeError):
            size_bytes = 0
        size_text = app._fmt_size(size_bytes) if size_bytes else "?"

        marker = "★" if i == best_idx else " "
        tag = "file_best" if i == best_idx else "file"
        ft.insert(tk.END,
                  f"  {marker} v{modversion}  [{gv_text}]  {size_text}\n",
                  (tag,))
        ft.insert(tk.END,
                  f"      {filename}  ({created[:10] if created else ''})\n",
                  ("file_meta",))
        app._moddb_file_rows[row_num] = rel
        app._moddb_file_rows[row_num + 1] = rel

    ft.configure(state='disabled')

    # Auto-select the best-match file
    if best_idx is not None and best_idx < len(releases):
        app._select_file_row(best_idx, releases)

def _pick_best_release(app: 'ServerManagerApp', releases, selected_gv_name):
    """Return index of the best release given the current gv filter.

    Preference order:
      1. A release whose tags include the selected gv name exactly.
      2. The first release (releases are typically sorted newest-first).
    """
    if selected_gv_name:
        for i, rel in enumerate(releases):
            tags = [str(t) for t in (rel.get("tags") or [])]
            if selected_gv_name in tags:
                return i
    return 0

def _select_file_row(app: 'ServerManagerApp', idx, releases):
    # Find the text row (line number) that maps to this release.
    header_line = None
    for line, rel in app._moddb_file_rows.items():
        if rel is releases[idx] and (header_line is None or line < header_line):
            header_line = line
    if header_line is None:
        return
    ft = app.moddb_files_text
    ft.configure(state='normal')
    ft.tag_remove("selected", "1.0", tk.END)
    ft.tag_add("selected", f"{header_line}.0", f"{header_line + 1}.end")
    ft.configure(state='disabled')
    app._moddb_file_selected_row = header_line
    app._moddb_current_file = releases[idx]

def _on_moddb_file_click(app: 'ServerManagerApp', event):
    row = int(app.moddb_files_text.index(f"@{event.x},{event.y}")
              .split('.')[0])
    rel = app._moddb_file_rows.get(row)
    if not rel or not app._moddb_current_mod:
        return
    releases = app._moddb_current_mod.get("releases") or []
    try:
        idx = releases.index(rel)
    except ValueError:
        return
    app._select_file_row(idx, releases)

# --- install pipeline --------------------------------------------

def _install_current_file(app: 'ServerManagerApp'):
    if app._moddb_download_active:
        app._notify("Another download is already running.", level="warn")
        return
    mod = app._moddb_current_mod
    rel = app._moddb_current_file
    if not mod or not rel:
        app._notify("Select a mod and a file to install.", level="warn")
        return

    side = app._normalize_side(mod.get("side"))
    # The side gate.
    if side == "client":
        if not messagebox.askyesno(
                "Client-side mod",
                f"The mod '{mod.get('name')}' is marked CLIENT SIDE ONLY.\n\n"
                "Installing it on a server is almost certainly useless and "
                "may cause startup errors or crashes.\n\n"
                "Install anyway?"):
            app._set_moddb_status("Install cancelled (client-side).")
            return
    elif side == "unknown":
        if not messagebox.askyesno(
                "Side not declared",
                f"The mod '{mod.get('name')}' does not declare which side "
                "it runs on.\n\nIf it turns out to be client-only it won't "
                "help your server. Continue?"):
            app._set_moddb_status("Install cancelled (side unknown).")
            return
    # "server" and "universal" proceed silently.

    # Warn if the server is actively running.
    if app.is_running:
        if not messagebox.askyesno(
                "Server running",
                "The server is running. The mod will be downloaded now "
                "but will not take effect until the server restarts.\n\n"
                "Continue?"):
            return

    # Resolve destination + duplicate handling
    mods_dir = app.mods_folder_var.get()
    if not mods_dir or not os.path.isdir(mods_dir):
        app._notify("Set a valid Mods folder first.", level="error")
        return
    url = rel.get("mainfile") or rel.get("file") or rel.get("filename")
    if url and not url.startswith("http"):
        # API often returns mainfile as a relative path; combine with site.
        url = app.moddb.SITE_BASE + "/" + url.lstrip("/")
    if not url:
        app._notify("Release has no file URL.", level="error")
        return
    if not app.moddb.is_trusted_url(url):
        app._notify(f"Refusing download from untrusted host: {url}",
                     level="error")
        return

    # Build the destination filename. ModDB's direct-download URLs
    # embed a cache-buster hash in the filename (e.g.
    # "mymod_1.2.3_abcdef123456789.zip"), so naively using the URL
    # basename leaves a long hex suffix on every installed file.
    #
    # Field hierarchy: ModDB's API returns the human-readable mod
    # slug as "urlalias" (e.g. "medievalexpansion"), and "modid" is
    # frequently a numeric primary key — using that produces names
    # like "4571.zip" which is meaningless. We pass urlalias first,
    # the helper rejects numeric values internally, and "name"
    # (display name) is a final slugify-able fallback.
    filename = clean_mod_filename(
        url=url,
        declared=rel.get("filename"),
        modid=mod.get("urlalias") or mod.get("modid"),
        version=rel.get("modversion") or rel.get("version"),
        name=mod.get("name"),
    )
    dest_path = os.path.join(mods_dir, filename)
    if os.path.exists(dest_path):
        choice = messagebox.askyesnocancel(
            "File exists",
            f"'{filename}' already exists in the Mods folder.\n\n"
            "Yes = overwrite\n"
            "No  = keep both (append timestamp to new file)\n"
            "Cancel = abort")
        if choice is None:
            return
        if choice is False:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            stem, ext = os.path.splitext(filename)
            filename = f"{stem}.{ts}{ext}"
            dest_path = os.path.join(mods_dir, filename)

    try:
        expected_size = int(rel.get("filesize") or 0) or None
    except (ValueError, TypeError):
        expected_size = None

    app._moddb_download_cancel["flag"] = False
    app._moddb_download_active = True
    app.moddb_install_btn.set_enabled(False)
    app.moddb_cancel_btn.set_enabled(True)
    app._set_moddb_progress(0, 0, "Starting…")
    app.append_console(
        f"Downloading '{mod.get('name')}' → {filename}", "system")

    t = threading.Thread(
        target=app._moddb_download_worker,
        args=(url, dest_path, expected_size, mod.get('name') or filename),
        daemon=True)
    t.start()

def _cancel_moddb_download(app: 'ServerManagerApp'):
    if not app._moddb_download_active:
        return
    app._moddb_download_cancel["flag"] = True
    app._set_moddb_status("Cancelling download…")

def _moddb_download_worker(app: 'ServerManagerApp', url, dest, expected_size, display_name):
    def progress_cb(got, total):
        app.after(0, app._set_moddb_progress, got, total, "Downloading")
    def cancel_cb():
        return app._moddb_download_cancel.get("flag", False)
    try:
        app.moddb.download_file(url, dest,
                                 progress_cb=progress_cb,
                                 cancel_flag=cancel_cb,
                                 expected_size=expected_size)
        error = None
    except Exception as e:
        error = str(e)
    app.after(0, app._finalize_moddb_download, dest, display_name, error)

def _finalize_moddb_download(app: 'ServerManagerApp', dest, display_name, error):
    app._moddb_download_active = False
    app.moddb_install_btn.set_enabled(True)
    app.moddb_cancel_btn.set_enabled(False)
    if error:
        app._set_moddb_status(f"Install failed: {error}")
        app._set_moddb_progress(0, 0, "failed")
        app.append_console(
            f"Download of '{display_name}' failed: {error}", "error")
        app._notify(f"Install failed: {error}", level="error")
        return
    app._set_moddb_status(f"Installed: {os.path.basename(dest)}")
    app._set_moddb_progress(1, 1, "done")
    app.append_console(
        f"✓ Installed {display_name} → {os.path.basename(dest)}",
        "success")
    app._notify(f"Installed: {os.path.basename(dest)}",
                 level="success")
    # Refresh installed list if the file landed in the active Mods folder.
    if os.path.dirname(dest) == app.mods_folder_var.get():
        app.load_mods()

def _set_moddb_progress(app: 'ServerManagerApp', got, total, label):
    if total and total > 0:
        frac = max(0.0, min(1.0, got / total))
        app.moddb_progress_fill.place_configure(relwidth=frac)
        app.moddb_progress_var.set(
            f"{label}  {app._fmt_size(got)} / {app._fmt_size(total)} "
            f"({frac * 100:.0f}%)")
    else:
        app.moddb_progress_var.set(f"{label}  {app._fmt_size(got)}")
        if label == "done":
            app.moddb_progress_fill.place_configure(relwidth=1)
        elif label == "failed":
            app.moddb_progress_fill.place_configure(relwidth=0)

# --- update checker ---------------------------------------------

def check_mod_updates(app: 'ServerManagerApp'):
    """Read every local mod's modinfo.json, then ask ModDB for the
    latest matching release. Present a summary dialog + offer bulk
    update for those that are stale.

    Uses the on-disk TTL cache by default; tick the "Force refresh"
    checkbox in the Mods tab toolbar to bypass it for one run."""
    mods_dir = app.mods_folder_var.get()
    if not mods_dir or not os.path.isdir(mods_dir):
        app._notify("Set a valid Mods folder first.", level="warn")
        return
    local = []
    for fn in sorted(os.listdir(mods_dir)):
        if not fn.lower().endswith(
                ('.zip', '.jar', '.cs', '.dll', '.disabled')):
            continue
        full = os.path.join(mods_dir, fn)
        info = LocalModInspector.read_mod_file(full)
        local.append(info)
    if not local:
        app._notify("No mods found locally.", level="info")
        return

    # Pull the force-refresh flag off the toolbar Tk var (set by the
    # checkbox added in _build_mods_browse_left). The plain bool
    # attribute is what the worker actually reads.
    try:
        var = getattr(app, "_update_check_force_refresh_var", None)
        app._update_check_force_refresh = bool(var.get()) if var is not None else False
    except Exception:
        app._update_check_force_refresh = False

    app._set_moddb_status(
        f"Checking updates for {len(local)} mod(s) "
        f"{'(force refresh) ' if app._update_check_force_refresh else ''}…")
    t = threading.Thread(
        target=app._update_check_worker,
        args=(local,),
        daemon=True)
    t.start()

def _update_check_worker(app: 'ServerManagerApp', local_mods):
    """For each local mod with a modid, query /api/mod/{modid} via
    the cached + parallelised path, compare versions, and collect
    results. Marshals back to the UI thread.

    Performance:
      - Network calls are run via ThreadPoolExecutor with
        ModDbClient.UPDATE_CHECK_PARALLELISM workers (default 8).
      - get_mod_cached() consults the on-disk TTL cache first
        (default 6h), so re-running the check inside the TTL
        window costs zero network.
      - The "Force refresh" checkbox in the Mods tab bypasses the
        cache for one check; the helper sets
        app._update_check_force_refresh.
    """
    import concurrent.futures

    # Lazy-attach the cache the first time we need it. The path lives
    # next to settings.json so it travels with portable installs.
    try:
        if getattr(app.moddb, "_mod_cache", None) is None:
            from core.settings import settings_path
            cache_path = os.path.join(
                os.path.dirname(settings_path()), "moddb_cache.json")
            app.moddb.attach_cache(cache_path)
    except Exception:
        # Cache attachment failure is non-fatal — fall through to
        # per-call fetches.
        pass

    force = bool(getattr(app, "_update_check_force_refresh", False))

    # Resolve the active GAME VER label once, outside the worker
    # closure. Defaults to None when the BROWSE tab has not been
    # built yet, in which case the gv filter is a no-op.
    try:
        _gv_label = app.moddb_gv_var.get()
    except (AttributeError, tk.TclError):
        _gv_label = ""
    selected_gv_name = (_gv_label
                         if _gv_label and _gv_label != "(any)"
                         else None)

    # Pre-pass: split mods that don't have a modid (no need to
    # consume thread-pool slots on them).
    no_modid = [info for info in local_mods if not info.get("modid")]
    have_modid = [info for info in local_mods if info.get("modid")]

    # Surface progress for cached vs. fetched as we go.
    cache_hits = [0]
    fetched    = [0]
    def _progress():
        done = cache_hits[0] + fetched[0]
        app.after(0, app._set_moddb_status,
                   f"Checking updates — {done}/{len(have_modid)} "
                   f"({cache_hits[0]} cached, {fetched[0]} fetched)…")

    def _check_one(info):
        modid = info.get("modid")
        try:
            had_cache = not force and app.moddb.has_fresh_cached(modid)
            detail = app.moddb.get_mod_cached(modid, force_refresh=force)
            if had_cache:
                cache_hits[0] += 1
            else:
                fetched[0] += 1
            _progress()
        except Exception as e:
            fetched[0] += 1
            _progress()
            return {"info": info, "status": "error", "error": str(e)}
        all_releases = _sorted_releases(detail.get("releases") or [])
        if not all_releases:
            return {"info": info, "status": "no_releases",
                    "detail": detail}
        # Filter to releases tagged for the selected game version.
        # selected_gv_name closes over the worker-level resolution
        # made before the pre-pass; None disables filtering.
        compatible = _releases_compatible_with_gv(
            all_releases, selected_gv_name)
        if not compatible:
            # Mod has releases, but none for the selected game
            # version. Surface separately so the user knows what
            # happened — it is neither outdated nor an error.
            return {
                "info":   info,
                "detail": detail,
                "status": "no_compatible",
                "selected_gv": selected_gv_name,
            }
        latest     = compatible[0]
        latest_ver = str(latest.get("modversion") or "")
        local_ver  = str(info.get("version") or "")
        # Canonical comparator from core.parsers — handles
        # prereleases correctly, with a packaging-library fast
        # path when present.
        is_newer   = _vsim_version_is_newer(latest_ver, local_ver)
        return {
            "info":       info,
            "detail":     detail,
            "latest":     latest,
            "latest_ver": latest_ver,
            "local_ver":  local_ver,
            "status":     "outdated" if is_newer else "current",
        }

    report = [{"info": info, "status": "no_modid"} for info in no_modid]
    if have_modid:
        # ModDbClient is thread-safe across separate get_mod calls
        # (urllib.request is, and the SSL context is shared safely).
        max_workers = max(1, getattr(
            type(app.moddb), "UPDATE_CHECK_PARALLELISM", 8))
        # Cap at the number of pending mods — no point spinning up
        # 8 workers for 3 mods.
        max_workers = min(max_workers, len(have_modid))
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="vssm-modcheck") as ex:
            futures = [ex.submit(_check_one, info) for info in have_modid]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    report.append(fut.result())
                except Exception as e:
                    # _check_one shouldn't raise — anything getting here
                    # is a logic bug, not a per-mod failure. Log it as
                    # an "error" entry on a placeholder info dict so the
                    # UI still surfaces it.
                    report.append({"info": {"name": "(unknown)"},
                                   "status": "error", "error": str(e)})

    # Persist the cache so the next launch starts warm. Best-effort.
    try:
        app.moddb.save_cache()
    except Exception:
        pass

    app.after(0, app._show_update_report, report)

def _show_update_report(app: 'ServerManagerApp', report):
    """Bucket the report and open the picker dialog.

    Buckets:
      outdated      — newer compatible release available
      current       — already at latest compatible release
      no_compatible — has releases but none match the GAME VER filter
      error         — ModDB lookup failed
      no_modid      — local file has no modid (can't be checked)
    """
    outdated     = [r for r in report if r.get("status") == "outdated"]
    current      = [r for r in report if r.get("status") == "current"]
    no_compat    = [r for r in report if r.get("status") == "no_compatible"]
    errors       = [r for r in report if r.get("status") == "error"]
    no_id        = [r for r in report if r.get("status") == "no_modid"]
    bits = [
        f"{len(outdated)} outdated",
        f"{len(current)} up-to-date",
    ]
    if no_compat:
        bits.append(f"{len(no_compat)} no compatible release")
    bits.append(f"{len(errors)} error(s)")
    bits.append(f"{len(no_id)} unreadable")
    app._set_moddb_status("Update check: " + ", ".join(bits) + ".")

    # Open the picker. Note that on confirm, control returns here
    # AFTER the bulk update has finished (the runner blocks the
    # dialog until done). On cancel, selected_reports stays empty.
    dlg = _ModUpdatePickerDialog(
        app, outdated, current, errors, no_id,
        no_compat=no_compat)
    try:
        app.wait_window(dlg)
    except tk.TclError:
        return

def _bulk_update(app: 'ServerManagerApp', outdated_reports):
    """Download and replace each outdated mod. Client-side warnings still
    apply; the user can opt to skip them all with a single confirm."""
    # Surface any client-only mods up-front so the user can skip them in bulk.
    client_side = []
    for r in outdated_reports:
        side = app._normalize_side(r.get("detail", {}).get("side"))
        if side == "client":
            client_side.append(r)

    skip_client = False
    if client_side:
        names = ", ".join(str(r["info"].get("name")) for r in client_side[:5])
        if len(client_side) > 5:
            names += f" and {len(client_side) - 5} more"
        skip_client = messagebox.askyesno(
            "Client-only mods in update list",
            f"{len(client_side)} of the outdated mods are CLIENT SIDE "
            f"ONLY: {names}.\n\nSkip all client-only mods?")

    to_process = []
    for r in outdated_reports:
        side = app._normalize_side(r.get("detail", {}).get("side"))
        if side == "client" and skip_client:
            continue
        to_process.append(r)

    if not to_process:
        app._notify("Nothing to update after filtering.", level="info")
        return

    app._set_moddb_status(
        f"Bulk update: {len(to_process)} mod(s) queued.")
    t = threading.Thread(
        target=app._bulk_update_worker,
        args=(to_process,),
        daemon=True)
    t.start()

def _bulk_update_worker(app: 'ServerManagerApp', reports):
    mods_dir = app.mods_folder_var.get()
    successes = 0
    failures = []
    for i, r in enumerate(reports, 1):
        info = r["info"]
        latest = r.get("latest") or {}
        name = info.get("name") or "(unnamed)"
        app.after(0, app._set_moddb_status,
                   f"[{i}/{len(reports)}] Updating {name}…")
        url = latest.get("mainfile") or ""
        if url and not url.startswith("http"):
            url = app.moddb.SITE_BASE + "/" + url.lstrip("/")
        if not url or not app.moddb.is_trusted_url(url):
            failures.append((name, "no/untrusted URL"))
            continue
        try:
            expected_size = int(latest.get("filesize") or 0) or None
        except (ValueError, TypeError):
            expected_size = None
        filename = clean_mod_filename(
            url=url,
            declared=latest.get("filename"),
            # info.modid here comes from the LOCAL mod's
            # modinfo.json — already a real string identifier, not
            # a numeric ModDB primary key. Still goes through the
            # numeric-rejection guard inside the helper for safety.
            modid=info.get("modid"),
            version=latest.get("modversion") or latest.get("version"),
            name=info.get("name"),
        )
        dest = os.path.join(mods_dir, filename)
        # If the old file is different from the new filename, back up
        # and remove the old one after a successful download.
        old_path = info.get("path")
        try:
            app.moddb.download_file(url, dest,
                                     expected_size=expected_size)
            if old_path and os.path.exists(old_path) \
                    and os.path.abspath(old_path) != os.path.abspath(dest):
                try:
                    os.remove(old_path)
                except OSError:
                    pass
            successes += 1
        except Exception as e:
            failures.append((name, str(e)))
    app.after(0, app._finalize_bulk_update, successes, failures)

def _finalize_bulk_update(app: 'ServerManagerApp', successes, failures):
    app.load_mods()
    summary = f"{successes} updated"
    if failures:
        summary += f", {len(failures)} failed"
        app.append_console("Bulk update failures:", "warn")
        for name, err in failures:
            app.append_console(f"  • {name}: {err}", "error")
    else:
        summary += "."
    app._set_moddb_status(summary)
    app._notify(f"Bulk update: {summary}",
                 level="success" if not failures else "warn")

# --- helpers -----------------------------------------------------

def _set_moddb_status(app: 'ServerManagerApp', text):
    app.moddb_status_var.set(text)

def _open_current_mod_in_browser(app: 'ServerManagerApp'):
    mod = app._moddb_current_mod
    if not mod:
        app._notify("Select a mod first.", level="info")
        return
    url = None
    if mod.get("urlalias"):
        url = f"{app.moddb.SITE_BASE}/{mod['urlalias']}"
    elif mod.get("assetid"):
        url = f"{app.moddb.SITE_BASE}/show/mod/{mod['assetid']}"
    if not url:
        app._notify("No URL for this mod.", level="warn")
        return
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception as e:
        app._notify(f"Could not open browser: {e}", level="error")



# ──────────────────────────────────────────────────────────────────────


from .mods_updates import _ModUpdatePickerDialog  # noqa: E402
