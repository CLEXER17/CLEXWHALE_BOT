# DECISIONS

Append-only. Each entry is a decision that is expensive to rediscover.

---

**DECISION:** Use the global `trades` websocket feed as the whale-discovery
backbone.
**REASON:** It is the only public Hyperliquid feed that carries wallet
addresses (`users: [buyer, seller]`). `l2Book` is aggregated and anonymous, and
per-user subscriptions are capped at 10 addresses per IP, so discovery cannot
start from wallets.

**DECISION:** Never fabricate an unavailable Hyperliquid field.
**REASON:** Spec §34/§35. Every user-visible number is a `DataPoint` with
`CONFIRMED | ESTIMATED | UNAVAILABLE`. TP/SL render as
`Not publicly detectable` (or `N/A` in the compact format) when no resting
trigger order exists. Estimated values are suffixed `(est.)` so an estimate is
never presented as a trader-set value.

**DECISION:** Separate `ValueKind` per event and threshold per class.
**REASON:** Spec §7 — "cash flow" and "position notional" are not the same
number. A $3M market buy, a $3M resting order and a $3M open position are
different phenomena and get independent minimums
(`MIN_TRADE_VALUE`, `MIN_ORDER_VALUE`, `MIN_POSITION_VALUE`,
`MIN_POSITION_DELTA_VALUE`), all defaulting to `MIN_WHALE_VALUE`.

**DECISION:** A position diff with no previous snapshot records a baseline and
emits nothing.
**REASON:** Otherwise every restart would report existing positions as newly
opened whale events — fabricated signals with real numbers.

**DECISION:** A vanished order whose `orderStatus` is inconclusive is reported
as "left the book (outcome unresolved)".
**REASON:** Cancelled and filled are materially different to a reader; guessing
would be inventing data.

**DECISION:** One WS connection per focus wallet, and cap the slate at 8.
**REASON:** `orderUpdates` frames do not name their user, so multiplexing is
impossible, and Hyperliquid limits an IP to 10 unique subscribed addresses and
10 connections. Two are reserved for the global market socket and headroom.

**DECISION:** PostgreSQL holds all persistent state; nothing critical lives only
in RAM.
**REASON:** Spec §48. A Railway redeploy must restore main admin, co-admins,
public mode, monitoring switch, threshold, coins, cooldown, alert settings,
tracked wallets and event history. `AppContainer.restore()` reloads them.

**DECISION:** Environment seeds settings once; the database is authoritative
afterwards.
**REASON:** An admin who lowers the threshold from Telegram must not have it
silently reset by the old env var on the next deploy.

**DECISION:** Authorization happens in exactly one place
(`app/bot/middleware/permissions.py`) and again inside `on_callback`.
**REASON:** Spec §30 — a user must never trigger an admin action by crafting
callback data. The button that produced the callback is irrelevant; the user id
attached to the press is what is checked.

**DECISION:** Store the Telegram **user id**, never the username, as the admin
identifier. `/addadmin @name` is refused with an explanation.
**REASON:** Spec §14 — usernames are re-assignable, ids are not.

**DECISION:** `/start` is the only unguarded handler.
**REASON:** A private bot still has to be able to tell a stranger that it is
private; every other entry point goes through the gate.

**DECISION:** Two-gate deduplication — an identity key (exact event) plus a
cooldown key that buckets notional by order of magnitude.
**REASON:** Spec §19. The identity key stops literal repeats; the magnitude
bucket stops a wallet nudging a position by 0.1% and re-alerting 30 times.

**DECISION:** Render the alert text at enqueue time, not at send time.
**REASON:** A threshold or coin change while the queue drains must not rewrite
history, and rendering off the sender task keeps delivery latency flat.

**DECISION:** `/health` returns 200 for `degraded`.
**REASON:** Spec §47. A disconnected Hyperliquid socket is a data outage, not an
application failure; failing the health check would make Railway restart the
process and lose the reconnect backoff for no benefit.

**DECISION:** Alembic migrations run inside `app.main` before anything touches
the schema.
**REASON:** Spec §46/§52 — deployment must be push-to-deploy with no SSH step
and no manually started processes.

**DECISION:** Single Railway service, `numReplicas = 1`.
**REASON:** Spec §53. Deduplication is per process and the Hyperliquid limits
are per IP, so a second replica would double-send every alert and compete for
the same subscription budget.

**DECISION:** Wallet addresses are rendered in Telegram monospace, truncated as
`0x1234...abcd`, and never labelled with a real-world identity.
**REASON:** Spec §20 — attribution requires independent verification the bot
does not have.

**DECISION:** Monitoring windows (2M…4H) are event-observation windows.
**REASON:** Spec §6 — the underlying data does not support presenting them as
candle timeframes, so wording is always "opened 3 minutes ago" / "active for
20 minutes".

**DECISION:** Hand-write the initial Alembic migration instead of using
autogenerate output.
**REASON:** Autogenerate ran against SQLite and emitted a SQLite-flavoured
schema. The hand-written `0001_initial.py` uses `sa.func.now()` and
`sa.true()/sa.false()` so it applies identically on PostgreSQL, and it survives
a `downgrade base` → `upgrade head` round-trip.

**DECISION:** Duck-typed fake `Update`/`Context` objects in tests instead of
real PTB objects.
**REASON:** Handlers only touch `effective_user`, `effective_chat`,
`callback_query`, `effective_message`, `args`, `user_data`, `bot_data`, `bot`.
Real PTB objects would drag in a live `Bot` for `query.answer()`.

**DECISION:** Pin `*.sh` and `Dockerfile` to LF in `.gitattributes`.
**REASON:** The project is developed on Windows; a CRLF shebang fails on Linux
with "bad interpreter".

**DECISION:** Serialise the engine's database write with a single
`asyncio.Lock` (`WhaleEngine._write_lock`) rather than making the repositories
upsert-safe.
**REASON:** Three ingest workers run concurrently and two events for the same
wallet can be in flight at once. `WalletRepository.record_activity` and the
position write are read-then-write, so unsynchronised sessions race: the losing
transaction fails (`StaleDataError` on SQLite, unique-key `IntegrityError` on
PostgreSQL), `_persist` returns `None`, `dedup.forget()` runs — and the alert is
silently never sent. Found by `tests/test_engine_pipeline.py`
(`test_an_order_of_magnitude_larger_trade_breaks_the_cooldown` produced 0 alerts
instead of 2). The lock is held only around the write, never around REST
enrichment, so it costs no throughput on the slow path. A dialect-specific
upsert was rejected because the same code must run on SQLite locally and
PostgreSQL in production.

**DECISION:** Assert pipeline timing with queue joins, never `asyncio.sleep`.
**REASON:** `tests/test_engine_pipeline.py` waits on `engine.trades_seen`, then
`engine._queue.join()` and `alerts._queue.join()`. This makes "no alert was
produced" a real assertion instead of a race, and keeps the end-to-end suite
deterministic and fast.
