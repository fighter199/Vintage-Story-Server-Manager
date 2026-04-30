# VSSM — Vintage Story Server Manager

A modular Python/Tkinter app for running and operating a Vintage Story
dedicated server: start/stop/restart, live console, mods management
with ModDB integration, scheduled restarts, world backups, custom chat
commands, interval-based autorun jobs, per-group chat archive, and
session/lifetime playtime tracking.

## Running

```bash
cd vssm
python VSSM.py                  # normal launch  (use `py -3 VSSM.py` on Windows)
python VSSM.py --log-level DEBUG
python run_tests.py             # 300+ tests, no pytest needed
```

## Installing dependencies

VSSM runs on the Python standard library alone — the entries below are
**optional** quality-of-life upgrades. The recommended install is:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If you'd rather keep these libraries out of your global site-packages, use
a virtual environment:

```bash
python -m venv .venv
# Windows (PowerShell):     .venv\Scripts\Activate.ps1
# macOS / Linux:            source .venv/bin/activate
python -m pip install -r requirements.txt
python VSSM.py
```

What each one does:

- **psutil** — per-process CPU + RAM readings in the resource panel.
- **packaging** — strictly correct SemVer / PEP 440 ordering for mod versions.

`requirements.txt` has the full per-platform install notes, including
distro-specific `python3-tk` instructions for Linux.

## Module layout

```
vssm/
├── VSSM.py                 ← entry point + ServerManagerApp class
├── run_tests.py            ← pytest-free test runner
├── requirements.txt
├── README.md
├── core/
│   ├── constants.py        APP_NAME, APP_VERSION, logging, OPERATOR_ROLES,
│   │                       SETTINGS_SCHEMA_VERSION (currently 7)
│   ├── parsers.py          classify_line, parse_player_event (handles
│   │                       multi-line /list clients + got-removed),
│   │                       parse_chat_message (both VS chat formats),
│   │                       parse_cron_expr, version_is_newer,
│   │                       parse_json5_ish
│   ├── settings.py         load/save/migrate (atomic + pre-migration .bak),
│   │                       per-profile rules, autorun, player totals,
│   │                       chat history paths, import/export helpers
│   ├── custom_commands.py  ChatCommandDispatcher engine, AuditRecord,
│   │                       cooldown tracker, destructive-keyword guard
│   ├── autorun.py          AutorunScheduler engine, AutorunAudit,
│   │                       fire_now (Run Now / Run on save), rule
│   │                       normalization + validation
│   ├── chat_log.py         ChatLogStore (per-group ring buffer + group
│   │                       names), parse_chat_with_group
│   ├── player_timers.py    PlayerTimers (session + lifetime tracking),
│   │                       fmt_duration
│   └── utils.py            port check, backup zip + testzip,
│                           clean_mod_filename, fmt_size, DPI awareness
├── ui/
│   ├── theme.py            Theme presets (amber / green / cyan / dark / custom)
│   ├── widgets.py          TermButton, TermEntry, Sparkline, ScrollableFrame,
│   │                       ToastQueue (queued, non-overlapping toasts)
│   ├── tab_custom_commands.py   CUSTOM CMDS tab — full editor + audit panel
│   ├── tab_autorun.py      AUTORUN tab — interval-based command scheduler
│   │                       with Run-on-save + Run-Now
│   ├── tab_chat_log.py     CHAT LOG tab — per-group subtabs + history
│   ├── tab_mods.py         Mods tab + ModDB browser (1,500 lines)
│   ├── tab_commands.py     COMMANDS tab — VS command reference
│   ├── tab_settings.py     SETTINGS tab — paths, scheduling, theme
│   ├── tab_backup.py       BACKUP tab
│   ├── tab_config.py       CONFIG editor
│   └── tab_custom_theme.py CUSTOM THEME color picker
├── backup/
│   └── manager.py          BackupManager — async zip + retention
├── mods/
│   ├── inspector.py        LocalModInspector (modinfo from zip/dir/cs/dll)
│   └── moddb.py            ModDbClient (ModDB REST API, stdlib only)
└── tests/                  300+ tests across 7 test modules
    ├── conftest.py
    ├── test_parsers.py     log-line classification, chat formats,
    │                       multi-line /list clients, got-removed,
    │                       cron, version compare
    ├── test_custom_commands.py   trigger matching, args, cooldowns,
    │                             roles, destructive-guard, audit hooks
    ├── test_autorun.py     interval scheduling, run-on-start,
    │                       pause-when-empty, multi-line commands,
    │                       live rule edits, fire_now (Run Now /
    │                       Run on save), audit emission
    ├── test_chat_log.py    parsing with group ID, ring-buffer cap,
    │                       group naming + persistence round-trip,
    │                       all-entries chronological merge
    ├── test_player_timers.py   session/total accumulation,
    │                           flush-without-double-counting,
    │                           reset_all, fmt_duration
    ├── test_settings.py    schema migration v1 → v7, per-profile
    │                       rules + autorun + player_totals,
    │                       import/export, atomic save
    └── test_utils.py       fmt_size, sanitize, mod-filename cleaning
```

## The CUSTOM CMDS tab

Reachable from the sidebar as **CUSTOM CMDS**. Each rule has:

- **Trigger** — text the player types in chat (e.g. `!warp`).
  Matched case-insensitively at a word boundary, so `!warp` triggers on
  `!warp`, `!warp spawn`, `!WARP foo` — but not on `!warpzone` or
  `hello !warp`.
- **Allowed roles** — checkbox chips for `suplayer`, `suadmin`, `admin`,
  `operator`, `guest`, plus a free-text "extra roles" field. An empty
  selection means *anyone*.
- **Cooldown** — seconds between fires per (rule, player). One player
  spamming `!warp` can't lock another player out.
- **Response** — one or more console commands (one per line). Supported
  placeholders, expanded at dispatch time:

  - `{player}` — the speaker's name
  - `{role}` — the speaker's role (lowercase)
  - `{1}` … `{9}` — the 1st through 9th whitespace-separated argument
    after the trigger
  - `{target}` — alias for `{1}`
  - `{args}` — all arguments joined by a single space

- **Destructive-action confirmation** — if the response contains
  `/stop`, `/ban`, `/op`, or any of the other gated keywords, the rule
  won't fire until you tick "I understand this is destructive". This
  is checked both at validation time *and* at dispatch time, so a typo
  in the role list can't accidentally let a guest run `/stop`.
- **Enabled** toggle — disable a rule without deleting it.

### Live trigger test

The editor includes a "Live trigger test" panel. Type a sample player
name, role, and chat message, and you'll see exactly what would happen:
which commands fire, or why the rule was rejected (cooldown, role,
disabled, missing args, …).

### Audit log

The bottom of the tab has a collapsible **Recent triggers** panel
showing the last 80 dispatch events:

```
[14:23:01] FIRED  Steve(admin): !warp
          → /tp Steve 0 150 0
[14:23:09] COOLDOWN  Steve: !warp (22.0s left)
[14:24:15] DENIED    Bob(guest): !day — role not allowed
[14:25:30] BLOCKED   Carol: !off — destructive (unconfirmed)
```

Every fire is also written to `logs/vserverman.log` with structured
fields (player, role, message, expanded command).

### Import / export

Two buttons at the top of the tab let you save the whole rule set to a
JSON file or load one from disk. On import you can choose to merge
with the existing rules or replace them entirely. The import path
runs every rule through `validate_rule`, so a malformed file is rejected
with a clear error before anything is written.

### Per-profile rules

Each profile owns its own rule list. Switching profiles in Settings
swaps the active rule set instantly — useful for running e.g. a
`creative` profile with `!gm 1` and a `survival` profile without it.

### Rule schema

```json
{
  "trigger":               "!warp",
  "response":              "/tp {player} 0 150 0",
  "roles":                 ["suplayer", "admin"],
  "enabled":               true,
  "cooldown_secs":         30,
  "confirmed_destructive": false
}
```

## The AUTORUN tab

Reachable from the sidebar as **AUTORUN**. Where Custom Commands react
to chat input, Autorun rules fire on a fixed interval while the server
is running. Useful for periodic saves, hourly announcements, automatic
day-skipping, that sort of thing.

Each rule has:

- **Name** — label shown in the rule list. Also the dedup key, so two
  rules can't share a name.
- **Enabled** — toggle without deleting.
- **Interval** — value + unit (seconds / minutes / hours).
- **Commands** — multi-line text, one console command per line. Lines
  starting with `#` are comments and ignored at dispatch time.
- **Run once on server start** — fire immediately when the server comes
  up, then continue on the regular interval.
- **Run on save** — fire whenever you click 💾 Save, AND reset the
  next-fire deadline so the periodic cadence re-anchors to save-time.
  Useful for "I just edited a /broadcast text — run it now to verify,
  and re-anchor the every-15-min cadence."
- **Pause when 0 players online** — skip the tick instead of double-
  firing later. If the server sits empty for an hour, you get **one**
  fire when a player comes back, not 12 backlogged ones.

Rules are per-profile, persisted in the same JSON store as everything
else. The audit strip across the bottom of the tab shows recent fires
and the reason for any skipped ones (`disabled`, `paused_empty`,
`not_running`).

### ▶ Run Now

The editor has a **▶ Run Now** button next to **💾 Save**. It fires
the *saved* state of the selected rule once, immediately, and resets
the next scheduled fire to `interval_secs` from now. Useful for testing
a newly-edited rule without waiting for the next tick.

If you have unsaved edits in the editor when you click Run Now, you
get a toast warning: "Running the SAVED version (unsaved edits
ignored)" — that prevents the surprise of clicking Run Now on a rule
you just edited and getting old behaviour. To run your edits, save
with the **Run on save** checkbox enabled.

Both Run Now and Run-on-save respect every existing gate (`enabled`,
`pause_when_empty`). A blocked fire emits the matching audit record
but **still resets the schedule** — pressing the button is an explicit
"re-anchor cadence to now" intent, separate from whether this
particular fire could go through.

### How the scheduler works

A single `AutorunScheduler` lives on `ServerManagerApp`. It's started
when the server transitions to running and stopped on shutdown, so all
interval deadlines anchor to a fresh server-start wall clock — they
don't drift across restarts. A 1Hz tick (`_tick_autorun`) drives the
scheduler while the server is up; rule deadlines are reread from
settings every tick, so saving a rule in the editor takes effect on
the very next tick without any reattach plumbing.

### Rule schema

```json
{
  "name":             "Hourly save",
  "enabled":          true,
  "interval_secs":    3600,
  "commands":         "/autosavenow",
  "run_on_start":     false,
  "run_on_save":      false,
  "pause_when_empty": false
}
```

## The CHAT LOG tab

Reachable from the sidebar as **CHAT LOG**. Vintage Story 1.20+ tags
every chat line with a group ID:

    [Server Chat] 0  | DerelictDawn: Oh
    [Server Chat] 10 | Fighter199: testing chat long

Group `0` is general (everyone). Other IDs are private/named groups
the server creates when players form a chat circle. The CHAT LOG tab
captures and persists all of it.

### What's in each subtab

One subtab per chat group seen on the server, plus an **All** subtab
that merges everything chronologically. Each per-group tab shows lines
as they arrive; the All tab adds a group label so you can tell where
each line came from:

```
[00:34:32] [Builders chat] Fighter199: testing chat long
[00:35:21] [General] Fighter199: this is general chat
```

### Naming groups

Group `0` shows as "General" by default; everything else shows as
"Group N" until you give it a name. Two ways to rename: right-click
any group tab, or click the rule and use the **✎ Rename group**
button in the toolbar. Names persist across restarts. Setting the
name to blank resets to the default.

### Toolbar

- **✎ Rename group** — rename the currently-selected tab.
- **🗑 Clear group** — drop history for this group only (name kept).
- **🗑 Clear all** — drop history for all groups (names kept).

### Persistence

Chat history is stored per profile, in
`chat_log_<profile>.json` next to your `settings.json`. The store
caps each group at 500 lines (oldest evicted) so the file stays small
even on busy servers. Saved on:

- Rename / clear actions (immediate)
- VSSM exit (final flush from `on_closing`)

Profile switches load that profile's chat archive automatically.

### Read-only

This tab only reads chat. Typing messages into a group from VSSM is
out of scope; the console-input field at the bottom of the main
window still works for sending raw console commands.

## Player timers

The player list (in the right sidebar) now shows two timers next to
each name:

    [AL]  Alice  [admin]                    🕐 0:32:15   Σ 14:22:40

- **🕐 H:MM:SS** — current session (resets on leave/rejoin).
- **Σ H:MM:SS** — total across all sessions; persisted across both
  VSSM and server restarts.

Totals live in the active profile's settings under `player_totals`
(plain `{"name": seconds}` dict). A 60-second flush tick accumulates
in-flight session time into the persisted totals so a hard crash loses
at most ~60s of playtime. Additional flushes happen on every player
leave, on `_finalize_stop`, and on `on_closing`.

The 1 Hz UI tick updates the labels in place (no row destroy/recreate)
so there's no flicker even with many players online.

## Settings tab highlights

- **Collapsible header** — the big top header (full title, version,
  hotkey cheat sheet) collapses into a compact toolbar strip via the
  **▾/▸** toggle. State persists between sessions. If a setup warning
  appears while the header is collapsed, it auto-expands once so the
  full message is visible.
- **Paths** — Mods folder, world folder, backup destination. Each row
  has a Browse button and a 📂 Open button that reveals the folder in
  the OS file manager (Explorer / Finder / xdg-open).
- **Recurring restart schedule** — wall-clock cron-style entries. Use
  24-hour `HH:MM`, optionally prefixed by a weekday (`mon`, `tue`,
  `wed`, `thu`, `fri`, `sat`, `sun`). Multiple entries separated by
  commas or semicolons. Validated live; the next-fire ETA is shown
  next to the entry box. Examples:
    - `06:00` → every day at 6 AM
    - `06:00, 18:00` → every day at 6 AM and 6 PM
    - `mon 04:00; fri 22:30` → Mondays 4 AM, Fridays 10:30 PM
- **Player count poll interval** — how often VSSM sends `/list clients`
  to refresh the connected-player list while the server is running.
  Default 30s; 0 disables. The /list response also acts as a
  fail-safe for the player tracker (see "Hooking into the server log"
  below).
- **Crash-loop threshold** — how many crashes within how many seconds
  trigger an auto-restart shutoff. Default 3 in 600s.
- **Auto-save** — interval-based `/autosavenow` plus optional pre-start
  / pre-stop world backups.
- **CRT theme** — amber, green, cyan, dark, or fully custom (the
  CUSTOM THEME tab is a per-color picker that writes to settings).

## Hooking into the server log

`core/parsers.py` handles every shape of line VSSM cares about. The
parser is pure (no I/O, no Tk), and lines flow through it from the
output-queue processor on the Tk main thread.

### Chat lines

Two parser functions, run in parallel on every chat line:

- `parse_chat_message(line)` → `(player, message)`. Used by the
  Custom Commands dispatcher. Handles **both** historical formats:
  the older Minecraft-style `[Server Chat] <Alice> hi` and the
  current VS 1.20+ `[Server Chat] 0 | Alice: hi`. The colon form
  requires a leading `<digits> |` group prefix to guard against
  false positives on notification lines.

- `parse_chat_with_group(line)` → `(group_id, player, message)`. Used
  by the Chat Log tab; only matches the colon form (which is the only
  shape that carries a group ID).

When `_handle_server_line()` classifies a line as `chat`, it dispatches
through the custom-commands engine **and** appends to the chat-log
store, in that order. Each path is independent — a chat line that
fires a custom command also lands in the chat archive.

### Player events

`parse_player_event(line)` recognises:

- **Joins** — `Player 'X' has joined the game`, `[Audit] X joined`,
  `X [ip]:port joins`, plus a few defensive variants.
- **Leaves** — `Player X left.`, `Player 'X' has left the game`,
  `[Audit] Client X disconnected`, `Client X disconnected`,
  and `Player X got removed.` (a phrasing some VS builds use).
- **List headers** — `List of online Players` opens a multi-line
  accumulation block.
- **List entries** — `Playing [N] Name [ip]:port (...)` rows.

The host code maintains a 1-second buffer for the multi-line list
block, then flushes through `_sync_players_from_list`, which does
**bidirectional sync**: any name in the list that isn't tracked is
added (with role lookup queued); any currently-tracked name not in the
list is removed (with timer accumulation). This is the fail-safe that
recovers from missed leave events — even if a leave message slips
through somehow, the next /list clients sync catches up.

The /list output uses a `[HH:MM:SS]` console-time prefix that the
existing parser also strips, in addition to the `D.M.YYYY` and ISO-style
timestamps.

## Windows-specific note: console attachment and stdin

On Windows, `VintagestoryServer.exe` reads commands from the parent's
console by default. If we just redirect stdin via a pipe and let the
child inherit our console, VS reads from the real console and ignores
our piped bytes — so `/stop`, `/list clients`, `/announce`, every
custom command we send, all silently disappear.

The fix in `start_server` is to launch the server with two Win32
creation flags:

- `CREATE_NEW_PROCESS_GROUP` (`0x00000200`) — detaches from our
  console group so a Ctrl-C in our window can't propagate to the
  server.
- `CREATE_NO_WINDOW` (`0x08000000`) — suppresses the child's own
  console window.

With no console attached, VS falls back to reading stdin, which is the
pipe we own. On non-Windows platforms these flags are not used.

## Tests

The test suite covers the pure logic modules (parsers, custom-commands
engine, autorun scheduler, chat-log store, player-timer engine,
settings layer, utility helpers). UI code is intentionally not
exercised — Tk testing is fragile and slow, and the value-per-line is
much higher in the engine.

```bash
# Without pytest (pure stdlib)
python3 run_tests.py

# Or with pytest, if you have it
pytest tests/ -v
```

The tests are written in pytest style (classes + `test_*` methods,
fixture for tmp settings dir, `pytest.raises`) and the in-tree runner
provides just enough of the pytest surface to execute them.

## Settings schema versions

Schema migrations run automatically on first launch after an upgrade.
A timestamped `.bak` is written next to `vserverman_settings.json`
before any migration mutates the file.

| Version | Change |
|---------|--------|
| v1 | Initial flat key/value layout |
| v2 | Multi-profile support (`active_profile`, `profiles{}`) |
| v3 | Top-level `custom_commands` list |
| v4 | `custom_commands` promoted to per-profile |
| v5 | Added `player_count_poll_secs` (default 30) |
| v6 | Added per-profile `autorun_rules` list |
| v7 | Added per-profile `player_totals` dict (lifetime playtime) |

The current schema constant is `SETTINGS_SCHEMA_VERSION` in
`core/constants.py`.

Chat history lives **outside** settings.json, in
`chat_log_<profile>.json` next to it — separate file per profile so
the main settings blob doesn't bloat as messages accumulate.
