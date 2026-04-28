# VSSM — Vintage Story Server Manager

A modular Python/Tkinter app for running and operating a Vintage Story
dedicated server: start/stop/restart, live console, mods management
with ModDB integration, scheduled restarts, world backups, custom chat
commands, and interval-based autorun jobs.

## Running

```bash
cd vssm
python VSSM.py                  # normal launch  (use `py -3 VSSM.py` on Windows)
python VSSM.py --log-level DEBUG
python run_tests.py             # 219-test suite, no pytest needed
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
│   │                       SETTINGS_SCHEMA_VERSION (currently 6)
│   ├── parsers.py          classify_line, parse_player_event,
│   │                       parse_chat_message (handles both VS chat
│   │                       formats), parse_cron_expr, version_is_newer,
│   │                       parse_json5_ish
│   ├── settings.py         load/save/migrate (atomic + pre-migration .bak),
│   │                       per-profile rules + autorun lists,
│   │                       import/export helpers
│   ├── custom_commands.py  ChatCommandDispatcher engine, AuditRecord,
│   │                       cooldown tracker, destructive-keyword guard
│   ├── autorun.py          AutorunScheduler engine, AutorunAudit,
│   │                       rule normalization + validation
│   └── utils.py            port check, backup zip + testzip,
│                           clean_mod_filename, fmt_size, DPI awareness
├── ui/
│   ├── theme.py            Theme presets (amber / green / cyan / dark / custom)
│   ├── widgets.py          TermButton, TermEntry, Sparkline, ScrollableFrame,
│   │                       ToastQueue (queued, non-overlapping toasts)
│   ├── tab_custom_commands.py   CUSTOM CMDS tab — full editor + audit panel
│   ├── tab_autorun.py      AUTORUN tab — interval-based command scheduler
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
└── tests/                  219 tests across 5 test modules
    ├── conftest.py
    ├── test_parsers.py     log-line classification, chat format
    │                       detection, cron, version compare
    ├── test_custom_commands.py   trigger matching, args, cooldowns,
    │                             roles, destructive-guard, audit hooks
    ├── test_autorun.py     interval scheduling, run-on-start,
    │                       pause-when-empty, multi-line commands,
    │                       live rule edits, audit emission
    ├── test_settings.py    schema migration v1 → v6, per-profile
    │                       rules + autorun, import/export, atomic save
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
- **Pause when 0 players online** — skip the tick instead of double-
  firing later. If the server sits empty for an hour, you get **one**
  fire when a player comes back, not 12 backlogged ones.

Rules are per-profile, persisted in the same JSON store as everything
else. The audit strip across the bottom of the tab shows recent fires
and the reason for any skipped ones.

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
  "pause_when_empty": false
}
```

## Settings tab highlights

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
  Default 30s; 0 disables.
- **Crash-loop threshold** — how many crashes within how many seconds
  trigger an auto-restart shutoff. Default 3 in 600s.
- **Auto-save** — interval-based `/autosavenow` plus optional pre-start
  / pre-stop world backups.
- **CRT theme** — amber, green, cyan, dark, or fully custom (the
  CUSTOM THEME tab is a per-color picker that writes to settings).

## Hooking into the server log

`core/parsers.py::parse_chat_message` parses Vintage Story chat lines.
The parser handles **both** historical formats:

- Older Minecraft-style: `[Server Chat] <Alice> !warp spawn`
- Current VS 1.20+: `[Server Chat] 0 | Alice: !warp spawn` — where
  `0` is the chat group ID and the colon-form has no angle brackets.

The colon form requires the `<digits> |` group prefix, which guards
against false positives where `[Server Notification] Game Version: …`
would otherwise parse as player "Version" speaking.

When `_handle_server_line()` classifies a line as `chat`, the parsed
`(player, message)` is dispatched through `ChatCommandDispatcher`,
which checks the speaker's role (populated from `/player NAME role`
responses) against each enabled rule and emits the resulting console
commands via the existing `_send_internal_command()` path.

Audit records are routed through `app.after_idle` so the UI updates
always happen on the Tk main thread, regardless of which thread the
dispatch was originally invoked from.

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
engine, autorun scheduler, settings layer, utility helpers). UI code
is intentionally not exercised — Tk testing is fragile and slow, and
the value-per-line is much higher in the engine.

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

The current schema constant is `SETTINGS_SCHEMA_VERSION` in
`core/constants.py`.
