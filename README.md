# VSSM v3 — Vintage Story Server Manager

A modular rewrite of the v2 monolithic single-file application. The
~7,500-line monolith is now a properly structured package with type
hints, unit tests, and a new **Custom Commands** tab that lets server
owners define chat triggers (e.g. `!warp`, `!day`, `!give`) that fire
console commands — gated by player role, throttled by per-player
cooldown, and audited in a panel that shows every fire and skip.

## Running

```bash
cd vssm
python VSSM.py                  # normal launch  (use `py -3 VSSM.py` on Windows)
python VSSM.py --log-level DEBUG
python run_tests.py             # 182-test suite, no pytest needed
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
├── VSSM.py           ← entry point + ServerManagerApp class
├── run_tests.py            ← pytest-free test runner
├── requirements.txt
├── README.md
├── core/
│   ├── constants.py        APP_NAME, APP_VERSION, logging, OPERATOR_ROLES
│   ├── parsers.py          classify_line, parse_player_event,
│   │                       parse_chat_message, parse_cron_expr,
│   │                       version_is_newer, parse_json5_ish
│   ├── settings.py         load/save/migrate (atomic + pre-migration .bak),
│   │                       per-profile rules, import/export helpers
│   ├── custom_commands.py  ChatCommandDispatcher engine, AuditRecord,
│   │                       cooldown tracker, destructive-keyword guard
│   └── utils.py            port check, backup zip + testzip,
│                           clean_mod_filename, fmt_size, DPI awareness
├── ui/
│   ├── theme.py            Theme presets (amber / green / cyan / dark / custom)
│   ├── widgets.py          TermButton, TermEntry, Sparkline, ScrollableFrame,
│   │                       ToastQueue (queued, non-overlapping toasts)
│   ├── tab_custom_commands.py   CUSTOM CMDS tab — full editor + audit panel
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
└── tests/                  155 tests for all pure modules
    ├── conftest.py
    ├── test_parsers.py     log-line classification, cron, version compare
    ├── test_custom_commands.py   trigger matching, args, cooldowns,
    │                             roles, destructive-guard, audit hooks
    ├── test_settings.py    schema migration v1→v4, per-profile rules,
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

### Second pass (this iteration)

| #  | Improvement                                                       | Status |
|----|-------------------------------------------------------------------|--------|
| 1  | Unit tests — 155 cases across 4 test modules                      | ✓ |
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

### File-size wins from extraction

- `VSSM.py`: 7,491 → 4,121 → 3,997 → 3,732 → **2,479** lines
- Mod block alone: 1,500 lines moved to `ui/tab_mods.py`
- Backup logic: 200 lines moved to `backup/manager.py`

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

### Per-profile rules (schema v4)

Each profile owns its own rule list. Switching profiles in Settings
swaps the active rule set instantly — useful for running e.g. a
`creative` profile with `!gm 1` and a `survival` profile without it.
Existing v3 settings files are auto-migrated on first launch (with a
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

## Tests

The test suite covers the pure logic modules (parsers, custom-commands
engine, settings layer, utility helpers). UI code is intentionally not
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
