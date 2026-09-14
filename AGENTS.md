# Agent notes

SwiftBar plugin. One executable file: `cursor-usage.1m.py`.

## Before changing behavior

1. Read `README.md` and `docs/PROJECT_STATUS.md`.
2. Run `python3 tests/test_cursor_usage.py`.
3. Keep the live plugin and this file identical if you install locally: copy `cursor-usage.1m.py` into the SwiftBar plugin folder, `chmod +x`, then `open 'swiftbar://refreshplugin?name=cursor-usage.1m.py'`.

## Constraints

- Do not log, print, or write the Cursor access token. Send it to `api2.cursor.sh` only, via curl `--config -`, not argv.
- Do not commit `state.vscdb`, `.env`, or SwiftBar plugin-data JSON (`cursor-*-usage.json`, notification settings).
- Do not hardcode machine-specific absolute paths. Defaults use `Path.home()`.
- Do not invent official Cursor API docs. Endpoints are unofficial (`GetCurrentPeriodUsage`, `GetPlanInfo`).
- UI strings are English. Title is Cursor Models remaining only.
- Informational rows use `color=` (SwiftBar no-op + hover). `disabled=true` without `color=` greys out the block.
- Remaining / Average stay default-colored unless remaining is below 50% (orange/red) or Average is Over pace (orange).
- After a menu or parser change, update tests and `docs/PROJECT_STATUS.md`.

## Commands

```sh
python3 tests/test_cursor_usage.py
python3 -m py_compile cursor-usage.1m.py
```
