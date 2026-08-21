# TEST STATUS

These are **actual** results from running the suite, not estimates. Reproduce
with:

```bash
python -m pytest -q
```

Last full run: **2026-08-21**
Environment: Python 3.13.3, pytest 8.4.2, `asyncio_mode = auto`,
SQLite (`sqlite+aiosqlite:///:memory:`) — no network, no Telegram, no Postgres
required.

## Totals

| | |
|---|---|
| **TOTAL** | **379** |
| **PASSED** | **379** |
| **FAILED** | **0** |
| **SKIPPED** | **0** |
| xfailed / errors | 0 |
| Wall time | 38.31 s |

## Per module (each run on its own)

| Module | Tests | Result | Time |
|---|---|---|---|
| `tests/test_config.py` | 27 | 27 passed | 0.08 s |
| `tests/test_database.py` | 68 | 68 passed | 4.58 s |
| `tests/test_dedup.py` | 21 | 21 passed | 0.12 s |
| `tests/test_detector_orders.py` | 23 | 23 passed | 0.03 s |
| `tests/test_detector_positions.py` | 23 | 23 passed | 0.03 s |
| `tests/test_detector_trades.py` | 17 | 17 passed | 0.03 s |
| `tests/test_engine_pipeline.py` | 22 | 22 passed | 7.87 s |
| `tests/test_filters.py` | 19 | 19 passed | 0.03 s |
| `tests/test_permissions.py` | 48 | 48 passed | 4.88 s |
| `tests/test_resilience.py` | 50 | 50 passed | 4.84 s |
| `tests/test_telegram_handlers.py` | 61 | 61 passed | 6.33 s |

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
| Order cancellation detection | `test_detector_orders.py` (cancel vs fill vs exchange-initiated cancel) |
| Coin filtering | `test_filters.py`, `test_engine_pipeline.py::test_a_coin_outside_the_filter_produces_nothing`, `…::test_an_unknown_coin_is_not_alerted` |
| Duplicate prevention | `test_dedup.py`, `test_engine_pipeline.py::test_the_same_trade_id_arriving_twice_alerts_once`, `…::test_a_second_similar_trade_is_held_by_the_cooldown`, `…::test_a_redeploy_does_not_replay_the_last_alert` |
| Admin permission checks | `test_permissions.py` |
| Co-admin permission checks | `test_permissions.py` (the §29 matrix, including every action a co-admin must be refused) |
| Public / private mode | `test_permissions.py`, `test_telegram_handlers.py` |
| Telegram callback handling | `test_telegram_handlers.py` (including forged `callback_data` from an unauthorised user) |
| Database operations | `test_database.py` (all 11 repositories, commit/rollback, unique constraints, restart/redeploy restore) |
| WebSocket reconnect | `test_resilience.py` (drop → reconnect → resubscribe, backoff ladder, cap, no leaked reader) |
| API failure handling | `test_resilience.py` (429 / 5xx / 4xx / timeout / unparseable body / exhausted budget) |
| Invalid command input | `test_telegram_handlers.py`, `test_config.py` |
| Unauthorized user rejection | `test_permissions.py`, `test_telegram_handlers.py` |
| Unit tests | all modules |
| Integration tests | `test_database.py`, `test_resilience.py`, `test_engine_pipeline.py`, `test_telegram_handlers.py` |

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

## Defect this suite found (fixed)

`WhaleEngine._persist` ran in three concurrent workers. Two events for the same
wallet in flight at once raced on the read-then-write of the `wallets` /
`positions` rows; the losing transaction failed, so the event was never persisted
**and its alert was never sent**. Reproduced by
`test_an_order_of_magnitude_larger_trade_breaks_the_cooldown` and
`test_a_zero_cooldown_lets_every_distinct_trade_through` (0 alerts instead of 2).
Fixed by `WhaleEngine._write_lock` (`app/whale/engine.py:124`), held only around
the database write and not around REST enrichment.

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
