# LIVE VERIFICATION RUNBOOK

Offline state at the time of writing: commit `d0194d7`, **587 tests passed / 0
failed**, `compileall` clean, working tree clean.

This file is the script for moving **OFFLINE VERIFIED → LIVE VERIFIED**. It exists
because most of the remaining checks cannot be performed by an agent: they need a
Telegram account acting as main admin, a second account acting as co-admin, a
third acting as a stranger, and real Hyperliquid fills arriving in real time.

**Rule for every step: record the evidence, then judge.** A step with no captured
output is `NOT VERIFIED`, never `PASS`.

---

## Blocking finding — no deployed service exists

Checked with the Railway CLI (already authenticated as `clexer123@gmail.com`,
workspace *clexer2's Projects*):

| Project | Services | Is it this bot? |
|---|---|---|
| `noble-creation` | `Postgres`, `CENTRAL API`, `CO3` | **No.** `CENTRAL API` builds `CLEXER17/CLEXER_BOT`; its variables are `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `ENCRYPTION_KEY`… `CO3` is an Aerolink/Gemini/Anthropic app. Neither has `BOT_TOKEN`, `MAIN_ADMIN_ID`, `MIN_WHALE_VALUE` or `HYPERLIQUID_WS_URL`. |
| `devoted-unity` | *none* | No services at all. |

Variable **names** only were read; no value was printed or stored.

So `CLEXER17/CLEXWHALE_BOT` is **not currently deployed** in this account. Steps 3
onward cannot start until a service exists. Creating it needs the `BOT_TOKEN`,
which must be typed into the Railway dashboard by a human and never into a chat,
a commit, or this file.

---

## Step 2 — configuration audit (done offline, no deployment needed)

| Check | Result |
|---|---|
| Boot fails loudly on a missing credential | **PASS** — `app/config.py:25` `REQUIRED_PRODUCTION_VARS = ("BOT_TOKEN", "MAIN_ADMIN_ID", "DATABASE_URL")`, enforced at `config.py:195+`. No silent fallback. |
| `BOT_TOKEN` shape validated | **PASS** — `_BOT_TOKEN_RE` rejects anything that is not `\d{6,}:[A-Za-z0-9_-]{30,}`. |
| SQLite refused in production | **PASS** — a `DATABASE_URL` pointing at SQLite is a boot error, not a warning. |
| No hardcoded credential anywhere in `app/`, `scripts/`, `Dockerfile`, `start.sh`, `alembic.ini`, `railway.toml` | **PASS** — scanned for BotFather token shapes, `postgres://user:pass@`, `sk-…`. Clean. |
| `.env` cannot be committed | **PASS** — `.gitignore:2` `.env`, `:3` `.env.*`; no `.env` exists locally; `*.db` ignored so `_boot.db` is untracked. |
| Migrations run on boot | **PASS** — `railway.toml` `startCommand = "python -m app.main"`; migrations run inside `app.main` before anything touches the schema. |
| Healthcheck | **PASS** — `healthcheckPath = "/health"`, 120 s timeout; 200 for healthy *and* degraded, 503 only when the database is unreachable. |
| Single replica | **PASS** — `numReplicas = 1`; a second replica would double every alert. |
| **Token rotation** | **NOT VERIFIED** — an agent cannot tell whether the exposed token was revoked. Only @BotFather knows. Confirm it yourself before Step 3. |

### Variables the service must have

Required, no default: `BOT_TOKEN`, `MAIN_ADMIN_ID`, `DATABASE_URL`
(set it to `${{Postgres.DATABASE_URL}}` — a reference, never a pasted string).

Everything else has a working default and is optional: `RUN_MIGRATIONS`,
`PUBLIC_MODE`, `MIN_WHALE_VALUE`, `MIN_TRADE_VALUE`, `MIN_POSITION_VALUE`,
`MIN_ORDER_VALUE`, `MIN_POSITION_DELTA_VALUE`, `ALERT_COOLDOWN_SECONDS`,
`ALERT_RATE_PER_MINUTE`, `DEFAULT_COINS`, `MONITOR_ALL_COINS`,
`MAX_MONITORED_COINS`, `ENABLE_TRADE_DETECTOR`, `ENABLE_POSITION_DETECTOR`,
`ENABLE_ORDER_DETECTOR`, `ENABLE_ORDER_CANCEL_ALERTS`, `ENABLE_WALLET_TRACKING`,
`ENABLE_BOOK_SCANNER`, `HYPERLIQUID_API_URL`, `HYPERLIQUID_WS_URL`,
`REST_WEIGHT_PER_MINUTE`, `REST_WEIGHT_SAFETY`, `WS_FOCUS_WALLETS`,
`POSITION_POLL_INTERVAL`, `ORDER_POLL_INTERVAL`, `BOOK_POLL_INTERVAL`,
`WALLET_CACHE_SIZE`, `WALLET_IDLE_TTL`, `LOG_LEVEL`, `LOG_JSON`, `PORT`.

Note there is **no** `ENABLE_ORDER_ALERTS` variable, and that is deliberate:
order alerts are a runtime setting stored in the database and flipped from the
alert-settings panel, so an admin's choice survives a redeploy instead of being
overwritten by an environment default on every boot.

---

## Step 3 — startup

```bash
railway logs --service <whale-bot-service> | tail -60
```

The block to look for, printed by `app/main.py:198-207`:

```
Database ......... CONNECTED (postgresql, durable)
Configuration .... LOADED from database
Enabled coins .... BTC, ETH, SOL
Threshold ........ $2,000,000
Mode ............. PRIVATE
Admins ........... 1 (0 co-admin)
Watched wallets .. 0
```

Read it carefully — the exact words carry the finding:

- `(postgresql, durable)` is the goal. `(sqlite, ephemeral)` means
  `DATABASE_URL` never reached the process, and every setting will be lost on the
  next deploy. A `SQLite is in the container filesystem` warning follows.
- `LOADED from database` means settings came back from storage. `SEEDED — first
  boot, defaults seeded` is correct **only** on the very first deploy against an
  empty database; seeing it on a later boot is the redeploy-reset symptom and the
  thing to report.

Also confirm, in the same log:

| Expected | Meaning |
|---|---|
| `Telegram polling started` | Telegram is up |
| `Startup complete` | the whole sequence finished |
| **absence of** `Alert dropped: Telegram bot not attached yet` | the alert service got its bot |
| **absence of** a repeating reconnect/backoff ladder | the websocket is stable, not looping |
| **absence of** repeated tracebacks | no exception loop |
| `curl -s https://<domain>/health` → `200` | healthcheck honest |

---

## Step 4 — persistence (Telegram, as main admin)

```
/addcoin HYPE
/setthreshold 5000000
/config
```

Copy the `/config` output verbatim into the evidence table. Then redeploy the
service and send `/config` again.

The second `/config` **must** show `BTC ETH HYPE SOL` — all four — and
`$5,000,000`. Two distinct failures to keep apart:

- Only `HYPE` after `/addcoin HYPE` → an addition became a replacement. That is
  the bug fixed in `handlers/prompts.py`; a regression there is a real defect.
- All four present before the redeploy, fewer after → the storage is not durable.
  Look at the Step 3 line first: `ephemeral` explains it without any code being
  at fault.

`/config` reads the `settings`, `tracked_coins`, `admins` and `tracked_wallets`
tables directly and compares them against the running cache, so it reports drift
rather than confirming itself. It prints the storage *kind* only — if you ever see
a connection string there, stop and report it.

---

## Step 5 — execution detection (needs real fills)

Watch the alerts as they arrive. What must hold:

| Live event | Expected |
|---|---|
| a large order rests in the book | **no** `🐋 WHALE TRADE` alert (with order alerts off, no alert at all) |
| that order is cancelled | **no** `🐋 WHALE TRADE` alert |
| an order fills | `🐋 WHALE TRADE`, `🔎 VERIFIED EXECUTION` |

For a fill, `💰 Executed:` must equal **execution price × executed quantity**. The
partial-fill case is the one that catches a regression: a 40-of-130 fill at
$95,000 is `$3.80M`, not the order's `$12.35M`. If order alerts are on, an order
event's value is labelled **intended** and must never be added to trade notional —
cross-check with `/stats`, where `Executed Trades`, `Order Events` and
`Position Events` are three separate lines.

---

## Step 6 — position lifecycle (needs real position changes)

Expect `POSITION OPENED`, `POSITION INCREASED`, `POSITION REDUCED`,
`POSITION CLOSED`.

The assertion that matters: **the side comes from the position, not the fill.** A
`SELL` that trims a long must render `📈 LONG` (reduced), not `📉 SHORT`. A `BUY`
that trims a short must render `📉 SHORT`. When size reaches zero the alert must
be `POSITION CLOSED`, valued from the last non-zero snapshot — not `$0`, and not
`SHORT` because the closing leg was a sell.

If a position's figures show `Not publicly detectable` / `N/A`, that is correct
behaviour for a wallet whose snapshot was not available, not a defect.

---

## Step 7 — wallet display

In every alert and in `/whales`, `/wallets`, `/recent`, the address must appear in
full and in monospace, e.g.

`0x31dea2516beee92135b96f464eeec3cf292a13f2`

Never `0x31de...13f2`. Shortening is allowed **only** on a button label. Tap the
buttons on a wallet-bearing view: nothing should break, because no
`callback_data` carries an address.

---

## Step 8 — order alert toggle

Defaults on a fresh install: **trade alerts ON, position alerts ON, order alerts
OFF**.

1. As main admin: `/settings` → alert settings → confirm the order-alert switch
   reads off, and that resting/cancelled order events are **not** arriving.
2. Turn it on. Order events may now appear, labelled *intended*.
3. Turn it off again and confirm `/orders` still lists live resting orders — the
   detector keeps tracking regardless of whether humans are told.
4. Repeat step 1–2 as a **co-admin**: must be allowed.
5. Repeat as a **normal user**: must be refused, and the refusal must come from
   the server, not from a hidden button.

---

## Step 9 — normal-user restrictions

From a third account that is neither admin nor co-admin, **type the commands by
hand** — do not rely on what the menu offers:

```
/panel  /settings  /config  /admins  /addadmin 123  /removeadmin 123
/setthreshold 1  /addcoin DOGE  /resetsettings  /audit  /pause  /go
```

Every one must be refused. Then check the command menu itself: a normal user's
suggestion list must contain only the public commands. Both layers matter —
hiding a button is not authorization.

## Step 10 — co-admin lifecycle

As main admin: `/addadmin <telegram_id>` → the new co-admin can change settings
and see `/admins`, but must still be refused `/resetsettings`, `/addadmin` and
`/removeadmin`. Then `/removeadmin <telegram_id>` → the demoted account loses
access **immediately**, on its very next command, and its command menu shrinks
back to the public set. A normal user must not be able to promote themselves by
crafting a callback.

## Steps 11–12 — `/recent` and `/whales`

`/recent` must list verified executions and position changes only —
`ORDER_PLACED`, `ORDER_CANCELLED` and `ORDER_MODIFIED` must not appear as trades
(with order alerts on they may appear, labelled as order events). `/whales` must
count actual executions; a wallet that placed and cancelled an order has made
**zero** trades. Cross-check against `/stats`.

---

## Evidence template

Fill this in and hand it back. One row per step; `NOT VERIFIED` is a legitimate
answer and far better than an optimistic `PASS`.

| Step | What was done | Telegram output (verbatim) | Railway log (verbatim) | Event type | Coin | Wallet | Timestamp (UTC) | Expected | Actual | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| 3 startup | | | | | | | | | | |
| 4 persistence | | | | | | | | | | |
| 5 execution | | | | | | | | | | |
| 6 lifecycle | | | | | | | | | | |
| 7 wallet | | | | | | | | | | |
| 8 toggle | | | | | | | | | | |
| 9 user | | | | | | | | | | |
| 10 co-admin | | | | | | | | | | |
| 11 /recent | | | | | | | | | | |
| 12 /whales | | | | | | | | | | |

## If a live failure occurs

Record all eight facts first (Telegram output, Railway log, event type, coin,
wallet, timestamp, expected, actual). Then the smallest responsible function is
found, **only that** is changed, a regression test is added that fails against
the current code, focused tests run, then the full suite. No subsystem rewrite,
no new migration unless a real failure proves one is needed, and no UNIQUE
constraint on `whale_events.dedup_key`.

**Never paste the bot token or a connection string into a chat, a commit message,
or this file.** If a value must be changed, change it in the Railway dashboard.
