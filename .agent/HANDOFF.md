# CURRENT HANDOFF

Written for context compaction. A fresh agent should be able to continue from
this file plus the repository alone. Read `.agent/NOW.md` first — it is smaller.

## Current Objective

Deployment. GitHub is done; Railway is the user's step.

## Current Phase

Phase 15 of 15. Everything is implemented, tested (379 passed, 0 failed,
0 skipped), documented, committed and **pushed** to
<https://github.com/CLEXER17/CLEXWHALE_BOT> (`main` tracks `origin/main`):
foundation, config, database + migrations, Hyperliquid REST/WS clients, whale
pipeline (parser → tracker → detector → filter → dedup), services
(settings, admin/permissions, alerts), Telegram surface, entry point,
deployment surface (Dockerfile / railway.toml / start.sh), project memory,
and the full §36 test suite. **Railway deployment has not been performed.**

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

Nothing in code, nothing in documentation, nothing in version control. Seven
commits are on `origin/main`:

```
test: add fixtures and unit coverage for config, detection and permissions
test: add database persistence coverage
test: add resilience coverage
test: add end-to-end engine pipeline coverage   (+ the _write_lock fix)
docs: add Hyperliquid API notes and test status
docs: add production README
docs: record the GitHub remote
```

The only remaining item is **Railway**, which this repository cannot do for the
user:

1. *New Project → Deploy from GitHub repo →* `CLEXER17/CLEXWHALE_BOT`
2. *New → Database → PostgreSQL*
3. On the bot service, set `DATABASE_URL` as a reference to
   `${{Postgres.DATABASE_URL}}` — never a literal connection string
4. Set `BOT_TOKEN` and `MAIN_ADMIN_ID`
5. Deploy; migrations run on boot; watch `/health`; `/start` in Telegram

Never paste a token or a connection string into this repository, a commit
message, or any `.agent/` file.

## Exact Files Being Worked On

None. The working tree is clean and matches `origin/main`.

## Exact Function/Class Being Worked On

None — documentation and version control only.

## Current Error

None.

## Last Command Run

`git push -u origin main` → `* [new branch] main -> main`, tracking set.

Before that: `./.venv/Scripts/python.exe -m pytest -q`.

Note: the bare `python` on PATH is **not** the project interpreter and lacks
`pytest_asyncio`. Always use `./.venv/Scripts/python.exe`.

## Last Test Result

**379 passed, 0 failed, 0 skipped.** Per-module counts in `TEST_STATUS.md`.

## Next Exact Action

Railway, by the user — the five steps above. Nothing in this repository is
waiting on an edit.

After the first live deploy, the one useful follow-up is to compare real
Hyperliquid payload shapes against `.agent/API_NOTES.md` and correct that file if
anything differs. Do not pre-emptively "fix" it from memory.

## Do NOT Redo

- Do not rewrite anything under `app/` — complete, committed, and green.
- Do not recreate any `tests/` module. All 11 exist and pass.
- Do not recreate `README.md`, `API_NOTES.md` or `TEST_STATUS.md`.
- Do not re-run Alembic autogenerate: `0001_initial.py` is hand-written on
  purpose (autogenerate emitted SQLite-flavoured DDL). See `DECISIONS.md`.
- Do not commit `_boot.db`, `render_preview.txt`, `.pytest_cache/`, `.venv/` or
  `.env`. `.gitignore` already excludes all of them (`*.db` catches `_boot.db`,
  and `render_preview.txt` is named explicitly).
- **Do not re-push history.** `origin/main` already has all seven commits;
  `git push` for new work only, never `--force`.
- **Do not attempt the Railway deployment.** It requires the user's own account
  and their own `BOT_TOKEN` / `MAIN_ADMIN_ID`. Never invent, guess or embed a
  credential to get past that.

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
