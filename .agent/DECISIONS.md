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

**DECISION:** Attach the Telegram bot to the alert service inside
`build_application()`, and treat `post_init` as identity-dependent work only.
**REASON:** python-telegram-bot calls `post_init` **only** from `run_polling()`
and `run_webhook()` — `Application.initialize()` explicitly does not. This
process manages the lifecycle itself (`initialize` → `start` →
`updater.start_polling`), so the hook never ran in production and
`AlertService.bot` stayed `None`; `_dispatch` treats that as "drop the alert", so
every whale alert was discarded and the command menu was never published.
`application.bot` exists the moment the builder returns, so attaching there
removes the ordering hazard entirely rather than moving it. `post_init` remains
for what genuinely needs `getMe` (identity logging, `set_my_commands`), is called
explicitly by `Runtime.start()`, is still registered on the builder so
`run_polling` would also work, and is guarded by a `bot_data` flag so the double
call cannot publish the menu twice.

**DECISION:** Wallet addresses are shown in full. (Supersedes the truncation
decision above.)
**REASON:** The audit reported `0x3200...c407` as unusable — a reader cannot paste
it into a block explorer, and two different wallets can share the same first and
last four characters. The canonical value is never shortened: not in the database,
not in an alert body, not in a list view. `short_wallet()` survives for one
purpose only, inline-button labels, where Telegram's 64-byte limit is a hard
constraint and the full address is one tap away anyway. Monospace and the
no-real-world-identity rule from that earlier entry both still hold.

**DECISION:** `CoinRepository.replace()` is a diff, not delete-all-then-insert.
**REASON:** The obvious implementation of "make the list exactly this" is
`DELETE FROM tracked_coins` followed by inserts, and it is wrong three ways.
It discards `added_by` and `created_at` for coins that were not changing, so the
audit trail of who started monitoring BTC is lost every time an unrelated coin is
added. It makes two admins editing concurrently destructive to each other: the
second write re-inserts the row the first just removed. And it makes every
whole-list operation look identical in the database to a genuine reset, so
"nothing was removed" is unprovable after the fact. The diff inserts what is
missing, deletes only what is genuinely no longer wanted, and **returns**
`(added, removed)` — which is what lets `/setcoins` name every coin it dropped
instead of reporting a silent success. `add`/`remove` remain the patch
operations; `replace` is reached only from `/setcoins` and the confirmed 🧹 Clear.

**DECISION:** Record a `bootstrapped_at` marker rather than inferring first boot
from an empty table.
**REASON:** "Apply defaults only when no configuration exists" needs a definition
of *exists*. An empty `tracked_coins` table is ambiguous — it is either a brand-new
installation or an admin who deliberately stopped monitoring everything — and
guessing wrong resurrects `DEFAULT_COINS` on the next redeploy, which is exactly
the reported symptom. One row turns the question into a stored fact. It is
excluded from `BOOL_KEYS` and the toggle machinery because it is a property of the
installation, not a preference, and `startup_summary()` reports the answer as
`seeded` vs `loaded` so an unexpected reset shows up in the Railway log rather
than being discovered later by a missing alert.

**DECISION:** `whale_events.dedup_key` is indexed but deliberately **not** UNIQUE.
**REASON:** A UNIQUE constraint is the tempting answer to "do not create duplicate
alerts because the WebSocket reconnects", and it would work — at the cost of
losing real events forever. Trade identity already includes the exchange `tid`, so
a genuine replay is caught by `EventRepository.seen_recently(key, since)` within
the 1-hour `IDENTITY_TTL`. What UNIQUE would additionally block is a *legitimate*
repeat: the same wallet making the identical position change to the same size on
the same coin a day later hashes to the same key, and the database would refuse to
record it. Over-deduplication is the worse failure — a duplicate alert is noise a
reader can dismiss, a missing alert is a whale they never heard about. A
time-bounded check in code can express "recently"; a UNIQUE index cannot.

**DECISION:** `/resetsettings` gets its own capability, `RESET_SETTINGS`, in
`MAIN_ONLY_CAPABILITIES` — not `CHANGE_SETTINGS`.
**REASON:** A co-admin is trusted to change any individual setting, which is what
`CHANGE_SETTINGS` means. One command that discards all of them at once is a
different kind of act, closer to `MANAGE_ADMINS` than to `/setthreshold`, and
reusing the broader capability would have made it available to every co-admin as a
side effect of a permission granted for something else. It is also two-step: the
command renders a confirmation and only `reset:confirm` acts, and that callback is
re-authorised against the pressing user's id, so a forged payload from a co-admin
hits the same refusal as the command.

**DECISION:** The secret redactor preserves the type of every `record.args`
entry; only strings are scrubbed unconditionally.
**REASON:** `record.args` is consumed as `msg % args`. Coercing entries to `str`
broke every numeric placeholder in third-party log calls — uvicorn's
`"Started server process [%d]"` raised `TypeError` inside the formatter, and
Python answers a formatter exception with a full traceback on stderr, so two
startup lines buried the real log under screens of noise. A non-string argument
is now inspected and replaced **only** when it actually contains a registered
secret, where returning a redacted string is correct regardless of the
placeholder. Leaking is still impossible; the type is preserved only in the case
where there is nothing to redact. Both formatters also route through
`_safe_message()`, so a malformed third-party template degrades to a single line
instead of a traceback per occurrence — logging must never be able to drown the
signal it exists to carry.

**DECISION:** An "execution" means a fill. `ORDER_PLACED`, `ORDER_MODIFIED` and
`ORDER_CANCELLED` are never rendered as trades, and their value is labelled
*intended*.
**REASON:** A resting limit order moved no coin. Calling it a WHALE TRADE is a
fabricated signal with a real number attached — the most damaging kind, because it
is indistinguishable from a true one at a glance. `EXECUTION_TYPE_NAMES` /
`RESTING_ORDER_TYPE_NAMES` in `app/whale/events.py` are the single place that
distinction is spelled out; alerts, `/recent`, `/whales`, the wallet leaderboard
and the statistics panel all read from them rather than repeating a type list.

**DECISION:** Detection and publication are separate switches:
`enable_order_detector` (default **on**) and `enable_order_alerts` (default
**off**).
**REASON:** Order state is a prerequisite for correct fill accounting — the
executed size of a fill is derived from the previously seen resting size — so
turning off the *noise* must not turn off the *tracking*. One flag conflated the
two, meaning an admin who silenced order chatter also blinded the fill detector.
Trade and position alerts default on; orders default off because they are the
high-volume, low-information stream.

**DECISION:** A fill's threshold is measured against `|price × executed_size|`,
not the order's original notional.
**REASON:** A 40-of-130 fill on a $95,000 limit is a $3.80M event, not a $12.35M
one. `_as_execution` rewrites `notional` and flips `value_kind` to `TRADE_VALUE`
so the trade minimum — not the order minimum — is the gate.

**DECISION:** A position side is only ever read from a `clearinghouseState`
snapshot. `Position.side` returns `None` when flat, and a flat snapshot is treated
exactly like a missing one.
**REASON:** `SELL` is not `SHORT`; it is equally the closing leg of a LONG.
Inferring the side from the fill mislabels every closed LONG as a SHORT, and
because Hyperliquid drops closed positions from `assetPositions`, a snapshot taken
mid-close arrives with `szi == 0` — reading a side off *that* fails the same way.
`_attach_position` therefore marks `position_value`, `entry_px`, `liquidation_px`
and `leverage` as `unavailable("no open position for this coin")` for a flat
snapshot rather than passing zeros along as data. A close reads its figures from
the last non-zero snapshot instead.

**DECISION:** `whale_events.dedup_key` keeps **no** UNIQUE constraint; identity is
the exchange `tid` (or `oid` for order events) plus the `seen_recently` TTL.
**REASON:** The TTL exists to absorb websocket reconnect replays, which is the
only duplication actually observed. A UNIQUE index would additionally reject
*legitimate* repeats — a wallet that genuinely increases the same position twice
in the same way produces the same natural key — and a rejected insert is
indistinguishable at the database layer from a suppressed duplicate. Silently
losing a real event is worse than occasionally storing one twice, so the looser
rule is the deliberate choice, not an oversight. Consequently no migration was
needed for this milestone: `0001_initial` and `0002_alert_thread_key` remain the
only migrations.

**DECISION:** One fill is legitimately observed twice — once from the global
`trades` feed as a `tid`-keyed `WHALE_TRADE`, once from `orderUpdates` as an
`oid`-keyed `ORDER_FILLED`.
**REASON:** The two feeds carry different information (the trades feed names both
wallets; `orderUpdates` knows the resting size the fill consumed) and neither is
derivable from the other. Collapsing them on a shared key would mean choosing
which facts to discard. They are kept apart on purpose, and the statistics panel
counts executions and order events on separate lines so the double observation
cannot inflate a single "trades" figure.
