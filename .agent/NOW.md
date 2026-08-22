# NOW — resume here, read this file first

Updated: 2026-08-21

---

## CURRENT TASK

**VERIFIED EXECUTION + POSITION LIFECYCLE** (39-section spec, Tasks A–I).
Worked as 10 checkpoints: 1 alert service · 2 /recent · 3 /whales ·
4 summary separation · 5 order-alert toggle · 6 position lifecycle ·
7 wallet formatting · 8 regression tests · 9 integration · 10 full suite.

## CURRENT SUBTASK

None. Checkpoints 1–10 are complete; documentation is updated and the work is
committed. What remains needs the deployed bot and is listed under
USER-OWNED below.

## STATUS

**All 10 checkpoints complete and green.**

| # | Checkpoint | State |
|---|---|---|
| 1 | Alert service: FILLED/EXECUTED → trade alert; PLACED/RESTING/MODIFIED/CANCELLED → internal | done |
| 2 | `/recent` shows verified executions only | done |
| 3 | `/whales` wallet stats count executions only | done |
| 4 | Summary metrics split: executed trades / order events / position events | done |
| 5 | `enable_order_detector` (detection) split from `enable_order_alerts` (user-facing) | done |
| 6 | Position lifecycle: SELL ≠ SHORT; zero → CLOSED from last non-zero snapshot | done |
| 7 | Wallet address never truncated, always monospace, never in `callback_data` | done |
| 8 | Regression tests (spec requirements 1–24) | done — 28 passed |
| 9 | Full integration | done |
| 10 | Complete test suite | done — 587 passed / 0 failed |

## FILES BEING MODIFIED

None mid-edit.

## FILES ALREADY COMPLETED

- `tests/test_verified_execution.py` — **new**, 28 tests, all passing.
- `app/whale/detector.py` — two edits, now covered by the full suite:
  - `_attach_position` (~L230) treats a *flat* snapshot like a missing one:
    `position_value` / `entry_px` / `liquidation_px` / `leverage` become
    `DataPoint.unavailable("no open position for this coin")`.
  - `from_position_change` (~L713): `reference_entry` is only read from
    `ctx.position` when that snapshot is not flat.
- `app/services/alert_service.py` — execution vs order rendering; dead docstring
  cross-reference fixed (`lifecycle.position_side` → `POSITION_SIDES`).
- `app/whale/events.py`, `app/whale/filters.py`, `app/bot/views.py`,
  `app/bot/messages/texts.py`, `app/bot/keyboards/inline.py`,
  `app/bot/handlers/callbacks.py`, `app/database/repository.py`,
  `app/hyperliquid/models.py` — checkpoints 1–7.
- `.agent/TEST_STATUS.md`, `CHANGELOG.md`, `DECISIONS.md`, `PROJECT_STATE.md`,
  `HANDOFF.md` — updated for this milestone.

## TESTS PASSED

- `./.venv/Scripts/python.exe -m pytest -q` → **587 passed in 49.82 s**
  (2026-08-22). Per-module counts in `TEST_STATUS.md`.
- `./.venv/Scripts/python.exe -m compileall -q app/ tests/` → clean.
- `tests/test_verified_execution.py` alone → 28 passed.

## TESTS FAILED

None.

## NEXT EXACT ACTION

Nothing offline. The next actions belong to the user (see USER-OWNED); after the
token is rotated and the bot redeploys, run the §33 six-step live scenario for
`0x31dea2516beee92135b96f464eeec3cf292a13f2` and record any payload shape that
differs from `API_NOTES.md`.

## DO NOT START

- **Persistence / settings work — COMPLETE and accepted (commit `6a0d677`).**
- **Verified execution / position lifecycle — COMPLETE. Do not redo checkpoints 1–10.**
- Do not create another Alembic migration. `0001_initial` + `0002_alert_thread_key`
  are the migrations; the verified-execution work needed no schema change.
- Do not add a UNIQUE constraint to `whale_events.dedup_key` — its absence is a
  decision, so legitimate repeated position changes stay possible.
- Do not redesign the settings system (key/value table is sufficient) or the
  `bootstrapped_at` marker row.
- Do not redo `.agent` memory or the Railway persistence implementation.
- Never run `git reset --hard`, `git checkout .`, or `git clean -fd`.

## BLOCKED BY

Nothing.

## NOT VERIFIED

Live Railway behaviour. Nothing in this milestone was observed against the
deployed bot — no live alert, no live redeploy log.

## USER-OWNED (not mine to do)

Rotate the exposed `BOT_TOKEN` via @BotFather `/revoke`, update the Railway
variable, confirm the redeploy log and a live alert.
