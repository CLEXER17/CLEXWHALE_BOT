# NOW

CURRENT TASK:
Task B (admin UI + wallet display + data integrity, 17 issues) and Task C
(global /pause + /go) are both complete, tested and green. Nothing is in flight.

CURRENT FILE:
none — last touched app/bot/commands.py (PUBLIC_COMMAND_MENU) and the two new
test files.

CURRENT FUNCTION:
none.

LAST COMPLETED:
The 18+ named regression tests: tests/test_admin_ui_integrity.py (32 tests,
issues 1-14) and tests/test_global_pause.py (15 tests, Task C). They caught one
real bug: /recent was published in the admin command scope while its handler
required only VIEW_WHALES and /help advertised it publicly — fixed by moving
`recent` into PUBLIC_COMMAND_MENU in app/bot/commands.py. Docs updated:
.agent/TEST_STATUS.md (474 totals, per-module table, defect 4) and
.agent/CHANGELOG.md (new top entry). Committed and pushed.

CURRENT PROBLEM:
none.

NEXT ACTION:
1 feature item 3 — TP/SL populate: relax the frontendOpenOrders weight-20 budget
  gate in WhaleEngine._enrich for a forced enrich (available > 60 when force,
  keep > 200 routine) so PositionContext.orders_known becomes True
2 feature item 5 — liquidation alert: new EventType + ValueKind.LIQUIDATION_VALUE
  + THRESHOLD_CLASS/HEADERS/DETECTOR_OF_EVENT entries + detector.from_liquidation
  + emit from WhaleEngine._process_liquidation + a dedup.identity_key branch
3 feature item 6 — startup line distinguishing durable Postgres from the
  ephemeral SQLite fallback
4 remaining docs: ARCHITECTURE/SECURITY notes on command scopes, README margin gate
User-owned: rotate BOT_TOKEN via @BotFather /revoke, update the Railway variable.

RELEVANT TEST:
./.venv/Scripts/python.exe -m pytest -q     -> 474 passed, 0 failed, 0 skipped
