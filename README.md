# VSSM — Vintage Story Server Manager

A desktop control panel for running a dedicated
[Vintage Story](https://www.vintagestory.at/) server. Pure Python +
Tkinter, styled as an amber CRT terminal, with zero required
dependencies — download, point it at `VintagestoryServer.exe`, press
**▶ Start**.

Current version: **3.1**

## Feature highlights

- **Server lifecycle** — start / stop / restart with graceful `/stop`,
  bounded shutdown timeout, force-kill fallback, and a crash-loop
  breaker for auto-restart. Restarts wait for the savegame database to
  actually release its file lock before relaunching, so a forced stop
  can't crash the next boot.
- **Live console** — colour-classified server output with filter bar,
  command history, broadcast row, right-click copy, and a scrollback
  cap so week-long sessions don't eat RAM.
- **Safe backups** — consistent live backups via the server's own
  `/genbackup`, separate filename families for manual / pre-start /
  stop backups, each with independent retention. Restores are staged
  through a temp dir so a corrupt zip can never destroy the current
  world.
- **Player tools** — live player list with roles, session + lifetime
  playtime tracking, kick / ban / op / teleport context menu, and
  player-aware restart/shutdown guards ("wait until empty").
- **Custom chat commands** — players type `!warp`-style triggers in
  chat; VSSM fires console commands with role gating, per-player
  cooldowns, argument capture, and a destructive-command safety latch.
- **Autorun rules** — fire console commands on a fixed interval while
  the server is up (hourly broadcasts, periodic saves), with
  run-on-start / run-on-save / pause-when-empty gates.
- **Chat log** — per-group chat history that persists across launches,
  including a subtab for proximity / roleplay-mod chat.
- **Mod manager** — inspect installed mods, browse the ModDB in-app,
  and run a parallel, cached update check with a per-mod picker,
  game-version compatibility filter, and atomic downloads.
- **Scheduling** — cron-style restart schedule with advance broadcast
  warnings, plus periodic auto-backup.
- **Profiles & themes** — every server-specific setting is
  per-profile; amber / green / cyan / dark / fully-custom colour
  themes; HiDPI-aware UI scaling with hotkeys.

## Requirements

- Python 3.10+ with Tkinter (bundled on Windows/macOS; on Debian/Ubuntu
  `sudo apt install python3-tk`).
- Everything runs on the standard library. Two **optional** packages
  improve quality of life:
  - **psutil** — per-process CPU + RAM readings in the resource panel.
  - **packaging** — strictly correct SemVer ordering for mod-update
    checks (a tested fallback comparator is used without it).

```bash
python -m pip install -r requirements.txt   # optional extras
```

## Running

```bash
python VSSM.py                    # normal launch (py -3 VSSM.py on Windows)
python VSSM.py --log-level DEBUG  # override the persisted log level
python run_tests.py               # full test suite, no pytest needed
```

First launch creates `vserverman_settings.json`, a `logs/` folder, and
(per profile) `chat_log_<profile>.json` next to `VSSM.py`. A
`Release/` folder containing only the runtime files (no tests or
caches) can be produced for deployment.

## Module layout

```
VSSM5/
├── VSSM.py                 entry point + ServerManagerApp (Tk host)
├── run_tests.py            pytest-free test runner (also runs a lint pass)
├── requirements.txt        optional extras + per-platform notes
├── vs_commands.json        command-reference data for the COMMANDS tab
├── core/
│   ├── constants.py        APP_NAME/VERSION, logging bootstrap, OPERATOR_ROLES
│   ├── parsers.py          log-line classification, player events, chat,
│   │                       JSON5-ish parser, cron parsing, version compare
│   ├── settings.py         load/save/migrate (atomic write + pre-migration
│   │                       .bak), per-profile storage helpers
│   ├── chat_log.py         ChatLogStore — per-group ring buffers + persistence
│   ├── custom_commands.py  ChatCommandDispatcher, validation, import/export
│   ├── autorun.py          AutorunScheduler — injectable clock/send, testable
│   ├── player_timers.py    PlayerTimers — session + lifetime playtime
│   └── utils.py            port check, backup/restore zip helpers, DPI
├── ui/                     one module per tab + theme.py + widgets.py
├── backup/
│   └── manager.py          BackupManager — async zip, /genbackup live path,
│                           per-family retention, completion callbacks
├── mods/
│   ├── inspector.py        LocalModInspector (modinfo from zip/dir/cs/dll)
│   ├── moddb.py            ModDbClient (ModDB REST API, atomic downloads)
│   └── moddb_cache.py      on-disk TTL cache for mod lookups
└── tests/                  pytest-style suite for every pure-logic module
```

The engine modules (`core/`, `backup/manager.py`, `mods/`) have no Tk
dependency — time and side-effects are injected, which is what keeps
them unit-testable. UI code lives entirely under `ui/` and `VSSM.py`.

## Backups

### Consistent live backups (/genbackup)

Zipping the world folder while the server is running risks a corrupt
backup: Vintage Story has no "pause saving" command, so the `.vcdbs`
SQLite database can be mid-write (chunk generation, autosave) at the
moment it's copied. VSSM therefore never zips the live database
directly. When a backup fires while the server is up, VSSM:

1. sends the server's own **`/genbackup`** command, which produces a
   *consistent* copy of the live savegame in the server's `Backups`
   folder without pausing play;
2. waits for that copy to finish writing (size stable + no open write
   handle);
3. zips it into the backup destination under the live savegame's own
   filename, so restores round-trip normally;
4. deletes the intermediate copy from the server's `Backups` folder.

If the live savegame can't be identified (zero or multiple `.vcdbs`
files in the world folder) or `/genbackup` produces nothing within
45 s, VSSM falls back to a direct zip and prints a clear
*"Consistency not guaranteed"* warning. With the server stopped, the
plain folder zip is used — the files are quiescent, so that's safe.

### Backup families & retention

Backups are named by what triggered them, and each family is pruned
independently:

| Trigger                    | Filename                      | Retention                          |
|----------------------------|-------------------------------|-------------------------------------|
| Manual / periodic auto     | `backup-<timestamp>.zip`      | *Keep last N* / *Keep last N days* |
| Backup before server start | `startbackup-<timestamp>.zip` | Keep newest N (BACKUP tab)         |
| Backup on server stop      | `stopbackup-<timestamp>.zip`  | Keep newest N (BACKUP tab)         |

The start/stop caps live in the **BACKUP** tab (0 = keep all,
persisted per profile). Start/stop backups never count against the
regular `backup-*` retention, so a nightly restart schedule can't age
out your manual backups.

### Sequencing

- **Backup before start** — the snapshot is taken while the world is
  quiescent and the server launches only after the zip completes.
- **Backup on stop** — the snapshot is taken *after* the process has
  exited (it includes the final world save); restart relaunch and
  window close wait for it to finish.

### Restore

Restores (from the BACKUP tab list or *Restore (browse…)*) extract the
zip to a temp dir and validate it **before** touching the current
world, archive the current world to `pre-restore-<ts>.zip`, then move
the contents into the configured world folder — regardless of what the
folder inside the archive was called, so backups survive world-folder
renames. Bare zips with world files at the archive root are also
accepted. The server must be stopped to restore.

## The Custom Commands tab

Each rule has a **trigger** (matched case-insensitively at a word
boundary — `!warp` fires on `!warp spawn` but not `!warpzone`),
**allowed roles** (empty = anyone), a per-(rule, player) **cooldown**,
and a multi-line **response** of console commands with placeholders
expanded at dispatch time:

| Placeholder    | Expands to                                   |
|----------------|----------------------------------------------|
| `{player}`     | the speaker's name                           |
| `{role}`       | the speaker's role (lowercase)               |
| `{1}` … `{9}`  | Nth whitespace-separated argument            |
| `{target}`     | alias for `{1}`                              |
| `{args}`       | all arguments joined by a space              |

Lines referencing a missing argument are dropped rather than sent
half-expanded. If a response contains a destructive command (`/stop`,
`/ban`, `/op`, …) the rule won't fire until the *"I understand this is
destructive"* box is ticked — checked at validation **and** dispatch
time. The tab includes a live trigger tester, a recent-triggers audit
panel, and JSON import/export (imports are validated rule-by-rule and
refuse settings dumps).

Rule schema:

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

## The Autorun tab

Each rule fires its console commands every N seconds while the server
runs. Lines starting with `#` are comments. Gates: **Run on start**
(fire once at server start), **Run on save** (fire once when saved,
re-anchoring the cadence), **Pause when empty** (skip ticks with 0
players online — the schedule keeps advancing). **▶ Run Now** fires
the saved rule immediately and resets its interval. An audit strip
shows every fire/skip decision with the reason. Intervals are measured
from server start, so they don't drift across restarts.

```json
{
  "name":             "Hourly broadcast",
  "enabled":          true,
  "interval_secs":    3600,
  "commands":         "/announce Welcome!\n# comment\n/autosavenow",
  "run_on_start":     true,
  "run_on_save":      false,
  "pause_when_empty": true
}
```

## The CHAT LOG tab

One subtab per chat group ID (`General` = group 0, plus named groups),
each a persisted 500-line ring buffer. Right-click a subtab to rename
or clear it. A separate **Ungrouped / Proximity** subtab captures
roleplay/proximity-mod lines of the shape `Dan mentions "hello"` —
the verb is preserved in the rendered message so the RP flavour stays
visible.

## Players & playtime

Every player row shows **🕐 session** (since their latest join) and
**Σ total** (lifetime, persisted per profile), updated in place at
1 Hz with no row rebuild. Active sessions are flushed to disk every
60 s, so a crash loses at most a minute of playtime. Names are
case-preserved — VS player names are case-sensitive. The list is kept
current by parsing join/leave lines plus a configurable `/list clients`
poll (Settings, 0 disables).

Three optional Settings-tab guards gate restarts/shutdowns against the
live player list: manual restart and manual shutdown show a
*Wait until empty / Continue anyway / Cancel* dialog, while a
cron-fired restart silently defers — broadcasting a heads-up and
polling every 5 s — until the server is empty. All three default off.

## The mod manager

**MODS → INSTALLED** lists local mods (zip/dir/cs/dll are all
inspected), with enable/disable/remove and a jump-to-ModDB button.
**MODS → BROWSE** is an in-app ModDB browser with tag filters, sort
orders, and one-click installs.

**⟳ Check Updates** runs the scan in a thread pool (8 workers) against
`version_is_newer`, honours the GAME VER filter so a 1.21-only release
is never offered to a 1.20 server, and consults a 6-hour on-disk cache
(tick *Force refresh* to bypass). Results open in a picker dialog with
per-mod checkboxes (server/universal mods pre-ticked, client-only mods
off), collapsible buckets for up-to-date / no-compatible-release /
failed, live progress, and a cancel that works mid-download. Downloads
stream to `<dest>.part` and are swapped into place with `os.replace`,
so a failed download never leaves a half-written mod.

## Scheduling

- **Periodic auto-backup** — every N minutes while running (labelled
  "Periodic auto-backup"; uses the consistent `/genbackup` path).
- **Cron-style restarts** — `HH:MM`, `DAY HH:MM`, or comma-separated
  lists (`mon 04:00, thu 04:00`), validated live with a next-fire ETA.
  Players get 5-minute / 1-minute / 10-second broadcast warnings.
- **Auto-restart on crash** — with a breaker: more than N crashes
  (default 3) inside the crash window (default 600 s) disables
  auto-restart until you intervene. Relaunches also wait for the
  savegame lock to clear.

## Settings, profiles & themes

All server-specific settings (paths, backups, guards, custom commands,
autorun rules, player totals) are **per-profile**; switching profiles
swaps everything instantly. Settings writes are atomic (tmp →
`os.replace`), the schema is versioned (currently v7) with automatic
migration and a timestamped pre-migration `.bak`. The persisted
`log_level` is applied at startup; `--log-level` overrides it for one
run.

Themes: amber (default), green, cyan, neutral dark, or a fully custom
palette via the CUSTOM THEME tab. UI scale: `Ctrl +` / `Ctrl -` /
`Ctrl 0`, persisted.

Hotkeys: `Ctrl+L` clear console · `Ctrl+Enter` send · `↑/↓` command
history · `Ctrl+/` focus command entry · right-click console/player
rows for context menus.

## Data files

| File                          | Purpose                                    |
|-------------------------------|---------------------------------------------|
| `vserverman_settings.json`    | all settings, profiles, rules, playtime    |
| `chat_log_<profile>.json`     | per-profile chat history                   |
| `moddb_cache.json`            | TTL cache for mod-update lookups           |
| `logs/vserverman.log`         | application log (rotating)                 |
| `logs/server-output.log`      | mirror of raw server stdout (rotating)     |

Filenames keep the legacy `vserverman` prefix so upgrades never orphan
existing user data.

## Tests

The suite covers every pure-logic module — parsers, custom-commands
engine, autorun scheduler, player timers, settings migration, chat-log
store, backup manager (family pruning, reason prefixes), backup/restore
zip round-trips, and utility helpers. UI code is intentionally not
exercised. 365 tests at the time of writing.

```bash
python run_tests.py            # stdlib-only runner (+ optional ruff/pyflakes lint)
pytest tests/ -v               # or with real pytest
```

## Patcher scripts

Code updates for existing installs ship as `apply_*_patch.py` scripts
rather than replacement files, so local edits survive. Every patcher
is idempotent, verifies each target snippet exactly once before
writing, backs modified files up to timestamped `.bak`s, byte-compiles
the result (rolling back on syntax error), preserves CRLF, and
supports `--dry-run`. Exit codes: `0` applied/already applied, `1` bad
path or missing prerequisite, `2` snippet mismatch (nothing written),
`3` syntax error (rolled back). Always run them from the folder
containing `VSSM.py`.

## Recent changes (July 2026)

- **Consistent live backups** — running-server backups now go through
  `/genbackup` instead of zipping the live database (see Backups).
- **Backup families** — `startbackup-*` / `stopbackup-*` naming with
  independent keep-last-N retention.
- **Backup sequencing** — pre-start backups complete before launch;
  stop backups run after process exit; restart/close wait for them.
- **Safer restores** — staged through a temp dir, tolerant of renamed
  world folders and bare zips.
- **Fixed:** port-in-use check on Windows (`SO_EXCLUSIVEADDRUSE`),
  CPU meter stuck at 0 %, spurious "server exited" after a restart
  (stale reader-queue marker), unbounded console memory, player-row
  label leak, splash screen ignoring the theme preset, persisted
  `log_level` never being applied.
