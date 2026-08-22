# CURRENT HANDOFF

Written for context compaction. A fresh agent should be able to continue from
this file plus the repository alone. Read `.agent/NOW.md` first — it is smaller.

**Do not restart the project. Do not recreate files that already exist. Do not
remove working features.** The repository is the source of truth.

## Current Objective

None outstanding in code. The **verified execution + position lifecycle** task
(39-section spec) is complete, as is the persistence + settings-safety task before
it. What is left is live work on the deployed bot: rotate the token, confirm the
redeploy, and run the §33 six-step scenario.

## Current Phase

Everything is implemented, tested (**587 passed, 0 failed**), documented and
pushed to <https://github.com/CLEXER17/CLEXWHALE_BOT>: foundation, config,
database + migrations, Hyperliquid REST/WS clients, whale pipeline (parser →
tracker → detector → filter → dedup), services (settings, admin/permissions,
alerts), Telegram surface, entry point, deployment surface (Dockerfile /
railway.toml / start.sh), project memory and the test suite. The bot has run on
Railway.

## What Was Just Completed

**The 34-section persistence + settings-safety fix.** Two reported production
symptoms:

1. **"changing one setting replaces other settings."** Root-caused to exactly one
   path, not a systemic problem: the coin panel's `✏️ Set list` prompt
   (`app/bot/handlers/prompts.py`, `kind == "coins"`) called
   `settings.set_coins()`. An admin monitoring BTC/ETH/SOL who answered `HYPE`
   was left monitoring only HYPE — a replacement dressed as an addition. It now
   calls `add_coins()`; the button reads `➕ Add coins`; the prompt text says
   "add". Everything else in the settings layer was already patch-based
   (`set_value` writes one row and patches one field; `/addcoin`, `/removecoin`
   and the per-coin toggles were already single-row).
2. **"settings lost after a Railway redeploy."** The DB was already authoritative,
   so this was hardening rather than a rewrite: a `bootstrapped_at` marker row now
   records that this installation has been initialised, so `load()` seeds the
   environment's coin list **only** while the marker is absent — an admin who
   deliberately monitors nothing no longer gets `DEFAULT_COINS` resurrected.
   `CoinRepository.replace` became a diff (returns `(added, removed)`) instead of
   `DELETE FROM tracked_coins` + re-insert, so adding HYPE does not rewrite the
   BTC row and two admins editing at once cannot undo each other.

Also added: `/config` (reads the tables directly and flags cache-vs-database
drift), `/resetsettings` (two-step, main-admin-only via a new
`Capability.RESET_SETTINGS`), a two-step 🧹 Clear for the coin panel,
`container.startup_summary()` and the plain-text startup block in `main.py`
(counts and states only — never a token or a URL), and
`tests/test_persistence.py` (29 tests).

Before that: liquidation work (items 3 + 5) at `8d48846`, then the
execution/lifecycle engine, detector and filter work at `31d8095`.

## What Is Half-Finished

Nothing in the code. **The 39-section verified-execution / position-lifecycle task
is complete**: alert service, `/recent`, `/whales`, split summary metrics, the
`enable_order_alerts` toggle UI, the position lifecycle, wallet formatting, and 28
regression tests in `tests/test_verified_execution.py` covering spec requirements
1–24. Full suite **587 passed / 0 failed** (2026-08-22).

Two items from the original spec were deliberately *not* built, and the reasons are
recorded in `DECISIONS.md`:

- No UNIQUE constraint on `whale_events.dedup_key`, and therefore no third
  migration — `0001_initial` and `0002_alert_thread_key` remain the only ones.
- No collapsing of the two observations of one fill (`tid`-keyed `WHALE_TRADE`
  from the trades feed, `oid`-keyed `ORDER_FILLED` from `orderUpdates`).

Still open from the spec: the §33 six-step live scenario for
`0x31dea2516beee92135b96f464eeec3cf292a13f2`, which needs the deployed bot.

Unverifiable from here: **live alert delivery** on Railway. If alerts do not
arrive, suspect recipients rather than wiring —
`AlertService._resolve_recipients` sends to `admins.admin_ids`, so
`MAIN_ADMIN_ID` must be the account watching and must not have blocked the bot.

Only the user can do: **rotate the bot token** (exposed in a chat transcript).
@BotFather → `/revoke` → new token into the Railway variable. Never paste a token
or a connection string into this repository, a commit message, or any `.agent/`
file.

## Exact Files Being Worked On

None mid-edit.

## Current Error

None.

## Last Command Run

`./.venv/Scripts/python.exe -m pytest -q` → 587 passed.

Note: the bare `python` on PATH is **not** the project interpreter and lacks
`pytest_asyncio`. Always use `./.venv/Scripts/python.exe`.

## Last Test Result

**587 passed, 0 failed.** Per-module counts in `TEST_STATUS.md`.

## Next Exact Action

Open `app/services/alert_service.py` and relabel `_render_trade` to the §4 format,
then update the five stale assertions listed above and re-run the suite.

## Do NOT Redo

- Do not rewrite anything under `app/` wholesale — it is complete and green.
- Do not recreate any `tests/` module. Do not recreate `README.md`,
  `API_NOTES.md` or `TEST_STATUS.md`.
- **Do not make `prompts.py`'s `kind == "coins"` branch call `set_coins()` again.**
  That was the reported "one change replaces my settings" bug.
  `tests/test_persistence.py::test_the_add_coins_prompt_adds_instead_of_replacing`
  is the guard.
- **Do not turn `CoinRepository.replace` back into delete-all + re-insert.** It is
  a diff on purpose; see `DECISIONS.md`.
- **Do not add a startup write of defaults over existing rows.** `load()` seeds
  only keys that are absent, and coins only while `bootstrapped_at` is unset.
- **Do not add a UNIQUE constraint on `whale_events.dedup_key`.** Trade identity
  already includes the exchange `tid`; a hard UNIQUE would permanently block a
  legitimate repeat of an identical position change. Non-unique index plus the
  1-hour `IDENTITY_TTL` is the mechanism.
- Do not move `attach_bot()` back into `post_init`. That was a production bug.
- Do not "simplify" `SecretRedactor._scrub_arg` back to `str(a)`. That was the
  other one.
- Do not re-run Alembic autogenerate: `0001_initial.py` is hand-written on purpose
  (autogenerate emitted SQLite-flavoured DDL).
- Do not commit `_boot.db`, `render_preview.txt`, `.pytest_cache/`, `.venv/` or
  `.env`. `.gitignore` already excludes all of them.
- **Do not re-push history.** `git push` for new work only, never `--force`.
- **Do not attempt Railway changes.** They need the user's own account and their
  own `BOT_TOKEN` / `MAIN_ADMIN_ID`. Never invent, guess or embed a credential.

## Important Technical Context

- **Settings are a patch, never a replacement.** `SettingsService.set_value()`
  writes one row and rebuilds the frozen `RuntimeConfig` from the cache plus that
  one field, under an `asyncio.Lock`. Anything new must follow that shape; do not
  reconstruct the config from defaults.
- **`SettingsService.first_boot` / `bootstrapped_at`** answer "should defaults
  apply?" from a stored row rather than a guess. `startup_summary()["configuration"]`
  surfaces it as `seeded` vs `loaded`, which is how an unexpected reset becomes
  visible in the Railway log.
- **`SettingsService.decode_stored()`** returns the raw text when JSON decoding
  fails, so `/config` shows a corrupt row instead of hiding it behind a default.
- `views.config_view()` reads `SettingsRepository.all` / `CoinRepository.enabled` /
  `AdminRepository.list` / `WalletRepository.tracked` directly — never the cache,
  or the panel would confirm itself.
- **`AdminRepository`'s list method is `list`, not `list_all`.** `Database`
  exposes `is_sqlite` and `stats()["dialect"]`; there is no `backend` attribute.
- Restart vs redeploy in tests: a restart is `SettingsService(database, env)` +
  `load()`; a redeploy is `AppContainer(env, database)` + `restore()`, both over
  the same `Database`. `tests/test_persistence.py::redeploy` is the helper.
- **PTB does not call `post_init` unless you use `run_polling`/`run_webhook`.**
  `Runtime.start()` invokes it explicitly.
- **The log filter must never change an argument's type.** `record.args` feeds
  `msg % args`; a stringified int breaks `%d` in third-party log calls.
- Repositories are classes of `@staticmethod`s taking an `AsyncSession`
  (`await SettingsRepository.set(session, key, value)`), used inside
  `async with db.session() as session:`.
- `AdminService.can()` is synchronous; `require()` raises `AdminError` whose
  message is the exact user-facing refusal text. `Capability.RESET_SETTINGS` is in
  `MAIN_ONLY_CAPABILITIES`, so a forged `reset:confirm` callback is refused
  server-side.
- `WhaleFilter.evaluate(event, config)` accepts an explicit `RuntimeConfig`, so
  threshold/coin/toggle tests need no database writes.
- `permissions.requires()` injects a third `actor` argument into handlers, and
  `tests/conftest.py` resets its throttle state between tests.
- Injection seams for offline tests: `HyperliquidREST._client` (set only when
  `None` in `start()`) and the module-level `app.hyperliquid.websocket.ws_connect`.
- `validate_runtime()` reads `os.getenv("DATABASE_URL")` **directly** for the
  production check — a test must set the real env var, not just the field.
- `Settings` field for the Telegram token is `bot_token` (env `BOT_TOKEN`); the
  production-environment flag is `app_env` (env `APP_ENV`, default `production`).
- `tests/test_engine_pipeline.py` asserts timing through queue joins
  (`engine.trades_seen`, `engine._queue.join()`, `alerts._queue.join()`), never
  `asyncio.sleep`. Keep it that way; a sleep-based version is flaky.

## Important API Findings

See `.agent/API_NOTES.md`. The six that shape the tests:
`trades` is the only global feed with wallet attribution; `orderUpdates` frames
do not name their user; `frontendOpenOrders` is the only TP/SL source;
`orderStatus` resolves cancel-vs-fill and may be inconclusive; `l2Book` is
aggregated and anonymous; there is no public global liquidation feed.
