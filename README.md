# VSSM v3 — Vintage Story Server Manager

A modular rewrite of the v2 monolithic single-file application. The
~7,500-line monolith is now a properly structured package with type
hints, unit tests, a **Custom Commands** tab that lets server owners
define chat triggers (e.g. `!warp`, `!day`, `!give`) gated by player
role and per-player cooldown, an **Autorun** tab that fires console
commands on a fixed interval while the server is up, an in-app
**ModDB** browser with parallel cached update checks, per-player
session/lifetime playtime tracking, and a **CHAT LOG** tab with
proximity / RP-mod chat support.

## How updates work — patchers are the canonical delivery mechanism

> **Code and feature updates are shipped as patcher scripts, not as
> drop-in replacement files.** When a new feature lands you'll get
> an `apply_*_patch.py` script that surgically modifies your existing
> install. Patchers are preferred over file replacement because they
> preserve local edits, are reversible (every modified file gets a
> timestamped `.bak`), and refuse to run if anything looks off.

See the **[Patcher scripts](#patcher-scripts)** section below for the
conventions every patcher follows and the list of patchers shipped in
the current iteration.

## Running

```bash
cd vssm
python VSSM.py                  # normal launch  (use `py -3 VSSM.py` on Windows)
python VSSM.py --log-level DEBUG
python run_tests.py             # full test suite, no pytest needed
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
├── apply_*_patch.py        ← patcher scripts (see Patcher scripts section)
├── core/
│   ├── constants.py        APP_NAME, APP_VERSION, logging, OPERATOR_ROLES
│   ├── parsers.py          classify_line, parse_player_event,
│   │                       parse_chat_message, parse_chat_with_group,
│   │                       parse_ungrouped_chat, parse_cron_expr,
│   │                       version_is_newer, parse_json5_ish
│   ├── settings.py         load/save/migrate (atomic + pre-migration .bak),
│   │                       per-profile rules, import/export helpers
│   ├── chat_log.py         ChatLogStore — per-group ring-buffer + persistence,
│   │                       UNGROUPED_KEY for proximity / RP-mod chat
│   ├── custom_commands.py  ChatCommandDispatcher engine, AuditRecord,
│   │                       cooldown tracker, destructive-keyword guard
│   ├── autorun.py          AutorunScheduler — interval-based command runner,
│   │                       AutorunAudit, rule validation
│   ├── player_timers.py    PlayerTimers — session + lifetime playtime,
│   │                       fmt_duration display helper
│   └── utils.py            port check, backup zip + testzip,
│                           clean_mod_filename, fmt_size, DPI awareness
├── ui/
│   ├── theme.py            Theme presets (amber / green / cyan / dark / custom)
│   ├── widgets.py          TermButton, TermEntry, Sparkline, ScrollableFrame,
│   │                       ToastQueue (queued, non-overlapping toasts)
│   ├── tab_custom_commands.py   CUSTOM CMDS tab — full editor + audit panel
│   ├── tab_autorun.py      AUTORUN tab — interval rule editor + audit strip
│   ├── tab_chat_log.py     CHAT LOG tab — per-group + Ungrouped / Proximity
│   ├── tab_mods.py         Mods tab + ModDB browser + update picker dialog
│   ├── tab_commands.py     COMMANDS tab — VS command reference
│   ├── tab_settings.py     SETTINGS tab — paths, scheduling, theme,
│   │                       player-aware restart/shutdown guards
│   ├── tab_backup.py       BACKUP tab
│   ├── tab_config.py       CONFIG editor
│   └── tab_custom_theme.py CUSTOM THEME color picker
├── backup/
│   └── manager.py          BackupManager — async zip + retention
├── mods/
│   ├── inspector.py        LocalModInspector (modinfo from zip/dir/cs/dll)
│   ├── moddb.py            ModDbClient (ModDB REST API)
│   └── moddb_cache.py      ModDbCache — on-disk TTL cache for get_mod
└── tests/                  pytest-style suite for all pure modules
    ├── conftest.py
    ├── test_parsers.py     log-line classification, cron, version compare
    ├── test_custom_commands.py   trigger matching, args, cooldowns,
    │                             roles, destructive-guard, audit hooks
    ├── test_autorun.py     scheduler ticks, intervals, gates, audit
    ├── test_player_timers.py  session/total accumulation, flush logic
    ├── test_settings.py    schema migration v1→v7, per-profile rules,
    │                       import/export, atomic save
    └── test_utils.py       fmt_size, sanitize, mod-filename cleaning
```

## Improvements vs v2

### Initial pass

| #  | Improvement                                                       | Status |
|----|-------------------------------------------------------------------|--------|
| 1  | Modular package split (no backwards compatibility kept)           | ✓ |
| 4  | Type hints throughout                                             | ✓ |
| 6  | Date-based backup retention (Keep last N days)                    | ✓ |
| 7  | Backup ZIP integrity check (`zipfile.testzip()`) after write      | ✓ |
| 8  | Crash-loop threshold reads from settings                          | ✓ |
| 10 | Console right-click → copy line / copy all / clear                | ✓ |
| 12 | Toast queue — toasts no longer overlap                            | ✓ |
| 13 | Neutral dark mode added alongside amber/green/cyan                | ✓ |
| 14 | Ban confirmation dialog                                           | ✓ |
| 15 | Crash-loop threshold UI in Settings tab                           | ✓ |
| 16 | Cron schedule entry validated live, shows next-fire ETA           | ✓ |
| 17 | Atomic settings save (tmp → rename)                               | ✓ |
| 18 | Settings schema versioning                                        | ✓ |
| 19 | `requirements.txt`                                                | ✓ |
| 20 | `--log-level` CLI argument                                        | ✓ |
| 21 | `main()` entry-point function                                     | ✓ |
| +  | Custom Commands tab + ChatCommandDispatcher                       | ✓ |

### Second pass

| #  | Improvement                                                       | Status |
|----|-------------------------------------------------------------------|--------|
| 1  | Unit tests across pure-logic modules                              | ✓ |
| 2  | `backup/` extraction — `BackupManager` class                      | ✓ |
| 3  | Tab extraction — every `_build_*_tab` lives in `ui/tab_*.py`      | ✓ |
| 4  | Per-rule cooldowns, tracked per (rule, player)                    | ✓ |
| 5  | Argument capture — `{1}–{9}`, `{target}`, `{args}`, `{role}`      | ✓ |
| 6  | Audit log — `AuditRecord` + listener, surfaced in tab + log file  | ✓ |
| 7  | Destructive-keyword guard for `/stop` `/ban` `/op` …              | ✓ |
| 8  | Pre-migration settings backup (`.v3.<timestamp>.bak`)             | ✓ |
| 9  | Type hints on extracted mod block                                 | ✓ |
| 10 | Print → LOG sweep (already clean from v2)                         | ✓ |
| 11 | Per-profile custom commands (schema v4 migration)                 | ✓ |
| 12 | Import / export rules as JSON                                     | ✓ |
| 13 | Live trigger preview — runs the dispatcher against a sample player| ✓ |

### Third pass

| #   | Improvement                                                       | Status |
|-----|-------------------------------------------------------------------|--------|
| 14  | Player-aware restart / shutdown guards                            | ✓ |
| 15  | Ungrouped / Proximity chat subtab in CHAT LOG                     | ✓ |
| 16  | "Auto-save" labels renamed to "Periodic auto-backup" for clarity  | ✓ |
| 17  | Mod-update picker dialog — per-mod selection with smart defaults  | ✓ |
| 18  | Canonical `version_is_newer` for update check (kills legacy path) | ✓ |
| 19  | GAME VER filter applied to update check                           | ✓ |
| 20  | Bulk-update progress bar + working Cancel inside the picker       | ✓ |
| 21  | Parallel `_update_check_worker` (`ThreadPoolExecutor`)            | ✓ |
| 22  | On-disk TTL cache for `moddb.get_mod` (`get_mod_cached`)          | ✓ |
| 23  | Atomic mod download — `.part` + `os.replace()` rename             | ✓ |

### Fourth pass (this iteration)

| #   | Improvement                                                       | Status |
|-----|-------------------------------------------------------------------|--------|
| 24  | AUTORUN tab — interval-based console-command rules per profile    | ✓ |
| 25  | `core/autorun.py` — testable scheduler with injectable clock/send | ✓ |
| 26  | Run-on-start, run-on-save, pause-when-empty rule gates            | ✓ |
| 27  | ▶ Run Now button — fire saved rule once + reset interval          | ✓ |
| 28  | Per-player session + lifetime playtime tracking (`PlayerTimers`)  | ✓ |
| 29  | 1Hz in-place label updates on player rows (no row rebuild)        | ✓ |
| 30  | Periodic flush of active sessions (≤60s loss on crash)            | ✓ |
| 31  | Schema v7 migration — `autorun_rules` + `player_totals` per profile | ✓ |

## The Custom Commands tab

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
Existing settings files are auto-migrated on first launch (with a
pre-migration `.bak` saved alongside).

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

## The Autorun tab

Reachable from the sidebar as **AUTORUN**. Each rule fires its
configured console commands every N seconds while the server is
running. Rules are per-profile and persist alongside custom commands.

### Rule fields

- **Name** — human-readable label, also the dedup key for the
  scheduler.
- **Enabled** — toggle without deleting.
- **Interval** — value + unit (seconds / minutes / hours). The UI
  picks the largest unit that divides cleanly when displaying the
  saved value.
- **Commands** — multi-line text, one console command per line. Lines
  starting with `#` are treated as comments and skipped.
- **Run on start** — fire once when the server starts, then resume
  the normal interval cadence.
- **Run on save** — fire once whenever the rule is saved, and reset
  the next-fire deadline so the periodic cadence re-anchors to
  save-time. Useful for "I just edited the broadcast text — run it
  now and re-time the every-15-min cadence."
- **Pause when empty** — skip ticks while 0 players are online. The
  next tick still gets scheduled normally; it just doesn't send.

### ▶ Run Now button

A one-shot fire of the **saved** state of the selected rule. The
interval is reset, so the next periodic fire is `interval_secs` from
now. Note that Run Now ignores unsaved editor changes — to fire your
in-progress edits, hit 💾 Save (which honours the *Run on save*
checkbox).

### Audit strip

The bottom of the tab shows the most recent scheduler decisions:
fires, gates that blocked a fire (disabled, paused, server down), and
the reason for each skip. Same look-and-feel as the Custom Commands
audit panel.

### Scheduler design

`core/autorun.py::AutorunScheduler` is fully decoupled from the UI:
rules are plain dicts (round-trip through settings JSON), and time
plus side-effects are injected via a `clock` and `send` callable —
which makes the engine trivially unit-testable without Tk or a real
server process. Intervals are measured from server start, so they
don't drift across restarts.

### Rule schema

```json
{
  "name":             "Hourly broadcast",
  "enabled":          true,
  "interval_secs":    3600,
  "commands":         "/announce Welcome to the server!\n# this line is a comment\n/autosavenow",
  "run_on_start":     true,
  "run_on_save":      false,
  "pause_when_empty": true
}
```

## Player playtime tracking

Every player row in the sidebar shows two timers:

- **🕐 session** — wall-clock time since this player's most recent
  join. Resets to 0 each time they leave and rejoin.
- **Σ total** — cumulative time across every session ever recorded.
  Persisted to settings so it survives VSSM restarts.

Both update in place at 1Hz with no row rebuild — the labels are
held by reference and re-textured. Right-click a player row to see
"Reset playtime" and "Forget player" options.

`core/player_timers.py::PlayerTimers` is engine-only (no Tk, no I/O).
The host calls `record_join` / `record_leave` on player events and
`flush()` once per minute (and at shutdown), so a crash loses at most
~60 s of playtime. Player names are case-preserved exactly as the
server reports them — VS player names are case-sensitive.

Storage lives per-profile under `player_totals` in
`vserverman_settings.json`.

## Player-aware shutdown / restart guards

Three checkboxes in the **Settings** tab gate the shutdown/restart
paths against the live player list (which VSSM keeps current via
`/list clients` polling, configurable in Settings):

- **Check for players before manual restart** — when you click the
  Restart button and players are online, a 3-button dialog appears:
  *Wait until empty* / *Continue anyway* / *Cancel*.
- **Check for players before manual shutdown** — same dialog wired
  to the Stop button.
- **Check for players before scheduled restart** — when a cron-fired
  restart is due and players are online, the restart **silently
  defers**: VSSM broadcasts a heads-up via `/announce`, polls every
  5 seconds, and fires the restart as soon as the server is empty.
  The pre-existing 5-min / 1-min / 10-sec broadcast warnings still
  fire at the originally-scheduled time, so players still get advance
  notice.

All three default to off — existing settings files load unchanged and
behaviour is identical to before until you tick a box. The flags are
persisted per-profile, so each server config can have its own policy.

## CHAT LOG tab — Ungrouped / Proximity subtab

The **CHAT LOG** tab keeps one subtab per chat group ID (`General`,
named groups, etc.). A separate subtab — **Ungrouped / Proximity** —
captures roleplay/proximity-mod chat lines that have no numeric group
ID, e.g. lines emitted by *The Basics — Roleplay (RP) Proximity Chat*:

```
[Server Chat] Dan mentions "hello there"
[Server Chat] Dan states "this is proximity yelling"
[Server Chat] Dan exclaims "this is proximity yelling!"
```

These lines previously fell through both parsers (no `<digit> | Player:`
prefix, no `Player: message` colon form) and were silently dropped.
`core.parsers.parse_ungrouped_chat` now matches the `Player VERB
"message"` shape, and the verb is preserved in the rendered message
body so the roleplay flavour stays visible. The default tab name is
`Ungrouped / Proximity`; you can rename it via the same right-click
menu used for any other group.

## Settings tab terminology

**"Auto-save enabled"** has been renamed **"Periodic auto-backup"** to
match what the feature actually does — it's a periodic *backup*
scheduler that *optionally* sends `/autosavenow` first via a separate
checkbox. The Python identifiers and settings-file keys are unchanged
(`autosave_enabled`, `autosave_cmd`, `autosave_interval`), so existing
settings files load with no migration.

| Old label                          | New label                            |
|------------------------------------|--------------------------------------|
| Auto-save enabled                  | Periodic auto-backup                 |
| Send /autosavenow with backup      | Save world before each auto-backup   |
| Auto-save every (min):             | Auto-backup every (min):             |

## The mod updater

Click **⟳ Check Updates** on the **MODS → INSTALLED** sub-tab to scan
every local mod against ModDB.

### What runs under the hood

- **Parallel update check.** Each mod is queried via
  `concurrent.futures.ThreadPoolExecutor` (default 8 workers); the
  status line shows live progress: `Checking updates — 12/50 (3 cached,
  9 fetched)…`.
- **On-disk TTL cache.** `ModDbClient.get_mod_cached` consults a JSON
  cache file (next to `vserverman_settings.json`) before hitting the
  network. The cache TTL is 6 hours by default, so re-running the
  check inside that window costs zero network. Tick *Force refresh*
  in the MODS tab to bypass the cache for one check.
- **Canonical version comparator.** Update detection routes through
  `core.parsers.version_is_newer`, which uses the `packaging` library
  when available and a tested fallback otherwise. Pre-releases are
  ranked correctly (`1.0.0` > `1.0.0-rc.2` > `1.0.0-pre.1`).
- **Game-version compatibility filter.** Set GAME VER on the BROWSE
  sub-tab to your server's actual VS version, and the update check
  will only consider releases tagged for that version. Mods with
  releases for *other* versions only are bucketed as
  *no compatible release* rather than *outdated*, so you can't
  accidentally pull a 1.21-only release onto a 1.20 server.

### The picker dialog

Hitting Update opens a modal picker dialog showing every outdated
mod with a checkbox:

```
┌─ Mod Update Report ──────────────────────────────────────────┐
│  3 outdated · 27 up-to-date · 2 no compatible release · …    │
├──────────────────────────────────────────────────────────────┤
│  [✓] [SERVER] CarryOn          1.7.3 → 1.8.0                 │
│  [✓] [ BOTH ] MedievalExpansion 4.5.1 → 4.6.0                │
│  [ ] [CLIENT] FancyHUD          1.0.0 → 1.1.0                │
│                                                              │
│  [Select all] [None] [Server+Universal only] [Client only]   │
├──────────────────────────────────────────────────────────────┤
│  ► Up-to-date (27)        — collapsible                      │
│  ► No release for selected game version (2)  — collapsible   │
│  ► Failed to check (1)    — collapsible                      │
│  ► No modid (0)           — collapsible                      │
├──────────────────────────────────────────────────────────────┤
│             [Cancel]            [Update 2 selected mod(s)]   │
└──────────────────────────────────────────────────────────────┘
```

Smart defaults: server / universal mods are pre-ticked; client-only
mods are off by default. The Update button label updates live as you
toggle, and the button is disabled when zero mods are ticked. Quick-
select buttons toggle subsets without closing the dialog.

### In-dialog progress + cancel

Once you click Update, the picker UI is replaced with a progress
panel in the same dialog:

- A **per-file progress bar** that fills as each download streams.
- A status line `[3/14] Updating MyMod…`
- A live counter `3 / 14`.
- A scrolling log of completed files (`✓ ModA`, `✗ ModB: 404`).
- A **Cancel** button that aborts cleanly **between files** AND
  **mid-stream** — the cancel flag is fed into the existing
  `download_file(cancel_flag=…)` hook in `mods/moddb.py`.

On completion, Cancel becomes Close and the dialog stays open so you
can review the log.

### Atomic downloads

`ModDbClient.download_file` writes the response to `<dest>.part` and
then performs an `os.replace()` to swap it into place. A failed or
cancelled download leaves no half-written file at the destination
path; `.part` files are cleaned up on error.

## Tests

The test suite covers the pure logic modules (parsers, custom-commands
engine, autorun scheduler, player timers, settings layer, utility
helpers, chat-log store). UI code is intentionally not exercised — Tk
testing is fragile and slow, and the value-per-line is much higher in
the engine.

```bash
# Without pytest (pure stdlib)
python3 run_tests.py

# Or with pytest, if you have it
pytest tests/ -v
```

The tests are written in pytest style (classes + `test_*` methods,
fixture for tmp settings dir, `pytest.raises`) and the in-tree runner
provides just enough of the pytest surface to execute them.

## Hooking into the server log

`core/parsers.py::parse_chat_message` parses Vintage Story chat lines
(`[Server Chat] <Alice> !warp spawn`). When `_handle_server_line()`
classifies a line as `chat`, the parsed `(player, message)` is dispatched
through `ChatCommandDispatcher`, which checks the speaker's role
(populated from `/player NAME role` responses) against each enabled rule
and emits the resulting console commands via the existing
`_send_internal_command()` path.

Audit records are routed through `app.after_idle` so the UI updates
always happen on the Tk main thread, regardless of which thread the
dispatch was originally invoked from.

For the CHAT LOG tab, the same `_handle_server_line` first tries
`parse_chat_with_group` (matches `<group_id> | Player: message`), then
falls through to `parse_ungrouped_chat` for `Player VERB "message"`
shapes from proximity / RP mods. Both feed the `ChatLogStore` ring
buffer (default 500 lines per group) which persists across launches.

The autorun scheduler runs on a separate 1Hz `after()` tick on the
host, independent of incoming chat — it only consults the rule list
and the player count, so it has no entanglement with the parser path.

## Patcher scripts

**Patchers are the canonical way to deliver code and feature updates
to an existing VSSM install.** Rather than shipping replacement
files (which would clobber any local edits), each new feature lands
as an `apply_*_patch.py` script that surgically modifies the relevant
files. Every patcher follows the same conventions:

- **Idempotent** — re-running detects a marker string and exits clean.
- **Pre-flight verification** — every search snippet must match
  exactly once before any file is touched. Mismatch → exit code 2,
  no changes written.
- **Timestamped backups** — every modified file gets a `.<stamp>.bak`
  next to it.
- **Post-patch syntax check** — modified files are byte-compiled with
  `py_compile`. Syntax error → automatic rollback from backup.
- **CRLF-aware** — Windows line endings are detected on read and
  preserved on write.
- **`--dry-run`** — verify everything would patch without writing.

### Running a patcher

```bash
cd vssm
python apply_<feature>_patch.py            # apply for real
python apply_<feature>_patch.py --dry-run  # verify only, no writes
```

Always run patchers from the VSSM package root (the directory that
contains `VSSM.py`).

### Exit codes

| Code | Meaning                                                      |
|------|--------------------------------------------------------------|
| `0`  | Applied successfully, OR already applied (idempotent).       |
| `1`  | Bad path / files missing / prerequisite patcher not run.     |
| `2`  | Snippet mismatch — file content doesn't match expectations.  |
| `3`  | Post-patch syntax error → automatic rollback from backup.    |

### Recovering from a failed patch

If a patcher exits non-zero, no permanent changes have been made:

- Exit `2` (snippet mismatch) means nothing was written; investigate
  why your file diverges from the expected baseline (likely a missing
  prerequisite patcher, or local edits that need rebasing).
- Exit `3` (syntax error) means the patcher already rolled back the
  file from its backup. The `.<stamp>.bak` files are kept around
  regardless, so you can also restore manually if needed.

### Patchers in the current iteration

| Patcher                                       | Adds                                       |
|-----------------------------------------------|--------------------------------------------|
| `apply_player_check_patch.py`                 | Player-aware restart/shutdown guards       |
| `apply_ungrouped_chat_patch.py`               | Ungrouped / Proximity subtab in CHAT LOG   |
| `apply_chat_log_patches.py`                   | CHAT LOG tab (per-group history)           |
| `apply_autosave_relabel_patch.py`             | Renames "Auto-save" → "Periodic auto-backup" |
| `apply_mod_update_picker_patch.py`            | Per-mod selection picker dialog            |
| `apply_mod_updater_polish_v2_patch.py`        | Canonical comparator + GAME VER filter + cancel/progress (run AFTER picker) |
| `apply_autorun_patch.py`                      | AUTORUN tab + scheduler                    |
| `apply_player_timers_patch.py`                | Per-player session + lifetime playtime tracking |

> **Run order matters where a patcher depends on another.** The mod
> updater polish patch, for instance, expects the picker dialog to
> already be in place — running it first will fail pre-flight with
> exit code 2, so you'll know to apply the picker patch first.
