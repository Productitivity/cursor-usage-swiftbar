# Agent notes

SwiftBar plugin. One executable file: `cursor-usage.1m.py`.

## Before changing behavior

1. Read `README.md` and this file. Do not expect `docs/` in this repository.
2. Run `python3 tests/test_cursor_usage.py`.
3. If installing locally: copy `cursor-usage.1m.py` into the SwiftBar plugin folder, `chmod +x`, then `open 'swiftbar://refreshplugin?name=cursor-usage.1m.py'`.

## Current behavior

- Title: `Cursor <remaining%>` from Cursor Models only (`100 - autoPercentUsed`), always two decimals.
- Remaining or Cursor Models opens `https://cursor.com/dashboard/spending`. Other Models is `100 - apiPercentUsed`.
- Pace: today usage, average vs target on one line (`On pace` / `Over pace`). Hourly is nested under Average.
- Cycle: one Resets line. Plan is nested under Resets.
- Refresh is error-menu only. Alerts: reset plus remaining 50 / 25 / 10.
- Auth: read `cursorAuth/accessToken` from Cursor `state.vscdb` at runtime. Never store or print it.
- Usage: unofficial `api2.cursor.sh` (`GetCurrentPeriodUsage`, `GetPlanInfo`) via `/usr/bin/curl` with the token on `--config -`, not argv.
- Today / hourly / alerts follow the auto quota only.

## Constraints

- Do not log, print, or write the Cursor access token.
- Do not commit `state.vscdb`, `.env`, SwiftBar plugin-data JSON, or a `docs/` folder.
- Do not hardcode machine-specific absolute paths. Defaults use `Path.home()`.
- Do not invent official Cursor API docs.
- UI strings are English. Title is Cursor Models remaining only.
- Informational rows use `color=` (SwiftBar no-op + hover). `disabled=true` without `color=` greys out the block.
- Remaining / Average stay default-colored unless remaining is below 50% (orange/red) or Average is Over pace (orange).
- After a menu or parser change, update tests and this file.

## Commands

```sh
python3 tests/test_cursor_usage.py
python3 -m py_compile cursor-usage.1m.py
```
