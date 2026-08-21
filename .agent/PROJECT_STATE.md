# PROJECT STATE

Last updated: 2026-08-21

Read this file first. The repository is the source of truth; this file records
*why* the code looks the way it does and *what happens next*.

---

## CURRENT PHASE

**PHASE 13 — TESTING** (unit + integration suite, spec §36)

Phases 1–12 are implemented and verified. Phase 14 (Railway deployment docs)
is the last remaining item after tests.

---

## COMPLETED

- Environment-variable configuration with fatal-at-startup validation (`app/config.py`)
- Structured logging with secret redaction (`app/utils/logging.py`)
- Weighted REST rate limiter + token bucket (`app/utils/ratelimit.py`)
- Exponential backoff with jitter (`app/utils/backoff.py`), TTL cache (`app/utils/cache.py`)
- Confidence-labelled formatting primitives (`app/utils/formatting.py`)
- PostgreSQL schema: 11 tables, 12 repositories, hand-written Alembic migration
  `0001_initial.py` (verified `upgrade head` → `downgrade base` → `upgrade head`)
- Hyperliquid REST client with per-endpoint weights and a 1200/min IP budget
- Hyperliquid WebSocket client with reconnect, resubscribe and heartbeat
- Response parsers → typed models (`app/hyperliquid/parser.py`, `models.py`)
- Whale detection: trades, position changes, order lifecycle, book walls
- Two-gate deduplication (identity key + cooldown key with magnitude bucket)
- Wallet tracker with decayed scoring and a focus-wallet slate
- Three-tier ingest engine (`app/whale/engine.py`)
- Settings service (DB-authoritative, env-seeded) with audit + change listeners
- Admin service implementing the §29 permission matrix
- Alert service rendering the verbatim §16/§17/§18 message formats
- Telegram bot: 29 commands, inline panel, callback router, prompt flow
- Single authorization gate (`app/bot/middleware/permissions.py`)
- Entry point with migrations-on-boot, `/health`, graceful SIGTERM shutdown
- Dockerfile, `railway.toml` (one service), `start.sh`, `.env.example`
- Git repository initialised with logical commits

## CURRENTLY WORKING ON

- `tests/` package: conftest fixtures, factories, unit tests, integration tests

## NEXT TASK

1. Finish `tests/` and run green: `./.venv/Scripts/python.exe -m pytest`
2. Write `README.md` (BotFather → git → 10-step Railway flow → env vars → updates)
3. Final production checklist pass (spec §54)

---

## FILES MODIFIED (this phase)

- `.gitignore`, `.gitattributes` (new)
- `.agent/*` (new)
- `tests/*` (new — in progress)

## FILES VERIFIED

- `app/config.py` — validation raises one aggregated `ConfigError`
- `app/whale/detector.py` — pure, no I/O, directly unit-testable
- `app/whale/engine.py` — `_on_trade` → queue → `_emit` → `alert_callback`
- `app/services/admin_service.py` — co-admins cannot manage admins
- `app/bot/handlers/callbacks.py` — every callback re-authorised by user id
- `app/database/migrations/versions/0001_initial.py` — round-trip clean
- `.gitignore` — excludes `.env`, `.env.*`, `*.key`, `*.pem`, `secrets/`, `credentials/`, caches, venvs

---

## KNOWN ISSUES / LIMITATIONS

These are Hyperliquid API limits, not bugs. They are documented for the user
rather than worked around with invented data.

1. **10 unique addresses per IP** across all user-specific WS subscriptions.
   `ws_focus_wallets` defaults to 8 and is clamped by the validator.
2. **`orderUpdates` frames do not name their user** → one WS connection per
   focus wallet, which is why the cap above is binding.
3. **No global liquidation feed.** Liquidation *price* comes from
   `clearinghouseState`; liquidation *events* are not published globally.
4. **TP/SL only exist for enriched wallets.** `frontendOpenOrders` is the only
   source; for everyone else TP/SL render as `Not publicly detectable` / `N/A`.
5. **`l2Book` has no wallet attribution** → book-wall events carry
   `wallet = None` and an explicit `wallet_attribution: UNAVAILABLE` point.
6. **`trades` is the only global feed with wallets** (`users: [buyer, seller]`),
   so it is the discovery backbone; positions are enrichment.

## IMPORTANT IMPLEMENTATION DETAILS

- Every numeric shown to a user is a `DataPoint` carrying
  `CONFIRMED | ESTIMATED | UNAVAILABLE`. Estimated values render with `(est.)`.
- `ValueKind` separates trade notional, order notional, position notional,
  position delta, margin and book depth. Thresholds are per class
  (`THRESHOLD_CLASS`), so "cash flow" is never conflated with position size.
- Monitoring windows (2M…4H) are **observation windows over events**, never
  presented as candles.
- Critical state lives in PostgreSQL, never only in RAM: admins, public mode,
  monitoring switch, threshold, coins, cooldown, tracked wallets, history.
- `AppContainer.restore()` reloads all of that after a redeploy.

## TEST STATUS

- Unit tests: in progress (target: spec §36 list, all 16 items)
- Integration tests: in progress (trade → detect → filter → dedup → persist → alert)
- Alembic migration: verified manually (round-trip)
- Railway deployment: pending (docs + first push)
