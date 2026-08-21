# ARCHITECTURE

## Application structure

```
app/
├── main.py                     entry point: env → DB → migrations → services → Telegram → ingest
├── config.py                   Settings (pydantic-settings) + validate_runtime()
├── container.py                AppContainer: owns every long-lived service
├── bot/
│   ├── application.py          builds the PTB Application, registers 32 handlers
│   ├── views.py                renders panels/status/statistics screens
│   ├── handlers/{common,admin,data,callbacks,prompts}.py
│   ├── keyboards/inline.py     inline keyboards + CB_* callback-data namespaces
│   ├── messages/texts.py       verbatim spec message templates
│   └── middleware/permissions.py  the single authorization gate
├── hyperliquid/
│   ├── constants.py            endpoint weights, WS limits, subscription names
│   ├── rest.py                 HyperliquidREST — POST /info, weight-budgeted
│   ├── websocket.py            HyperliquidWebSocket — reconnect + resubscribe
│   ├── parser.py               raw JSON → typed models
│   └── models.py               Trade, L2Book, Position, AccountState, OpenOrder, …
├── whale/
│   ├── engine.py               WhaleEngine — three-tier ingest orchestration
│   ├── detector.py             pure event classification (no I/O)
│   ├── events.py               EventType, ValueKind, WhaleEvent
│   ├── filters.py              per-class threshold + coin + toggle gate
│   ├── dedup.py                identity key + cooldown key
│   └── tracker.py              wallet scoring and focus slate
├── services/
│   ├── settings_service.py     DB-authoritative RuntimeConfig
│   ├── admin_service.py        roles + capability matrix
│   └── alert_service.py        rendering + queued delivery
├── database/{base,models,repository}.py, migrations/
└── utils/{formatting,logging,ratelimit,backoff,cache}.py
```

## Data flow

```
                    ┌──────────────── Hyperliquid ────────────────┐
                    │  wss trades + allMids   (global, wallets!)   │
                    │  POST /info             (enrichment)         │
                    │  wss orderUpdates/userEvents (focus wallets) │
                    └──────────────────────┬──────────────────────┘
                                           │
                         WhaleEngine  ─────┴─────  bounded queue (2000)
                                           │        3 worker tasks
                    ┌──────────────────────▼──────────────────────┐
                    │ WhaleDetector  → WhaleEvent (DataPoints)    │
                    │ WhaleFilter    → threshold / coin / toggle  │
                    │ Deduplicator   → identity + cooldown        │
                    │ EventRepository→ persist, get event_id      │
                    └──────────────────────┬──────────────────────┘
                                           │ alert_callback
                    ┌──────────────────────▼──────────────────────┐
                    │ AlertService: render now, queue, send later  │
                    │ → admins (+ subscribers when PUBLIC_MODE)    │
                    └─────────────────────────────────────────────┘
```

Ingestion never blocks Telegram: they are separate asyncio tasks joined only by
a bounded queue. A full queue drops the oldest candidate and increments a
counter rather than applying back-pressure to the socket.

## Hyperliquid integration (three tiers)

1. **Discovery (global WS, free).** `trades` is the only public feed that names
   wallets (`users: [buyer, seller]`); `allMids` keeps a price map for distance
   and notional maths. A trade above `threshold × DISCOVERY_FACTOR` (0.5) marks
   the wallet interesting; above the full threshold it becomes an event.
2. **Enrichment (REST, budgeted).** `clearinghouseState` (weight 2) yields
   position size, entry, leverage, liquidation price and margin;
   `frontendOpenOrders` (weight 20) is the *only* source of TP/SL;
   `orderStatus` (weight 2) resolves cancelled-vs-filled. A weighted sliding
   window keeps total spend under `REST_WEIGHT_PER_MINUTE × safety`.
3. **Focus wallets (per-wallet WS).** `orderUpdates` frames do not identify
   their user, so each focus wallet needs its own connection. Hyperliquid caps
   an IP at 10 unique addresses, so the slate is small and score-ranked
   (recency-decayed notional, pinned wallets first).

## Telegram integration

- python-telegram-bot 22.x, HTML parse mode, long polling (no webhook, so no
  public URL is required beyond the health check).
- Every command handler is wrapped by `@requires(capability)`, which resolves
  the role from `update.effective_user.id`, throttles with a token bucket, then
  injects an `actor` argument. Handlers never check permissions themselves.
- Callback data is `area:action:arg`. `on_callback` maps the area to a
  capability and re-authorises **on every press** — crafted callback data from a
  non-admin is refused with an alert, never executed.
- Multi-step input uses `context.user_data["pending"]`; the capability is
  re-checked when the answer arrives, not only when the prompt was issued.

## Database structure

PostgreSQL on Railway via `DATABASE_URL` (asyncpg). SQLite+aiosqlite is allowed
for local runs and tests only — `validate_runtime` makes it fatal in production.
Tables: `admins`, `users`, `settings`, `tracked_coins`, `tracked_wallets`,
`wallets`, `whale_events`, `orders`, `positions`, `alert_history`,
`admin_audit`, `bot_logs`. All timestamps are timezone-aware UTC. See
`DATA_MODEL.md`.

## Event processing

`WhaleEvent` is a bag of `DataPoint`s plus indexed scalars. `event_type` is one
of 13 values (`WHALE_TRADE`, `POSITION_OPENED/INCREASED/DECREASED/CLOSED/FLIPPED`,
`ORDER_PLACED/MODIFIED/CANCELLED/FILLED/PARTIALLY_FILLED/REJECTED`,
`BOOK_LEVEL`). `value_kind` records *what* the notional measures so a $4.8M
position is never compared against a $4.8M cash flow threshold.

## Whale detection

- **Trade**: notional = `px × sz`; both participants are tracked, the taker is
  the actor. Side is derived from the taker side (`"B"` → LONG/buy).
- **Position**: diff two `clearinghouseState` snapshots. First snapshot only
  records a baseline (no event) so a restart cannot fabricate "opened" events.
  Closes report `POSITION_NOTIONAL`, everything else `POSITION_DELTA`.
- **Order**: diff `frontendOpenOrders` per wallet plus live `orderUpdates`.
  A vanished order is resolved through `orderStatus`; if that is inconclusive
  the event says "outcome unresolved" instead of guessing cancel or fill.
- **Book**: an aggregated `l2Book` level above the threshold (`BOOK_LEVEL`);
  wallet unknown by construction and labelled as such.

## Admin permissions

Three roles: `MAIN_ADMIN` (from `MAIN_ADMIN_ID`, unremovable), `CO_ADMIN`
(unlimited count, added by the main admin only), `USER`. Capabilities are a
frozenset per role; `MANAGE_ADMINS` and `VIEW_AUDIT` are main-admin only. Users
get `VIEW_PUBLIC`/`VIEW_WHALES` only while `PUBLIC_MODE` is on. Every mutation
writes an `admin_audit` row (admin id, action, target, old, new, timestamp).

## Alert flow

`enqueue(event, event_id)` renders the message synchronously (so a later
settings change cannot alter an already-decided alert), then queues it. A single
sender task walks recipients with a 60 ms inter-chat delay, records every
delivery in `alert_history`, and marks users blocked on `Forbidden`.

## Railway deployment

One service, `numReplicas = 1`, Dockerfile build, `python -m app.main` start
command, `/health` health check (200 for healthy *and* degraded, 503 only when
PostgreSQL is unreachable). `app.main` runs Alembic itself, so no SSH step and
no manual background processes. Config comes only from environment variables.
