"""
ui/tab_discord.py — "DISCORD" tab.

Configures the Discord status poster: a channel webhook that receives
the server's address and current player count, plus optional periodic
map images (rendered from the savegame, or exported by a map mod).
The tab only edits the
per-profile config; ServerManagerApp owns the scheduler and the
background worker that actually talk to Discord.
"""
from __future__ import annotations

import tkinter as tk
from datetime import datetime
from tkinter import filedialog, ttk

from .theme import Theme
from .widgets import (TermButton, TermEntry, TermCheckbutton,
                      ScrollableFrame, auto_wrap, flow_children,
                      panel_header)
from core.discord_status import (parse_tag_ids, url_thread_id,
                                 validate_webhook_url)

# Display label <-> config value for the update-mode combobox
_MODES = [("Edit one status message (recommended)", "edit"),
          ("Post a new message every update",       "post")]
_MODE_LABELS = [m[0] for m in _MODES]
_LABEL_TO_MODE = dict(_MODES)
_MODE_TO_LABEL = {v: k for k, v in _MODES}

_SOURCES = [("Built-in renderer (live savegame)", "savegame"),
            ("Image exported by a map mod",       "file")]
_SOURCE_LABELS = [s[0] for s in _SOURCES]
_LABEL_TO_SOURCE = dict(_SOURCES)
_SOURCE_TO_LABEL = {v: k for k, v in _SOURCES}


class DiscordTab:
    """Builds and owns the Discord tab content."""

    def __init__(self, parent: tk.Frame, app):
        self._parent = parent
        self._app = app
        self._build(parent)
        self.reload_from_settings()

    # ------------------------------------------------------------------
    # Public API used by the host app
    # ------------------------------------------------------------------
    def reload_from_settings(self) -> None:
        cfg = self._load_config()
        self._enabled_var.set(cfg["enabled"])
        self._url_var.set(cfg["webhook_url"])
        self._addr_var.set(cfg["address_override"])
        self._mode_var.set(_MODE_TO_LABEL.get(cfg["update_mode"],
                                              _MODE_LABELS[0]))
        self._interval_var.set(str(cfg["interval_mins"]))
        self._on_start_var.set(cfg["post_on_start"])
        self._on_stop_var.set(cfg["post_on_stop"])
        self._on_players_var.set(cfg["post_on_player_change"])
        self._names_var.set(cfg["show_player_names"])
        self._map_enabled_var.set(cfg["map_enabled"])
        self._map_source_var.set(_SOURCE_TO_LABEL[cfg["map_source"]])
        self._map_mark_var.set(cfg["map_mark_players"])
        self._map_path_var.set(cfg["map_path"])
        self._map_interval_var.set(str(cfg["map_interval_mins"]))
        self._map_cmd_var.set(cfg["map_command"])
        self._map_wait_var.set(str(cfg["map_command_wait_secs"]))
        self._forum_title_var.set(cfg["forum_post_title"])
        self._forum_tags_var.set(cfg["forum_tag_ids"])
        self._show_forum_status(cfg)
        self._sync_map_source()

    def _show_forum_status(self, cfg: dict) -> None:
        fixed = url_thread_id(cfg["webhook_url"])
        if fixed:
            text = f"Posting into the forum post / thread {fixed} (from the URL)."
        elif cfg["forum_thread_id"]:
            text = (f"Posting into forum post {cfg['forum_thread_id']}. "
                    "New Messages starts a new post.")
        else:
            text = ""
        self._forum_status_var.set(text)

    def show_result(self, result) -> None:
        """Called by the host (on the Tk thread) after each send."""
        ts = datetime.now().strftime("%H:%M:%S")
        what = (result.kind if result.reason == result.kind
                else f"{result.kind}, {result.reason}")
        if result.ok:
            where = f" — {result.address}" if result.address else ""
            self._result_var.set(f"[{ts}] ✓ {result.message} "
                                 f"({what}){where}")
            self._result_label.configure(fg=Theme.GREEN)
        else:
            self._result_var.set(f"[{ts}] ✗ {what}: {result.message}")
            self._result_label.configure(fg=Theme.RED)
        self._show_forum_status(self._load_config())

    def show_detected_ip(self, ip: str | None) -> None:
        if ip:
            self._detected_var.set(f"Detected public IP: {ip}")
        else:
            self._detected_var.set("Could not detect public IP "
                                   "(offline or lookup blocked).")

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------
    def _build(self, parent: tk.Frame) -> None:
        F_SMALL, F_NORMAL = self._app.F_SMALL, self._app.F_NORMAL
        outer = tk.Frame(parent, bg=Theme.BG_PANEL)
        outer.pack(fill=tk.BOTH, expand=True)
        panel_header(outer, "Discord status", font_spec=self._app.F_HDR)

        sf = ScrollableFrame(outer, bg=Theme.BG_PANEL)
        sf.pack(fill=tk.BOTH, expand=True)
        body = sf.body
        pad = 8

        def label(text, pady=(10, 1), parent=body):
            auto_wrap(tk.Label(parent, text=text, fg=Theme.AMBER_DIM,
                               bg=Theme.BG_PANEL, font=F_SMALL)
                      ).pack(fill=tk.X, padx=pad, pady=pady)

        def hint(text, pady=(1, 0), parent=body):
            auto_wrap(tk.Label(parent, text=text, fg=Theme.MUTED,
                               bg=Theme.BG_PANEL, font=F_SMALL)
                      ).pack(fill=tk.X, padx=pad, pady=pady)

        hint("Posts the server's address and player count to a Discord "
             "channel. Settings are per-profile.", pady=(4, 0))

        self._enabled_var = tk.BooleanVar(value=False)
        auto_wrap(TermCheckbutton(body, "Enable Discord status updates",
                                  self._enabled_var, font_spec=F_NORMAL)
                  ).pack(fill=tk.X, padx=pad, pady=(8, 2))

        # Webhook URL
        label("CHANNEL WEBHOOK URL:")
        url_row = tk.Frame(body, bg=Theme.BG_PANEL)
        url_row.pack(fill=tk.X, padx=pad)
        self._url_var = tk.StringVar()
        self._url_entry = TermEntry(url_row, textvariable=self._url_var,
                                    font_spec=F_NORMAL, show="•")
        self._url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        self._show_url_var = tk.BooleanVar(value=False)
        TermCheckbutton(url_row, "Show", self._show_url_var,
                        font_spec=F_SMALL, command=self._toggle_url_visible
                        ).pack(side=tk.LEFT, padx=(6, 0))
        hint("In Discord: Channel → Edit Channel → Integrations → Webhooks "
             "→ New Webhook → Copy Webhook URL. Treat it like a password — "
             "anyone with it can post to the channel.")

        # Address
        label("SERVER ADDRESS  (blank = auto-detect):")
        self._addr_var = tk.StringVar()
        TermEntry(body, textvariable=self._addr_var,
                  font_spec=F_NORMAL).pack(fill=tk.X, padx=pad, ipady=3)
        addr_row = tk.Frame(body, bg=Theme.BG_PANEL)
        addr_row.pack(fill=tk.X, padx=pad, pady=(4, 0))
        TermButton(addr_row, "Detect public IP", self._on_detect_ip,
                   variant="amber", font_spec=F_SMALL,
                   padx=8, pady=3).pack(side=tk.LEFT)
        self._detected_var = tk.StringVar(value="")
        auto_wrap(tk.Label(body, textvariable=self._detected_var,
                           fg=Theme.MUTED, bg=Theme.BG_PANEL, font=F_SMALL)
                  ).pack(fill=tk.X, padx=pad, pady=(2, 0))
        hint("Use a hostname (e.g. play.example.com) to hide your raw IP. "
             "The port from serverconfig.json is appended automatically. "
             "Auto-detect asks api.ipify.org for your public address.")

        # Forum channels
        label("FORUM POST TITLE  (forum channels only):")
        self._forum_title_var = tk.StringVar()
        TermEntry(body, textvariable=self._forum_title_var,
                  font_spec=F_NORMAL).pack(fill=tk.X, padx=pad, ipady=3)
        label("FORUM TAG IDS  (optional, comma-separated):", pady=(6, 1))
        self._forum_tags_var = tk.StringVar()
        TermEntry(body, textvariable=self._forum_tags_var,
                  font_spec=F_NORMAL).pack(fill=tk.X, padx=pad, ipady=3)
        self._forum_status_var = tk.StringVar(value="")
        auto_wrap(tk.Label(body, textvariable=self._forum_status_var,
                           fg=Theme.MUTED, bg=Theme.BG_PANEL, font=F_SMALL)
                  ).pack(fill=tk.X, padx=pad, pady=(2, 0))
        hint("For a webhook in a forum channel: VSSM starts one forum post "
             "with this title and keeps posting its status and map there "
             "(blank = \"Vintage Story server\"). Tags are needed only if the "
             "forum requires them — right-click a tag with Developer Mode on "
             "→ Copy ID. To post into a post that already exists instead, "
             "add ?thread_id=<post id> to the webhook URL.")

        # Update mode + interval
        label("UPDATE MODE:")
        self._mode_var = tk.StringVar(value=_MODE_LABELS[0])
        ttk.Combobox(body, textvariable=self._mode_var, values=_MODE_LABELS,
                     state="readonly", width=40, font=F_SMALL,
                     style="Term.TCombobox").pack(anchor=tk.W, padx=pad)

        int_row = tk.Frame(body, bg=Theme.BG_PANEL)
        int_row.pack(fill=tk.X, padx=pad, pady=(10, 2))
        tk.Label(int_row, text="REFRESH EVERY:", fg=Theme.AMBER_DIM,
                 bg=Theme.BG_PANEL, font=F_SMALL).pack(side=tk.LEFT)
        self._interval_var = tk.StringVar(value="5")
        TermEntry(int_row, textvariable=self._interval_var, width=6,
                  font_spec=F_NORMAL).pack(side=tk.LEFT, padx=6, ipady=2)
        tk.Label(int_row, text="min", fg=Theme.MUTED, bg=Theme.BG_PANEL,
                 font=F_SMALL).pack(side=tk.LEFT)
        flow_children(int_row, spacing=6)
        hint("While the server runs. 0 = only post on the events below.")

        # Triggers
        label("POST WHEN:", pady=(10, 0))
        self._on_start_var   = tk.BooleanVar(value=True)
        self._on_stop_var    = tk.BooleanVar(value=True)
        self._on_players_var = tk.BooleanVar(value=True)
        self._names_var      = tk.BooleanVar(value=True)
        for text, var in [
            ("The server starts", self._on_start_var),
            ("The server stops or crashes", self._on_stop_var),
            ("A player joins or leaves (batched over 10 s)",
             self._on_players_var),
        ]:
            auto_wrap(TermCheckbutton(body, text, var, font_spec=F_SMALL)
                      ).pack(fill=tk.X, padx=(pad + 8, pad), pady=1)
        auto_wrap(TermCheckbutton(body, "Include the names of online players",
                                  self._names_var, font_spec=F_SMALL)
                  ).pack(fill=tk.X, padx=pad, pady=(8, 2))

        # Map snapshots
        tk.Frame(body, bg=Theme.BORDER, height=1).pack(
            fill=tk.X, padx=pad, pady=(12, 4))
        self._map_enabled_var = tk.BooleanVar(value=False)
        auto_wrap(TermCheckbutton(body, "Post a map image periodically",
                                  self._map_enabled_var, font_spec=F_NORMAL)
                  ).pack(fill=tk.X, padx=pad, pady=(4, 0))
        label("MAP SOURCE:", pady=(8, 1))
        self._map_source_var = tk.StringVar(value=_SOURCE_LABELS[0])
        source_menu = ttk.Combobox(body, textvariable=self._map_source_var,
                                   values=_SOURCE_LABELS, state="readonly",
                                   width=40, font=F_SMALL,
                                   style="Term.TCombobox")
        source_menu.pack(anchor=tk.W, padx=pad)
        source_menu.bind("<<ComboboxSelected>>",
                         lambda _e: self._sync_map_source())

        map_int_row = tk.Frame(body, bg=Theme.BG_PANEL)
        map_int_row.pack(fill=tk.X, padx=pad, pady=(10, 2))
        tk.Label(map_int_row, text="POST MAP EVERY:", fg=Theme.AMBER_DIM,
                 bg=Theme.BG_PANEL, font=F_SMALL).pack(side=tk.LEFT)
        self._map_interval_var = tk.StringVar(value="60")
        TermEntry(map_int_row, textvariable=self._map_interval_var, width=6,
                  font_spec=F_NORMAL).pack(side=tk.LEFT, padx=6, ipady=2)
        tk.Label(map_int_row, text="min", fg=Theme.MUTED, bg=Theme.BG_PANEL,
                 font=F_SMALL).pack(side=tk.LEFT)
        flow_children(map_int_row, spacing=6)
        hint("While the server runs. Skipped when nothing has changed.")

        # Only one of these two boxes is shown, per the source picked.
        self._map_boxes_anchor = tk.Frame(body, bg=Theme.BG_PANEL)
        self._map_boxes_anchor.pack(fill=tk.X)

        # -- built-in renderer
        sg = tk.Frame(self._map_boxes_anchor, bg=Theme.BG_PANEL)
        self._savegame_box = sg
        hint("Renders the live savegame with the same shaded-relief "
             "renderer as the WORLD MAP tab — no mod needed. Uses the "
             "newest .vcdbs in the world folder set in SETTINGS. Reading "
             "is safe while the server runs; big worlds take a little "
             "while to render.", pady=(6, 0), parent=sg)
        self._map_mark_var = tk.BooleanVar(value=True)
        auto_wrap(TermCheckbutton(sg, "Mark online players (at their "
                                      "last-saved position)",
                                  self._map_mark_var, font_spec=F_SMALL)
                  ).pack(fill=tk.X, padx=pad, pady=(6, 0))

        # -- map-mod image
        fb = tk.Frame(self._map_boxes_anchor, bg=Theme.BG_PANEL)
        self._file_box = fb
        hint("Point this at the image file your map mod exports, or at "
             "the folder it writes to (the newest image there is used). "
             "PNG/JPG/WEBP/GIF, max 10 MB.", pady=(6, 0), parent=fb)

        label("MAP IMAGE FILE OR FOLDER:", parent=fb)
        map_row = tk.Frame(fb, bg=Theme.BG_PANEL)
        map_row.pack(fill=tk.X, padx=pad)
        self._map_path_var = tk.StringVar()
        TermEntry(map_row, textvariable=self._map_path_var,
                  font_spec=F_NORMAL).pack(side=tk.LEFT, fill=tk.X,
                                           expand=True, ipady=3)
        TermButton(map_row, "File…", self._on_browse_map_file,
                   variant="amber", font_spec=F_SMALL,
                   padx=6, pady=3).pack(side=tk.LEFT, padx=(4, 0))
        TermButton(map_row, "Folder…", self._on_browse_map_folder,
                   variant="amber", font_spec=F_SMALL,
                   padx=6, pady=3).pack(side=tk.LEFT, padx=(4, 0))

        label("EXPORT COMMAND  (optional — sent to the console first):",
              parent=fb)
        self._map_cmd_var = tk.StringVar()
        TermEntry(fb, textvariable=self._map_cmd_var,
                  font_spec=F_NORMAL).pack(fill=tk.X, padx=pad, ipady=3)
        wait_row = tk.Frame(fb, bg=Theme.BG_PANEL)
        wait_row.pack(fill=tk.X, padx=pad, pady=(6, 2))
        tk.Label(wait_row, text="THEN WAIT:", fg=Theme.AMBER_DIM,
                 bg=Theme.BG_PANEL, font=F_SMALL).pack(side=tk.LEFT)
        self._map_wait_var = tk.StringVar(value="30")
        TermEntry(wait_row, textvariable=self._map_wait_var, width=6,
                  font_spec=F_NORMAL).pack(side=tk.LEFT, padx=6, ipady=2)
        tk.Label(wait_row, text="s", fg=Theme.MUTED, bg=Theme.BG_PANEL,
                 font=F_SMALL).pack(side=tk.LEFT)
        flow_children(wait_row, spacing=6)
        hint("Time for the export to finish before uploading. Use your map "
             "mod's export command if it only renders on demand; leave it "
             "blank if the mod keeps the image up to date on its own.",
             parent=fb)
        self._sync_map_source()

        # Buttons + result line
        btn_row = tk.Frame(body, bg=Theme.BG_PANEL)
        btn_row.pack(fill=tk.X, padx=pad, pady=(12, 4))
        TermButton(btn_row, "💾 Save", self._on_save,
                   variant="start", font_spec=self._app.F_BTN,
                   padx=10, pady=4).pack(side=tk.LEFT, padx=(0, 4))
        TermButton(btn_row, "▶ Post Now", self._on_post_now,
                   variant="amber", font_spec=self._app.F_BTN,
                   padx=10, pady=4).pack(side=tk.LEFT, padx=(0, 4))
        TermButton(btn_row, "🗺 Post Map Now", self._on_post_map_now,
                   variant="amber", font_spec=self._app.F_BTN,
                   padx=10, pady=4).pack(side=tk.LEFT, padx=(0, 4))
        TermButton(btn_row, "New Messages", self._on_new_message,
                   variant="amber", font_spec=F_SMALL,
                   padx=10, pady=4).pack(side=tk.LEFT)
        flow_children(btn_row, spacing=4)
        hint("Post Now / Post Map Now save first. New Messages forgets the "
             "status and map messages being edited, so the next updates "
             "post fresh ones.")

        self._result_var = tk.StringVar(value="")
        self._result_label = auto_wrap(tk.Label(
            body, textvariable=self._result_var, fg=Theme.MUTED,
            bg=Theme.BG_PANEL, font=F_SMALL))
        self._result_label.pack(fill=tk.X, padx=pad, pady=(6, 12))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _map_source(self) -> str:
        return _LABEL_TO_SOURCE.get(self._map_source_var.get(), "savegame")

    def _sync_map_source(self) -> None:
        """Show the settings box for the selected map source only."""
        file_mode = self._map_source() == "file"
        show, hide = ((self._file_box, self._savegame_box) if file_mode
                      else (self._savegame_box, self._file_box))
        hide.pack_forget()
        show.pack(fill=tk.X)

    def _toggle_url_visible(self) -> None:
        self._url_entry.configure(show="" if self._show_url_var.get() else "•")

    def _int_field(self, var: tk.StringVar, name: str, lo: int) -> int | None:
        try:
            n = int((var.get() or "").strip())
        except ValueError:
            self._app._notify(f"{name} must be a whole number.", level="error")
            return None
        if n < lo:
            self._app._notify(f"{name} must be at least {lo}.", level="error")
            return None
        return n

    def _collect(self) -> dict | None:
        """Editor state → config dict, or None (with a toast) if invalid."""
        interval = self._int_field(self._interval_var, "Refresh interval", 0)
        if interval is None:
            return None
        map_interval = self._int_field(self._map_interval_var,
                                       "Map interval", 1)
        if map_interval is None:
            return None
        map_wait = self._int_field(self._map_wait_var, "Map export wait", 0)
        if map_wait is None:
            return None
        map_path = self._map_path_var.get().strip()
        source = self._map_source()
        if self._map_enabled_var.get() and source == "file" and not map_path:
            self._app._notify("Pick a map image file or folder first.",
                              level="error")
            return None
        url = self._url_var.get().strip()
        if url or self._enabled_var.get():
            ok, reason = validate_webhook_url(url)
            if not ok:
                self._app._notify(f"Webhook: {reason}", level="error",
                                  duration_ms=4000)
                return None
        saved = self._load_config()
        return {
            "enabled":               self._enabled_var.get(),
            "webhook_url":           url,
            "address_override":      self._addr_var.get().strip(),
            "update_mode":           _LABEL_TO_MODE.get(self._mode_var.get(),
                                                        "edit"),
            "interval_mins":         interval,
            "post_on_start":         self._on_start_var.get(),
            "post_on_stop":          self._on_stop_var.get(),
            "post_on_player_change": self._on_players_var.get(),
            "show_player_names":     self._names_var.get(),
            "map_enabled":           self._map_enabled_var.get(),
            "map_source":            source,
            "map_mark_players":      self._map_mark_var.get(),
            "map_path":              map_path,
            "map_interval_mins":     map_interval,
            "map_command":           self._map_cmd_var.get().strip(),
            "map_command_wait_secs": map_wait,
            # A message id only means something for the webhook that
            # posted it — drop both when the URL changes.
            "message_id": (saved["message_id"]
                           if url == saved["webhook_url"] else ""),
            "map_message_id": (saved["map_message_id"]
                               if url == saved["webhook_url"] else ""),
            "forum_post_title":      self._forum_title_var.get().strip(),
            "forum_tag_ids":         ", ".join(
                parse_tag_ids(self._forum_tags_var.get())),
            "forum_thread_id": (saved["forum_thread_id"]
                                if url == saved["webhook_url"] else ""),
        }

    def _on_save(self, quiet: bool = False) -> bool:
        cfg = self._collect()
        if cfg is None:
            return False
        self._save_config(cfg)
        if not quiet:
            self._app._notify("Discord settings saved.", level="success",
                              duration_ms=1800)
        self._app._discord_config_changed()
        return True

    def _on_post_now(self) -> None:
        if not self._on_save(quiet=True):
            return
        if not self._url_var.get().strip():
            self._app._notify("Paste a webhook URL first.", level="warn")
            return
        self._result_var.set("Posting…")
        self._result_label.configure(fg=Theme.MUTED)
        self._app._discord_post_now()

    def _on_post_map_now(self) -> None:
        if not self._on_save(quiet=True):
            return
        if not self._url_var.get().strip():
            self._app._notify("Paste a webhook URL first.", level="warn")
            return
        cfg = self._load_config()
        if cfg["map_source"] == "file" and not cfg["map_path"]:
            self._app._notify("Pick a map image file or folder first.",
                              level="warn")
            return
        if cfg["map_source"] == "savegame":
            self._result_var.set("Rendering the savegame and uploading…")
        elif cfg["map_command"] and self._app.is_running:
            self._result_var.set(f"Running export command, uploading in "
                                 f"{cfg['map_command_wait_secs']} s…")
        else:
            self._result_var.set("Uploading map…")
        self._result_label.configure(fg=Theme.MUTED)
        self._app._discord_post_map_now()

    def _on_browse_map_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self._parent, title="Choose map image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.gif"),
                       ("All files", "*.*")])
        if path:
            self._map_path_var.set(path)

    def _on_browse_map_folder(self) -> None:
        path = filedialog.askdirectory(
            parent=self._parent, title="Choose the folder your map mod "
                                       "exports images to")
        if path:
            self._map_path_var.set(path)

    def _on_new_message(self) -> None:
        cfg = self._load_config()
        cfg["message_id"] = ""
        cfg["map_message_id"] = ""
        cfg["forum_thread_id"] = ""
        self._save_config(cfg)
        self._app._discord_worker.forget_thread()
        self._show_forum_status(cfg)
        self._app._notify("Next updates will post new messages.",
                          level="info", duration_ms=2200)

    def _on_detect_ip(self) -> None:
        self._detected_var.set("Detecting…")
        self._app._discord_detect_ip()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load_config(self) -> dict:
        from core.settings import load_discord_config
        return load_discord_config(self._app._settings)

    def _save_config(self, cfg: dict) -> None:
        from core.settings import save_discord_config, save_settings
        save_discord_config(self._app._settings, cfg)
        save_settings(self._app._settings)
