# CURRENT HANDOFF

Written for context compaction. A fresh agent should be able to continue from
this file plus the repository alone. Read `.agent/NOW.md` first — it is smaller.

## Current Objective

Confirm that alerts actually reach Telegram on the live Railway deployment.

## Current Phase

Phase 15 of 15. Everything is implemented, tested (392 passed, 0 failed,
0 skipped), documented, committed and **pushed** to
<https://github.com/CLEXER17/CLEXWHALE_BOT> (`main` tracks `origin/main`):
foundation, config, database + migrations, Hyperliquid REST/WS clients, whale
pipeline (parser → tracker → detector → filter → dedup), services
(settings, admin/permissions, alerts), Telegram surface, entry point,
deployment surface (Dockerfile / railway.toml / start.sh), project memory,
and the full §36 test suite. **The bot has been deployed and has run on
Railway**; the first run exposed two defects, both now fixed and pushed.

## What Was Just Completed

The two defects the first live deploy exposed — root-caused from the Railway log,
fixed, and each covered by a regression test **verified to fail** against the
code that shipped:

1. **Every alert was dropped** (`Alert dropped: Telegram bot not attached yet`),
   and the command menu never appeared. `AlertService.attach_bot()` was only
   reachable from `post_init`, which PTB calls **only** from `run_polling` /
   `run_webhook` — and `Runtime` drives the lifecycle by hand. Fixed in
   `app/bot/application.py`: `build_application()` attaches the bot directly;
   `post_init` is public, guarded, and called explicitly by `Runtime.start()`
   after `initialize()`. New module `tests/test_bot_application.py` (6 tests) —
   nothing had ever built the real `Application`, which is why this shipped.
2. **Secret redaction corrupted `%`-style log args.** `SecretRedactor` coerced
   every `record.args` entry to `str`, so uvicorn's `"Started server process
   [%d]"` raised `TypeError` inside the formatter and Railway got screens of
   logging tracebacks. Fixed in `app/utils/logging.py` with a type-preserving
   `_scrub_arg()` and `_safe_message()`. Redaction coverage unchanged, and now
   tested with args (including a secret hidden inside a non-string arg).

Earlier in the phase: the full §36 suite, the `WhaleEngine._write_lock` fix it
found, `README.md`, `API_NOTES.md`, `TEST_STATUS.md`, and the GitHub publish.

## What Is Half-Finished

Nothing in code. One thing is unverified and cannot be verified from here:
**live alert delivery**. The redeploy carrying `aa910bc` must show
`Telegram connected` in the log and no `Alert dropped` lines, and a whale above
`MIN_WHALE_VALUE` must actually arrive in the admin's chat.

If it still does not arrive, suspect recipients rather than wiring:
`AlertService._resolve_recipients` sends to `admins.admin_ids`, so
`MAIN_ADMIN_ID` must be the account watching, and that account must not have
blocked the bot.

Also outstanding, and only the user can do it: **rotate the bot token**, which
was exposed in a chat transcript. @BotFather → `/revoke` → new token into the
Railway variable. Never paste a token or a connection string into this
repository, a commit message, or any `.agent/` file.

## Exact Files Being Worked On

None. The working tree is clean and matches `origin/main`.

## Exact Function/Class Being Worked On

None — documentation and version control only.

## Current Error

None.

## Last Command Run

`git push origin main` → `ae50d7b..c965da9  main -> main`.

Before that: `./.venv/Scripts/python.exe -m pytest -q`.

Note: the bare `python` on PATH is **not** the project interpreter and lacks
`pytest_asyncio`. Always use `./.venv/Scripts/python.exe`.

## Last Test Result

**392 passed, 0 failed, 0 skipped.** Per-module counts in `TEST_STATUS.md`.

## Next Exact Action

Read the Railway log after the redeploy. Two positive signals and one negative:
`Telegram connected` present, an alert delivered to the admin chat, and no
`Alert dropped: Telegram bot not attached yet`.

Nothing in this repository is waiting on an edit. After a clean live run, the one
useful follow-up is to compare real Hyperliquid payload shapes against
`.agent/API_NOTES.md` and correct that file if anything differs. Do not
pre-emptively "fix" it from memory.

## Do NOT Redo

- Do not rewrite anything under `app/` — complete, committed, and green.
- Do not recreate any `tests/` module. All 12 exist and pass.
- Do not recreate `README.md`, `API_NOTES.md` or `TEST_STATUS.md`.
- Do not move `attach_bot()` back into `post_init`. That was the production bug;
  see `DECISIONS.md`.
- Do not "simplify" `SecretRedactor._scrub_arg` back to `str(a)`. That was the
  other production bug.
- Do not re-run Alembic autogenerate: `0001_initial.py` is hand-written on
  purpose (autogenerate emitted SQLite-flavoured DDL). See `DECISIONS.md`.
- Do not commit `_boot.db`, `render_preview.txt`, `.pytest_cache/`, `.venv/` or
  `.env`. `.gitignore` already excludes all of them (`*.db` catches `_boot.db`,
  and `render_preview.txt` is named explicitly).
- **Do not re-push history.** `git push` for new work only, never `--force`.
- **Do not attempt Railway changes.** They need the user's own account and their
  own `BOT_TOKEN` / `MAIN_ADMIN_ID`. Never invent, guess or embed a credential.

## Important Technical Context

- **PTB does not call `post_init` unless you use `run_polling`/`run_webhook`.**
  This process calls `initialize()` / `start()` / `updater.start_polling()`
  itself, so anything that must happen once at startup has to be invoked by
  `Runtime.start()` explicitly. This cost a whole deploy's worth of alerts.
- **The log filter must never change an argument's type.** `record.args` feeds
  `msg % args`; a stringified int breaks `%d` in third-party log calls and Python
  answers with a traceback per line.
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
