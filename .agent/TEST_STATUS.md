# TEST STATUS

These are **actual** results from running the suite, not estimates. Reproduce
with:

```bash
./.venv/Scripts/python.exe -m pytest -q
```

The bare `python` on PATH is **not** the project interpreter and lacks
`pytest_asyncio`. On Linux/macOS the equivalent is `.venv/bin/python -m pytest -q`.

Last full run: **2026-08-22**
Environment: Python 3.13.3, pytest 8.4.2, `asyncio_mode = auto`,
SQLite (`sqlite+aiosqlite:///:memory:`) — no network, no Telegram, no Postgres
required.

## Totals

| | |
|---|---|
| **TOTAL** | **555** |
| **PASSED** | **555** |
| **FAILED** | **0** |
| **SKIPPED** | **0** |
| xfailed / errors | 0 |
| Wall time | 33.60 s |

## Per module (each run on its own)

| Module | Tests | Result | Time |
|---|---|---|---|
| `tests/test_admin_ui_integrity.py` | 32 | 32 passed | 1.84 s |
| `tests/test_bot_application.py` | 7 | 7 passed | 2.13 s |
| `tests/test_config.py` | 34 | 34 passed | 0.09 s |
| `tests/test_database.py` | 68 | 68 passed | 2.98 s |
| `tests/test_dedup.py` | 27 | 27 passed | 0.12 s |
| `tests/test_detector_liquidations.py` | 30 | 30 passed | 0.08 s |
| `tests/test_detector_orders.py` | 23 | 23 passed | 0.05 s |
| `tests/test_detector_positions.py` | 23 | 23 passed | 0.05 s |
| `tests/test_detector_trades.py` | 17 | 17 passed | 0.05 s |
| `tests/test_engine_pipeline.py` | 33 | 33 passed | 10.09 s |
| `tests/test_filters.py` | 33 | 33 passed | 0.05 s |
| `tests/test_global_pause.py` | 15 | 15 passed | 1.20 s |
| `tests/test_order_position_separation.py` | 20 | 20 passed | 0.05 s |
| `tests/test_permissions.py` | 48 | 48 passed | 3.51 s |
| `tests/test_persistence.py` | 29 | 29 passed | 2.97 s |
| `tests/test_resilience.py` | 50 | 50 passed | 5.57 s |
| `tests/test_telegram_handlers.py` | 66 | 66 passed | 6.94 s |

The per-module times sum to more than the full-suite wall time because each row
pays its own interpreter and fixture start-up. The wall time is also not stable
on Windows: the same suite took 396 s immediately after `compileall` rewrote
every `.pyc`, and 28.55 s on the next run, so treat a slow run as an
antivirus/filesystem artefact rather than a regression — the pass count is the
signal.

Support files (no tests of their own): `tests/conftest.py` (fixtures, fake
Telegram objects, environment scrubbing), `tests/factories.py` (builders for
Hyperliquid shapes).

## Spec §36 coverage map

| §36 requirement | Where |
|---|---|
| Whale threshold detection | `test_filters.py`, `test_detector_trades.py`, `test_engine_pipeline.py::test_a_trade_below_the_threshold_produces_nothing`, `…::test_a_raised_threshold_silences_a_previously_alertable_trade` |
| LONG position detection | `test_detector_positions.py`, `test_engine_pipeline.py::test_a_raw_trades_frame_becomes_one_formatted_whale_alert` |
| SHORT position detection | `test_detector_positions.py`, `test_engine_pipeline.py::test_a_sell_aggressor_on_a_short_renders_as_a_short` |
| Limit order detection | `test_detector_orders.py` |
| Order ≠ position separation | `test_order_position_separation.py` (a resting BUY/SELL never becomes LONG/SHORT; a fill or cancellation never moves position state; a close is measured from the last verified non-zero snapshot) |
| Order cancellation detection | `test_detector_orders.py` (cancel vs fill vs exchange-initiated cancel) |
| Coin filtering | `test_filters.py`, `test_engine_pipeline.py::test_a_coin_outside_the_filter_produces_nothing`, `…::test_an_unknown_coin_is_not_alerted` |
| Duplicate prevention | `test_dedup.py`, `test_engine_pipeline.py::test_the_same_trade_id_arriving_twice_alerts_once`, `…::test_a_second_similar_trade_is_held_by_the_cooldown`, `…::test_a_redeploy_does_not_replay_the_last_alert`, `test_admin_ui_integrity.py` (one fill observed twice; a duplicate that outlived the memory cache) |
| Admin permission checks | `test_permissions.py`, `test_admin_ui_integrity.py` (command scopes, and the same commands driven as a stranger) |
| Co-admin permission checks | `test_permissions.py` (the §29 matrix, including every action a co-admin must be refused) |
| Public / private mode | `test_permissions.py`, `test_telegram_handlers.py` |
| Telegram callback handling | `test_telegram_handlers.py` (including forged `callback_data` from an unauthorised user), `test_admin_ui_integrity.py` (no builder emits a wallet or an oversized payload) |
| Database operations | `test_database.py` (all 11 repositories, commit/rollback, unique constraints, restart/redeploy restore), `test_persistence.py` (patch semantics, defaults-only-where-absent, redeploy survival) |
| WebSocket reconnect | `test_resilience.py` (drop → reconnect → resubscribe, backoff ladder, cap, no leaked reader) |
| API failure handling | `test_resilience.py` (429 / 5xx / 4xx / timeout / unparseable body / exhausted budget) |
| Invalid command input | `test_telegram_handlers.py`, `test_config.py` |
| Unauthorized user rejection | `test_permissions.py`, `test_telegram_handlers.py` |
| Unit tests | all modules |
| Integration tests | `test_database.py`, `test_resilience.py`, `test_engine_pipeline.py`, `test_telegram_handlers.py`, `test_bot_application.py`, `test_persistence.py` |

## What the end-to-end pipeline test actually asserts

`tests/test_engine_pipeline.py` pushes a raw Hyperliquid `trades` frame into the
real websocket handler and asserts on the **final Telegram message text** and the
rows left in the database. The whole chain is production code — parser →
`_on_trade` gates → work queue → REST enrichment → tracker → detector → filter →
deduplicator → persistence → formatter → `bot.send_message`. Only the two network
seams are substituted (`ws_connect`, `HyperliquidREST._client`) and the bot is
`conftest.FakeBot`.

Notably it verifies the three distinct TP/SL states — real trigger orders render
a price, a checked wallet with no triggers renders `N/A`, and an unchecked wallet
renders `N/A (not checked)` — so an unavailable value can never be printed as a
real one.

## Beyond §36: the two audit suites

`tests/test_admin_ui_integrity.py` (32 tests) is the regression suite for the
"ADMIN UI + WALLET DISPLAY + DATA INTEGRITY" audit. Each test drives the seam
that caused the reported defect rather than the Telegram text that displayed it:

| Reported defect | Seam asserted |
|---|---|
| Wallet shown as `0x3200...c407` | `wallet_code()` output in every list view and alert body, plus the round trip through the database — nothing between the feed and the screen shortens the canonical value. `short_wallet` is allowed only in button labels. |
| Wallet not monospace | the rendered `<code>` wrapper, and that bolding is not substituted for it |
| Co-Admin List button dead | `on_callback("adm:list")` renders its own panel, distinct from the home edit, and answers the press |
| Users see admin commands | `publish_command_menus()` inspected per scope; every admin command then driven *as a stranger* and refused server-side; a demoted co-admin's chat scope deleted |
| Duplicate whale events | `identity_key()` — one `tid` observed twice pre- and post-enrichment, distinct `oid`s kept apart, and `_already_recorded` for a duplicate that outlived the in-memory cache |
| $4.60M under a $5,000,000 heading | `WhaleFilter.evaluate` at, just below and just above the per-class gate |
| "Alerts delivered: 0" with 411 events | the whole `AlertService` path with and without a subscribed recipient — the zero is explained by `no_recipients`, surfaced in `runtime_warnings()` and the status panel, not hidden |
| `N/A` owner / notional / liquidation | real `WhaleDetector` output through `container.alerts.render`: an aggregated book level never claims an owner, a snapshot-less position says so, a liquidation price is never computed |
| Full address in `callback_data` | every one of the 19 `inline.py` builders: no `0x…` payload, and ≤ 64 bytes UTF-8 |

`tests/test_global_pause.py` (15 tests) covers `/pause` and `/go`: the middleware
gate that refuses every command, button and half-finished prompt; the four
deliberate exemptions (`/go`, `/status`, `/panel`, `/stop`); that
`monitoring_enabled` is left exactly as configured; that nothing is delivered
while paused; and that the pause survives a redeploy through two fresh
`AppContainer.restore()` cycles.

## Beyond §36: persistence and settings safety

`tests/test_persistence.py` (29 tests) is the regression suite for the two
reported production symptoms: *"changing one setting replaces other settings"* and
*"settings are lost after a Railway redeploy"*. Every test asserts one of two
things — that **a change is a patch** (after it, everything the caller did not name
is unchanged), or that **a value survives a new process** reading the same
database. A restart is `SettingsService(database, env).load()`; a redeploy is a
brand-new `AppContainer(env, database).restore()`, which is what Railway does to a
container: fresh caches, fresh services, same rows.

| Group | What it asserts |
|---|---|
| A coin addition is a patch | `/addcoin HYPE` on BTC/ETH/SOL yields **BTC ETH HYPE SOL**, not HYPE; the second `/addcoin HYPE` is a no-op that says "already monitored" and creates no duplicate row; the `➕ Add coins` prompt adds; the per-coin toggle button behaves exactly like the command |
| One setting at a time | `/setthreshold` does not touch the coin list; changing the threshold leaves 11 named fields byte-identical; toggling one alert switch leaves the others and the coins alone |
| Durability | the §33 acceptance sequence (add HYPE → change threshold → restart → redeploy) survives twice over; public mode, watched wallets (untruncated), the global pause, co-admin **role**, and each alert toggle all come back |
| Defaults only where nothing exists | a stored threshold is never overwritten by the environment default; a deliberately empty coin list is not re-seeded; `bootstrapped_at` is written once and read back; an empty database *does* get the defaults |
| Nothing removed without an explicit remove | `/setcoins` replaces but names every removal; `replace` leaves the unchanged rows' `created_at` intact; 🧹 Clear needs the second tap; `/resetsettings` alone changes nothing; a confirmed reset restores defaults yet keeps wallets and co-admins, and the reset itself persists |
| Authority | a co-admin's forged `reset:confirm` is refused server-side |
| `/config` | reads the stored rows, reports cache-vs-database drift when a row is edited underneath it, prints no credential, and is refused to a stranger |
| Startup summary | counts and states only, no token or URL; `ephemeral` on SQLite, `durable` on Postgres |

The suite was mutation-checked against the actual bug: reverting the coins prompt
to `set_coins()` makes
`test_the_add_coins_prompt_adds_instead_of_replacing` fail with
`('HYPE',) != ('BTC', 'ETH', 'HYPE', 'SOL')` — the exact reported symptom.

## Defects this suite found (fixed)

### 1. Concurrent-write race dropped alerts

`WhaleEngine._persist` ran in three concurrent workers. Two events for the same
wallet in flight at once raced on the read-then-write of the `wallets` /
`positions` rows; the losing transaction failed, so the event was never persisted
**and its alert was never sent**. Reproduced by
`test_an_order_of_magnitude_larger_trade_breaks_the_cooldown` and
`test_a_zero_cooldown_lets_every_distinct_trade_through` (0 alerts instead of 2).
Fixed by `WhaleEngine._write_lock` (`app/whale/engine.py:124`), held only around
the database write and not around REST enrichment.

### 4. `/recent` was advertised publicly but published only to admins

Found by `test_admin_ui_integrity.py::test_a_normal_user_is_never_shown_an_admin_identity_or_control`,
which failed with `admin commands disclosed to a normal user: ['recent']`.

Three sources of truth are written by hand and had drifted: `data.cmd_recent` is
`@requires(Capability.VIEW_WHALES)` (public), `texts.help_text` listed `/recent`
under "Whale data" for everyone, but `BotCommand("recent", …)` sat in
`CO_ADMIN_EXTRA` — so it was published *only* to the admin chat scopes. An
ordinary user was told about a command that never appeared in their ✚ menu, and
the audit's own disclosure check read the mismatch as a leak.

Fixed in `app/bot/commands.py` by moving `recent` into `PUBLIC_COMMAND_MENU`.
Rather than re-asserting the three lists,
`test_what_is_advertised_publicly_is_exactly_what_a_user_may_invoke` now drives
every publicly advertised command as a stranger (expecting no refusal) and every
admin command as a stranger (expecting refusal), so visibility and authority
cannot drift apart again in either direction. Each command gets its own caller
id: the rate limiter is per Telegram user, and thirty-odd invocations from one id
would be throttled rather than answered.

## Defects found in production (fixed, now covered)

Both were found on the first Railway deploy, 2026-08-21 — not by this suite.
Each now has a regression test that was **verified to fail** against the code
that shipped.

### 2. Every alert was dropped: the bot was never attached

The log showed `Alert dropped: Telegram bot not attached yet` for every whale
detected, and the command menu never appeared in Telegram.
`AlertService.attach_bot()` was only reachable from the application's
`post_init` hook, but python-telegram-bot calls `post_init` **only** from
`run_polling()` / `run_webhook()` (see `Application.initialize`: *"Does not call
post_init"*). `app.main.Runtime` drives the lifecycle by hand, so the hook never
ran and `AlertService.bot` stayed `None`, which `_dispatch` treats as "drop".

Fixed in `app/bot/application.py`: `build_application()` attaches the bot
immediately, and `post_init` is now public, called explicitly by
`Runtime.start()`, and guarded against a double call.

**Why the suite missed it:** every other Telegram test drives handlers through
the duck-typed fakes in `conftest.py`, so no test had ever built the real
`Application`. `tests/test_bot_application.py` now does, and asserts
`container.alerts.bot is application.bot`.

### 3. Secret redaction corrupted `%`-style log arguments

`SecretRedactor.filter` coerced every `record.args` entry to `str`, so any
third-party log call using a numeric placeholder raised inside the formatter:

```
TypeError: %d format: a real number is required, not str
Message: 'Started server process [%d]'
Arguments: ('1',)
```

Python answers a formatter exception with a full traceback on stderr, so
uvicorn's two startup lines produced several screens of interleaved tracebacks.

Fixed in `app/utils/logging.py`: `_scrub_arg()` preserves each argument's type
and only substitutes a non-string when it genuinely contains a secret; both
formatters resolve the message through `_safe_message()`, so a malformed
third-party call degrades to one line instead of a traceback per occurrence.

**Why the suite missed it:** the redaction tests only ever used pre-formatted
messages with no `args`. Six new tests in `test_config.py` cover numeric args,
mixed args, dict-style args, secrets passed as args, secrets hidden inside
non-string args, and both formatters' degradation path.

### 5. The coin prompt replaced the whole list instead of adding to it

Reported as *"when I change one setting, the bot unnecessarily replaces other
existing settings."* Root-caused to exactly one path, not a systemic problem: the
coin panel's `✏️ Set list` button opened a prompt whose handler
(`app/bot/handlers/prompts.py`, `kind == "coins"`) called
`settings.set_coins()`. An admin monitoring BTC/ETH/SOL who answered `HYPE` was
left monitoring only HYPE — a replacement dressed up as an addition. Everything
else in the settings layer was already patch-based.

Fixed by calling `add_coins()`, relabelling the button `➕ Add coins`, and
rewording the prompt to say "add". Removal keeps its own explicit paths:
`/removecoin`, the per-coin toggle, `/setcoins`, and the now two-step 🧹 Clear.

**Why the suite missed it:** the existing prompt tests asserted that the answer
was *applied*, never that the previous list *survived*. The distinction only shows
when the pre-existing state is non-empty and different from the answer.
`tests/test_persistence.py::test_the_add_coins_prompt_adds_instead_of_replacing`
sets BTC/ETH/SOL first, and was verified to fail against the old code with
`('HYPE',)`.

## Known limitations of this suite

1. **No live Hyperliquid or Telegram traffic.** By design (spec: tests must not
   require a live connection). Real-endpoint behaviour is documented in
   `.agent/API_NOTES.md` instead of asserted here, so a breaking change to
   Hyperliquid's payload shapes would not be caught by these tests.
2. **SQLite, not PostgreSQL.** The schema is exercised on
   `sqlite+aiosqlite:///:memory:`. Alembic migration *scripts* are not executed
   by the suite; they run on boot against `DATABASE_URL`. Postgres-specific
   behaviour (e.g. concurrent-connection semantics) is therefore untested.
   `sqlite+aiosqlite` gives every session one shared connection via `StaticPool`,
   which is stricter than Postgres in some ways and looser in others.
3. **Timing is asserted through queue joins, not sleeps**, except in
   `test_resilience.py`, where the real `ExponentialBackoff` is wrapped so its
   computed delays are asserted without actually sleeping ~15 s. The delay
   *policy* is verified; the wall-clock sleep is not.
4. **No load or soak test.** Queue-full behaviour is unit-tested via counters;
   sustained 2000-messages-per-minute throughput is not measured.
5. **Docker image and Railway deployment are not tested here.** Both are verified
   by inspection (`Dockerfile`, `railway.toml`, `start.sh`) and by the `/health`
   tests in `test_resilience.py`, not by building the image in CI.
6. **The process lifecycle is not driven end to end.** `test_bot_application.py`
   covers the wiring (`build_application`, `post_init`), but no test runs
   `Runtime.start()` — that needs a real database, a real Telegram login and a
   bound port. Defect 2 above lived exactly in that gap, so treat any change to
   the startup order in `app/main.py` as untested until it has run on Railway.
