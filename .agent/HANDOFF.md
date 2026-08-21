# CURRENT HANDOFF

Written for context compaction. A fresh agent should be able to continue from
this file plus the repository alone. Read `.agent/NOW.md` first — it is smaller.

## Current Objective

Finish Phase 14 (documentation) and Phase 15 (pre-deployment verification), then
**stop** with a deployment-readiness report. Do not push. Do not deploy.

## Current Phase

Phase 14–15 of 15. Phases 1–13 are implemented, tested and green:
foundation, config, database + migrations, Hyperliquid REST/WS clients, whale
pipeline (parser → tracker → detector → filter → dedup), services
(settings, admin/permissions, alerts), Telegram surface, entry point,
deployment surface (Dockerfile / railway.toml / start.sh), project memory,
and the full §36 test suite.

## What Was Just Completed

- `tests/` **exists and passes**: 11 test modules, 379 tests, 0 failed,
  0 skipped. Offline — no Hyperliquid connection, no Telegram API, no Postgres.
- `tests/test_engine_pipeline.py` (22 tests) drives a raw `trades` websocket
  frame through the real pipeline and asserts the final Telegram message text
  plus the rows persisted.
- One **real production defect** found by that test and fixed:
  `WhaleEngine._persist` ran in three concurrent workers, so two events for the
  same wallet raced on the read-then-write of `wallets` / `positions`; the loser's
  transaction failed and **its alert was never sent**. Fixed with
  `WhaleEngine._write_lock` (`app/whale/engine.py`), held only around the DB
  write, not around REST enrichment. This is the only change to `app/` in this
  phase.
- `.agent/API_NOTES.md` — per-source availability, verified 2026-08-21, with
  every missing field written as `NOT AVAILABLE FROM THIS DATA SOURCE`.
- `.agent/TEST_STATUS.md` — real numbers, per-module timings, §36 coverage map,
  and the suite's five known limitations.
- Doc correction applied: **12 database tables, 11 repository classes** (verified
  by counting `__tablename__` in `app/database/models.py` and
  `^class .*Repository` in `app/database/repository.py`) in
  `PROJECT_STATE.md` and `CHANGELOG.md`.
- `README.md` — written, and every claim checked against source: the command
  table comes from `app/bot/handlers/__init__.py::COMMANDS`, the permission
  matrix from `app/services/admin_service.py`, the env table from `.env.example`,
  the health states from `AppContainer.health`, the toggles from the real
  `ENABLE_*` names.

## What Is Half-Finished

Nothing in code. Remaining work is bookkeeping:

1. Refresh `TASK_QUEUE.md` (Task 014 subtasks are still unchecked) and
   `FILE_INDEX.md` (its project-memory line lists 7 of the 11 `.agent` files and
   does not mention `tests/`).
2. Five/six logical commits, in this order, with `git status` + `git diff` before
   each and `git log --oneline -10` after:
   `test: add unit coverage …` (fixtures, factories, config, detectors, filters,
   dedup, permissions, handlers — these were never committed, so they need their
   own commit ahead of the three prescribed test commits) →
   `test: add database persistence coverage` →
   `test: add resilience coverage` →
   `test: add end-to-end engine pipeline coverage` →
   `docs: add Hyperliquid API notes and test status` →
   `docs: add production README`.
   The engine `_write_lock` fix belongs with the pipeline test that found it.
   `railway.toml` and `scripts/` are already tracked (committed in `c054e0c`);
   nothing there needs adding.
3. Deployment-readiness report, then stop.

## Exact Files Being Worked On

`.agent/TASK_QUEUE.md`, `.agent/FILE_INDEX.md`, `.agent/NOW.md` — then git.

## Exact Function/Class Being Worked On

None — documentation and version control only.

## Current Error

None.

## Last Command Run

`./.venv/Scripts/python.exe -m pytest -q`

Note: the bare `python` on PATH is **not** the project interpreter and lacks
`pytest_asyncio`. Always use `./.venv/Scripts/python.exe`.

## Last Test Result

**379 passed, 0 failed, 0 skipped.** Per-module counts in `TEST_STATUS.md`.

## Next Exact Action

Update `TASK_QUEUE.md` and `FILE_INDEX.md`, then make the five commits above.

## Do NOT Redo

- Do not rewrite anything under `app/` — complete, committed, and green.
- Do not recreate any `tests/` module. All 11 exist and pass.
- Do not recreate `README.md`, `API_NOTES.md` or `TEST_STATUS.md`.
- Do not re-run Alembic autogenerate: `0001_initial.py` is hand-written on
  purpose (autogenerate emitted SQLite-flavoured DDL). See `DECISIONS.md`.
- Do not commit `_boot.db`, `render_preview.txt`, `.pytest_cache/`, `.venv/` or
  `.env`. `.gitignore` already excludes all of them (`*.db` catches `_boot.db`,
  and `render_preview.txt` is named explicitly).
- **Do not push to GitHub and do not deploy to Railway.** No remote exists and
  none may be invented.

## Important Technical Context

- Repositories are classes of `@staticmethod`s taking an `AsyncSession`
  (`await SettingsRepository.set(session, key, value)`), used inside
  `async with db.session() as session:`.
- `AdminService.can()` is synchronous; `require()` raises `AdminError` whose
  message is the exact user-facing refusal text.
- `WhaleFilter.evaluate(event, config)` accepts an explicit `RuntimeConfig`, so
  threshold/coin/toggle tests need no database writes.
- `permissions.requires()` injects a third `actor` argument into handlers.
- Injection seams for offline tests: `HyperliquidREST._client` (set only when
  `None` in `start()`) and the module-level `app.hyperliquid.websocket.ws_connect`.
- `validate_runtime()` reads `os.getenv("DATABASE_URL")` **directly** for the
  production check — a test must set the real env var, not just the field.
- `Settings` field for the Telegram token is `bot_token` (env `BOT_TOKEN`).
- `tests/test_engine_pipeline.py` asserts timing through queue joins
  (`engine.trades_seen`, `engine._queue.join()`, `alerts._queue.join()`), never
  `asyncio.sleep`. Keep it that way; a sleep-based version is flaky.

## Important API Findings

See `.agent/API_NOTES.md`. The six that shape the tests:
`trades` is the only global feed with wallet attribution; `orderUpdates` frames
do not name their user; `frontendOpenOrders` is the only TP/SL source;
`orderStatus` resolves cancel-vs-fill and may be inconclusive; `l2Book` is
aggregated and anonymous; there is no public global liquidation feed.
