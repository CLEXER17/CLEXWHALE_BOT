# CLEXER Hyperliquid Whale Monitor

A production Telegram bot that watches **Hyperliquid perpetuals** for whale
activity — large market trades, large resting orders, position opens / increases
/ decreases / closes, order cancellations and liquidations — and pushes formatted
alerts to Telegram.

Every number in every alert comes from Hyperliquid's public API. Nothing is
simulated, randomised, back-filled from a placeholder, or hardcoded. When a data
point genuinely is not available from Hyperliquid, the bot prints `N/A` (with the
reason) instead of inventing a value. See
[Hyperliquid: what is and is not available](#hyperliquid-what-is-and-is-not-available)
— that section is the honest part of this README and is worth reading before you
trust a field.

---

## Contents

1. [Features](#features)
2. [How it works](#how-it-works)
3. [Telegram commands](#telegram-commands)
4. [Roles and permissions](#roles-and-permissions)
5. [Local installation](#local-installation)
6. [Environment variables](#environment-variables)
7. [Creating the bot with BotFather](#creating-the-bot-with-botfather)
8. [Pushing to GitHub](#pushing-to-github)
9. [Deploying to Railway](#deploying-to-railway)
10. [Health checks](#health-checks)
11. [Hyperliquid: what is and is not available](#hyperliquid-what-is-and-is-not-available)
12. [Tests](#tests)
13. [Project layout](#project-layout)
14. [Security](#security)

---

## Features

**Detection**

* Large **market trades** (executed trade value) from the global `trades`
  websocket feed — the only public Hyperliquid feed that is both market-wide and
  wallet-attributed.
* Large **LONG / SHORT positions**, with entry price, position notional, margin
  used, leverage (and cross/isolated), unrealised PnL and liquidation price —
  read from the wallet's `clearinghouseState`.
* Position **opened / increased / decreased / closed** transitions, by comparing
  successive position snapshots.
* Large **resting limit orders**, including trigger metadata, from
  `frontendOpenOrders`.
* **Order lifecycle** — placed, modified, filled, cancelled, rejected — for
  admin-tracked focus wallets, with `orderStatus` used to tell a real
  cancellation apart from a fill.
* **Liquidations** for focus wallets (Hyperliquid has no global liquidation
  feed — see the limitations section).
* Unusually large **aggregate resting depth** at a single price level
  (opt-in, `ENABLE_BOOK_SCANNER`) — reported as aggregate, never attributed to
  one trader.

**Control**

* Configurable USD threshold (`MIN_WHALE_VALUE`, default `2000000`), with
  optional per-category overrides for trades, positions, orders and position
  deltas.
* Coin filter: `/setcoins`, `/addcoin`, `/removecoin`, `/coins`, `/allcoins`.
  A coin that is not a Hyperliquid perp is rejected at the point of entry.
* Inline **🐋 WHALE CONTROL PANEL** covering monitoring, thresholds, coins,
  windows, admins and access mode.
* `/startmonitor` and `/stopmonitor` — monitoring state is stored in the
  database, so it survives a redeploy.
* **Public / private mode.** Private is the default; unauthorised users get one
  refusal and nothing else.
* **Deduplication** on two axes — trade identity (`tid`) and a per-coin,
  per-magnitude cooldown — warmed from the database on boot so a restart does
  not replay the last alerts.

**Operations**

* Single Railway service, single process: Telegram bot, Hyperliquid ingestion,
  and the HTTP health server all run in one asyncio application.
* Alembic migrations run automatically on boot.
* `/health` reporting `healthy` / `degraded` / `unhealthy`.
* Weighted REST rate limiter matched to Hyperliquid's published budget, and a
  websocket that reconnects with jittered exponential backoff and replays its
  full subscription set.
* Structured logging (optional JSON), with secrets never logged.
* 379 automated tests, no network required.

---

## How it works

```
             ┌──────────────────────── Hyperliquid (public, unauthenticated) ───────────────────────┐
             │  WS trades (global, attributed)   WS allMids   WS l2Book   WS orderUpdates/userEvents │
             │  REST /info: metaAndAssetCtxs · clearinghouseState · frontendOpenOrders · orderStatus │
             └───────────────────────────────────────┬───────────────────────────────────────────────┘
                                                     │
   parser ──► pre-gate (DISCOVERY_FACTOR) ──► work queue (3 workers) ──► REST enrichment ──► tracker
                                                     │
                                          detector (event classification)
                                                     │
                               filters (threshold · coin · monitoring enabled)
                                                     │
                                     dedup (identity key + cooldown key)
                                                     │
                              persistence (12 tables) ──► formatter ──► Telegram
```

The three-tier design exists because of one hard constraint: Hyperliquid exposes
**no global feed of positions, resting orders or liquidations**. Only `trades` is
market-wide *and* tells you whose trade it was. Everything position-shaped must be
requested per wallet, under a shared rate-limit budget, and websocket user
subscriptions are capped at **10 unique addresses per IP**. So the bot discovers
candidates from the global trade feed, spends its REST budget enriching only the
wallets that might matter, and reserves the scarce per-wallet websocket slots for
a small admin-chosen "focus slate".

Full per-endpoint detail, including every field that is *not* available, is in
[`.agent/API_NOTES.md`](.agent/API_NOTES.md).

---

## Telegram commands

Public commands work for anyone **only when public mode is on**; otherwise every
command below is admin-only.

| Command | Who | What |
|---|---|---|
| `/start` | anyone | Open the monitor / register |
| `/help` | anyone | List available commands |
| `/about` | public | What this bot does and its data sources |
| `/panel` | admin | Open the 🐋 WHALE CONTROL PANEL |
| `/status` | public | Monitoring status, connection state, counters |
| `/whales` | public | Recent whale events |
| `/recent` | public | Most recent alerts |
| `/orders` | public | Large resting orders currently tracked |
| `/positions` | public | Tracked open positions |
| `/coins` | public | Monitored coins |
| `/stats` | admin | Engine, alert, database and rate-limit statistics |
| `/wallets` | admin | Watched wallets |
| `/startmonitor` | admin | Start Hyperliquid monitoring |
| `/stopmonitor` | admin | Stop monitoring (state persists) |
| `/threshold` | admin | Show the current thresholds |
| `/setthreshold <usd>` | admin | Set the minimum whale value |
| `/cooldown <seconds>` | admin | Set the per-alert cooldown |
| `/settings` | admin | Show all effective settings |
| `/setcoins <A,B,C>` | admin | Replace the coin filter |
| `/addcoin <COIN>` | admin | Add a coin |
| `/removecoin <COIN>` | admin | Remove a coin |
| `/allcoins <on\|off>` | admin | Monitor every perp, or only the filter |
| `/watch <0x…>` | admin | Add a wallet to the focus slate |
| `/unwatch <0x…>` | admin | Remove a watched wallet |
| `/public <on\|off>` | admin | Toggle public mode |
| `/admins` | admin | List admins |
| `/addadmin <telegram_id>` | **main admin only** | Add a co-admin |
| `/removeadmin <telegram_id>` | **main admin only** | Remove a co-admin |
| `/audit` | **main admin only** | Recent admin audit log |

In private mode, an unauthorised user receives exactly:

```
🔒 This bot is currently private.
Whale monitoring is available only to authorized administrators.
```

Most commands that take an argument also work without one: they open the matching
panel view or an inline prompt instead of returning a usage error. Unknown
commands and malformed arguments get a specific message, never a silent failure.

---

## Roles and permissions

Three roles, enforced in `app/services/admin_service.py`:

| Capability | MAIN_ADMIN | CO_ADMIN | USER |
|---|---|---|---|
| View signals / whale events | ✅ | ✅ | only in public mode |
| Start / stop monitoring | ✅ | ✅ | ❌ |
| Change threshold / cooldown / settings | ✅ | ✅ | ❌ |
| Change coin filter | ✅ | ✅ | ❌ |
| Change public mode | ✅ | ✅ | ❌ |
| Manage watched wallets | ✅ | ✅ | ❌ |
| View statistics | ✅ | ✅ | ❌ |
| View admin list | ✅ | ✅ | ❌ |
| **Add / remove co-admin** | ✅ | ❌ | ❌ |
| **View audit log** | ✅ | ❌ | ❌ |
| **Change or remove the main admin** | ❌ | ❌ | ❌ |

Design rules that are enforced by code, not convention:

* The main admin is whoever `MAIN_ADMIN_ID` says. **No code path** writes
  `MAIN_ADMIN` to another row or deletes that row — the role cannot be
  transferred, demoted or removed through any command or callback.
* Admins are identified by **Telegram user id**, never by username. Usernames
  change; ids do not.
* Every authorisation check reads the user id from the Telegram *update object*,
  never from callback data or command arguments. A hand-crafted
  `callback_data` string cannot escalate privileges — a forged admin callback
  from a non-admin is refused, and there is a test for exactly that.
* Every mutation is written to an audit table with the acting Telegram id.

---

## Local installation

Requires **Python 3.13**.

```bash
git clone https://github.com/CLEXER17/CLEXWHALE_BOT.git
```

```bash
cd CLEXWHALE_BOT
```

```bash
python -m venv .venv
```

```bash
source .venv/bin/activate
```

On Windows PowerShell the activation command is `.\.venv\Scripts\Activate.ps1`;
in Git Bash it is `source .venv/Scripts/activate`.

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Edit `.env` and fill in `BOT_TOKEN` and `MAIN_ADMIN_ID`. Leave `DATABASE_URL`
empty to use a local SQLite file (`sqlite+aiosqlite:///./whalebot.db`) — no local
PostgreSQL is needed for development.

```bash
python -m app.main
```

Migrations run on start-up. The health endpoint is then at
`http://localhost:8080/health` (or whatever `PORT` you set).

For development tooling and the test suite:

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest -q
```

---

## Environment variables

[`.env.example`](.env.example) is the source of truth — it lists every variable
with its default. Never commit a filled-in `.env`, and never put a real token or
database URL in this README, in `.env.example`, or in any `.agent/` file.

**Required in production.** The application refuses to start if either is missing
or malformed; it does **not** fall back to a default or a fake credential.

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Telegram bot token from BotFather. Secret. |
| `MAIN_ADMIN_ID` | Your numeric Telegram user id. Becomes the permanent main admin. |

**Database**

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | *(empty)* | Injected by Railway when you attach PostgreSQL. Empty locally ⇒ SQLite file. No host, user, password, port or database name is hardcoded anywhere. |
| `RUN_MIGRATIONS` | `true` | Run Alembic migrations automatically on boot. |

**Access**

| Variable | Default | Description |
|---|---|---|
| `PUBLIC_MODE` | `false` | `false` = private (admins only). Runtime toggling via `/public` is stored in the database and wins after first boot. |

**Thresholds**

| Variable | Default | Description |
|---|---|---|
| `MIN_WHALE_VALUE` | `2000000` | Base USD threshold. |
| `MIN_TRADE_VALUE` | *(empty)* | Override for executed market trades. |
| `MIN_POSITION_VALUE` | *(empty)* | Override for position notional. |
| `MIN_ORDER_VALUE` | *(empty)* | Override for resting orders. |
| `MIN_POSITION_DELTA_VALUE` | *(empty)* | Override for position increase/decrease size. |

Empty overrides inherit `MIN_WHALE_VALUE`. These four are separate on purpose: a
trade value, a position notional, an order notional and a position delta are
**four different numbers** and are never treated as interchangeable.

**Alerting**

| Variable | Default | Description |
|---|---|---|
| `ALERT_COOLDOWN_SECONDS` | `30` | Per coin/direction/magnitude cooldown. `0` disables. |
| `ALERT_RATE_PER_MINUTE` | `20` | Outbound Telegram cap. |

**Coins**

| Variable | Default | Description |
|---|---|---|
| `DEFAULT_COINS` | `BTC,ETH,SOL` | Initial coin filter (first boot only; then the database wins). |
| `MONITOR_ALL_COINS` | `false` | Ignore the filter and monitor every perp. |
| `MAX_MONITORED_COINS` | `40` | Subscription ceiling. |

**Feature toggles**

| Variable | Default | Description |
|---|---|---|
| `ENABLE_TRADE_DETECTOR` | `true` | Large executed market trades. |
| `ENABLE_POSITION_DETECTOR` | `true` | Position open / increase / decrease / close. |
| `ENABLE_ORDER_DETECTOR` | `true` | Large resting and trigger orders. |
| `ENABLE_ORDER_CANCEL_ALERTS` | `true` | Alert when a tracked whale order is cancelled. |
| `ENABLE_WALLET_TRACKING` | `true` | Per-wallet focus-slate streams and enrichment. |
| `ENABLE_BOOK_SCANNER` | `false` | Off by default: `l2Book` size cannot be attributed to a wallet. |

**Hyperliquid and pacing**

| Variable | Default | Description |
|---|---|---|
| `HYPERLIQUID_API_URL` | `https://api.hyperliquid.xyz` | REST base. |
| `HYPERLIQUID_WS_URL` | `wss://api.hyperliquid.xyz/ws` | Websocket base. |
| `REST_WEIGHT_PER_MINUTE` | `1200` | Hyperliquid's published per-IP budget. |
| `REST_WEIGHT_SAFETY` | `0.5` | Fraction of the budget this bot allows itself. |
| `WS_FOCUS_WALLETS` | `8` | Per-wallet websocket slots used, out of Hyperliquid's hard limit of 10 per IP. |
| `POSITION_POLL_INTERVAL` | `20` | Seconds between position refreshes. |
| `ORDER_POLL_INTERVAL` | `45` | Seconds between order refreshes. |
| `BOOK_POLL_INTERVAL` | `30` | Seconds between book scans. |
| `WALLET_CACHE_SIZE` | `400` | Wallets held in memory. |
| `WALLET_IDLE_TTL` | `3600` | Seconds before an idle wallet is evicted. |

No Hyperliquid credential exists anywhere in this list — every endpoint used is a
public read endpoint. The bot never signs a transaction and never trades.

**Runtime**

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | |
| `LOG_JSON` | `false` | JSON log lines, useful on Railway. |
| `PORT` | `8080` | Railway injects this; the health server binds to it. |

---

## Creating the bot with BotFather

1. Open [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`.
2. Choose a display name, then a username ending in `bot`.
3. BotFather replies with an **HTTP API token**. That token is a credential —
   put it in `BOT_TOKEN`, in your local `.env` or in Railway's variables. Never
   paste it into a commit, an issue, a chat message or a log line. If it leaks,
   use `/revoke` in BotFather immediately.
4. Optional but recommended: `/setprivacy` → **Disabled** is *not* needed — this
   bot works in DMs and does not read group messages.
5. Get your own numeric Telegram id (for example from
   [@userinfobot](https://t.me/userinfobot)) and set it as `MAIN_ADMIN_ID`. Use
   the numeric id, not your `@username`.
6. Start the bot, then send `/start` in a DM. The command menu is registered
   automatically on boot.

---

## Pushing to GitHub

This repository lives at
**<https://github.com/CLEXER17/CLEXWHALE_BOT>** and `main` already tracks it.
Day to day, that means:

```bash
git push
```

If you are setting up a fresh clone or a new remote instead:

```bash
git remote add origin https://github.com/CLEXER17/CLEXWHALE_BOT.git
```

```bash
git push -u origin main
```

Before every push, confirm nothing sensitive is staged:

```bash
git status --short
```

[`.gitignore`](.gitignore) already excludes `.env`, `.env.*`, `*.key`, `*.pem`,
`secrets/`, `credentials/`, `__pycache__/`, `.pytest_cache/`, `.venv/`, `venv/`
and local `*.db` files. Keep it that way. **The Telegram bot token and the
database credentials must never appear in GitHub.**

---

## Deploying to Railway

The whole application is **one** Railway service. Do not split the bot,
the websocket ingestion and the health server into separate services: the
Hyperliquid websocket limits are *per IP*, so multiple instances would compete
for the same 10 user-subscription slots and the same REST weight budget.
`railway.toml` pins `numReplicas = 1` for the same reason.

1. **Push the repository to GitHub** — already done:
   <https://github.com/CLEXER17/CLEXWHALE_BOT>.
2. **Create the project.** In Railway: *New Project → Deploy from GitHub repo →*
   select `CLEXER17/CLEXWHALE_BOT`. Railway detects
   [`Dockerfile`](Dockerfile) via [`railway.toml`](railway.toml)
   (`builder = "DOCKERFILE"`).
3. **Provision PostgreSQL.** In the same project: *New → Database → Add
   PostgreSQL*.
4. **Attach the database.** On the bot service, add a variable reference so
   `DATABASE_URL` points at the Postgres service (Railway offers this as
   `${{Postgres.DATABASE_URL}}`). Do not paste a literal connection string, and
   do not type a host, user, password or port anywhere.
5. **Set the variables.** On the bot service, add at minimum:
   * `BOT_TOKEN` — from BotFather
   * `MAIN_ADMIN_ID` — your numeric Telegram id

   Everything else has a working default; override any of the variables from
   [Environment variables](#environment-variables) as needed. `PORT` is injected
   by Railway — leave it alone.
6. **Deploy.** Railway builds the Dockerfile and runs
   `python -m app.main`. There is nothing to start by hand: no SSH, no
   `alembic upgrade` step, no separate worker command, no cron job, and no
   background script. On boot the process
   * validates the environment and fails loudly if a required variable is
     missing (it will **not** continue with a placeholder credential),
   * runs Alembic migrations against `DATABASE_URL`,
   * starts the HTTP health server on `PORT`,
   * starts Hyperliquid ingestion,
   * starts long-polling Telegram.
7. **Watch the health check.** `railway.toml` sets `healthcheckPath = "/health"`
   with a 120 s timeout, and restarts on failure up to 10 times.
8. **Verify in Telegram.** DM the bot `/start`, then `/status`. `/status` should
   report the connection state and the active coin filter. `/startmonitor` begins
   alerting.

The application has no dependency on Windows paths, local files for persistent
state, `localhost` services, a local database, or any file outside this
repository. All durable state — settings, admins, coins, watched wallets,
monitoring on/off, dedup keys, event history — lives in PostgreSQL, so a
redeploy resumes exactly where it left off rather than replaying old alerts.

---

## Health checks

`GET /health` returns JSON and one of three states:

| State | HTTP | Meaning |
|---|---|---|
| `healthy` | 200 | Database reachable, Hyperliquid websocket connected. |
| `degraded` | 200 | The bot answers, but Hyperliquid is disconnected or ingestion has not started, so no new whale data is arriving. Reconnection continues in the background — this deliberately does **not** fail the deploy. |
| `unhealthy` | 503 | The database is unreachable; nothing works. |

The payload also carries `reasons`, `uptime_seconds`, `database`,
`hyperliquid.connected`, `hyperliquid.rest_healthy`, `monitoring_enabled`,
`public_mode`, `alerts` and any start-up `warnings`. It contains no secrets.

---

## Hyperliquid: what is and is not available

Read this before trusting a field. The bot is deliberately blunt about gaps
rather than filling them in.

**Reliably available (exchange-reported):**

* Coin, price, size and therefore **trade value** (`px × sz`), plus the wallet
  addresses of both sides of a trade, from the global `trades` feed. `side` is
  the *taker* side.
* For a wallet the bot has enriched, from `clearinghouseState`: position side and
  size, entry price, **position notional**, **margin used**, unrealised PnL,
  leverage with cross/isolated, and **liquidation price**.
* For a wallet the bot has enriched, from `frontendOpenOrders`: resting orders
  with `orderType`, `reduceOnly`, `isTrigger`, `triggerPx` and `isPositionTpsl` —
  the only verified source of real trader-set **TP / SL**.
* Order placed / modified / filled / cancelled / rejected, for the ≤10 focus
  wallets, from `orderUpdates` plus `orderStatus`.

**Not available, and never fabricated:**

* **No global position feed.** Positions are per wallet, so "every whale
  position on Hyperliquid" is not obtainable. The bot covers the wallets its
  rate-limit budget can reach.
* **No global pending-order feed.** `l2Book` shows aggregate size per price level
  (≤20 levels per side) with **no wallet attribution** — `n > 1` means several
  traders. Book events are reported as aggregates and never as one whale's order.
* **No global liquidation feed.** A liquidation is only visible as a per-wallet
  fill carrying a `liquidation` object, i.e. after the fact, and only for
  subscribed addresses (10 per IP maximum).
* **TP / SL for a wallet that was not individually queried.** There is no bulk
  variant of the request. The bot distinguishes three states and never blurs
  them: a real price, `N/A` (checked, none set), and `N/A (not checked)`.
* **Liquidation price when Hyperliquid returns `null`** — which happens for some
  cross positions. It is printed as `N/A`, never computed or guessed.
* **On-chain position open time.** What the bot shows as `Observed:` is derived —
  time since *this monitor* first saw the position — and is labelled as such.
* **Real-world identity of a wallet.** No alert claims a wallet belongs to a
  named person or company.
* **`orderType` on the `orderUpdates` websocket feed**, so TP/SL cannot be
  classified from that feed alone.

**Two distinctions the bot refuses to collapse:**

* *Cash flow ≠ position notional.* Trade value, position notional, order
  notional, margin used, funding and liquidation value are separate numbers with
  separate labels. Margin (`marginUsed`) is not position value
  (`positionValue`), and neither is the value of the trade that moved it.
* *Observation windows ≠ candles.* The 2M / 3M / 4M / 5M / 10M / 20M / 30M / 1H /
  4H selectors are **event and position observation windows**. Hyperliquid has no
  2m, 4m, 10m or 20m candles, so these are never presented as candle timeframes.

Also, an order is not called a "whale order" merely because its price is far from
the market — size and notional decide that.

Per-endpoint detail, with verification dates and every field marked
`NOT AVAILABLE FROM THIS DATA SOURCE`, is in
[`.agent/API_NOTES.md`](.agent/API_NOTES.md).

---

## Tests

```bash
python -m pytest -q
```

**379 tests, all passing, no network access required** — no live Hyperliquid
connection and no Telegram API. The two network seams are substituted at the
boundary; everything inside them is production code.

| Module | Tests | Covers |
|---|---|---|
| `test_config.py` | 27 | Env parsing, missing required vars, invalid values |
| `test_database.py` | 68 | All 12 tables and 11 repositories, commit/rollback, restart restore |
| `test_dedup.py` | 21 | Identity keys, cooldowns, magnitude buckets |
| `test_detector_trades.py` | 17 | Large market trade classification |
| `test_detector_positions.py` | 23 | LONG/SHORT, open/increase/decrease/close |
| `test_detector_orders.py` | 23 | Limit orders, TP/SL classification, cancel vs fill |
| `test_filters.py` | 19 | Threshold and coin filtering |
| `test_permissions.py` | 48 | The role matrix, including every action a co-admin must be refused |
| `test_resilience.py` | 50 | Websocket reconnect, backoff, REST 429/5xx/timeout, `/health` |
| `test_telegram_handlers.py` | 61 | Commands, callbacks, forged callback data, unauthorised users, invalid input |
| `test_engine_pipeline.py` | 22 | End-to-end: raw `trades` frame → final Telegram message text |

Current results, per-module timings and the suite's known limitations are
recorded in [`.agent/TEST_STATUS.md`](.agent/TEST_STATUS.md).

---

## Project layout

```
app/
  main.py             entry point: env validation, migrations, health server, bot, ingestion
  container.py        dependency wiring and the /health payload
  config.py           typed settings from the environment
  bot/                Telegram application, handlers, keyboards, formatters
  whale/              engine, parser, detector, tracker, filters, dedup
  hyperliquid/        REST client, websocket client, parser, constants, models
  services/           admin/permissions, settings, alerts
  database/           models, repositories, session factory, Alembic migrations
  utils/              logging, rate limiting, backoff, formatting
tests/                379 tests + fixtures and factories
.agent/               project memory: state, decisions, architecture, API notes, test status
Dockerfile            python:3.13-slim, non-root, health check
railway.toml          single service, Dockerfile builder, /health, 1 replica
alembic.ini           migration config (URL comes from DATABASE_URL)
.env.example          every variable, no real values
```

One canonical implementation per responsibility — there is no `detector2.py`,
`_new.py` or `_final.py` variant anywhere in `app/`.

---

## Security

* **No credential is hardcoded.** The Telegram token, the database URL and every
  other secret come from the environment. There is no default token, no default
  database host, user, password or port, and no committed credential file.
* **Fail loudly, never fake.** A missing required production variable stops
  start-up with a clear message instead of silently continuing with a
  placeholder.
* **Secrets are never echoed.** Not in Telegram messages, not in `/health`, not
  in logs. The token is not logged even at `DEBUG`.
* **Telegram user ids are untrusted until authorised.** Authorisation is resolved
  from the update object, so callback data cannot be crafted to reach an admin
  action.
* **The main admin cannot be changed or removed** by any command or callback.
* **No trading capability.** Only public Hyperliquid read endpoints are used; the
  bot holds no key material and can neither place nor cancel an order.
* **The container runs as a non-root user** (uid 10001).
* `.agent/` documentation contains no secrets, tokens, keys or personal
  credentials.
