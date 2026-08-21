# PROJECT STATE

Last updated: 2026-08-21

Read this file first. The repository is the source of truth; this file records
*why* the code looks the way it does and *what happens next*.

---

## CURRENT PHASE

**PHASE 15 — PRE-DEPLOYMENT VERIFICATION** (spec §54)

Phases 1–14 are implemented and verified, including the full §36 test suite
(**379 passed, 0 failed, 0 skipped**) and all documentation. What remains is the
final commit sequence and a deployment-readiness report. **Nothing is pushed and
nothing is deployed**; no Git remote exists and none may be invented.

---

## COMPLETED

- Environment-variable configuration with fatal-at-startup validation (`app/config.py`)
- Structured logging with secret redaction (`app/utils/logging.py`)
- Weighted REST rate limiter + token bucket (`app/utils/ratelimit.py`)
- Exponential backoff with jitter (`app/utils/backoff.py`), TTL cache (`app/utils/cache.py`)
- Confidence-labelled formatting primitives (`app/utils/formatting.py`)
- PostgreSQL schema: 12 tables, 11 repositories, hand-written Alembic migration
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
- **Test suite (spec §36): 11 modules, 379 tests, all passing offline** — no
  Hyperliquid connection, no Telegram API, no PostgreSQL required
- **`WhaleEngine._write_lock`** — fix for a concurrent-write race found by the
  end-to-end pipeline test, which silently dropped alerts (see `DECISIONS.md`)
- **Documentation**: `README.md`, `.agent/API_NOTES.md`, `.agent/TEST_STATUS.md`

## CURRENTLY WORKING ON

- Final commit sequence (three `test:` commits, two `docs:` commits) and the
  deployment-readiness report

## NEXT TASK

1. Commit in logical order; do not squash unrelated changes
2. Produce the deployment-readiness report and **stop**
3. Deployment itself is blocked until the user supplies their own GitHub remote

---

## FILES MODIFIED (this phase)

- `app/whale/engine.py` — added `_write_lock` around the persistence write
  (the only `app/` change since Phase 13)
- `tests/*` (new — complete, 379 tests)
- `README.md` (new)
- `.agent/API_NOTES.md`, `.agent/TEST_STATUS.md`, `.agent/NOW.md`,
  `.agent/HANDOFF.md`, `.agent/FILE_INDEX.md` (new)
- `.agent/PROJECT_STATE.md`, `.agent/TASK_QUEUE.md`, `.agent/DECISIONS.md`,
  `.agent/CHANGELOG.md`, `.agent/ARCHITECTURE.md`, `.agent/DATA_MODEL.md` (updated)

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

Authoritative detail in `.agent/TEST_STATUS.md`. Summary:

- **TOTAL 379 · PASSED 379 · FAILED 0 · SKIPPED 0** (43.00 s)
- Run with `./.venv/Scripts/python.exe -m pytest -q`. The bare `python` on PATH
  is not the project interpreter and lacks `pytest_asyncio`.
- Unit tests: all 16 items of the spec §36 list are covered; the mapping from
  requirement to test name is in `TEST_STATUS.md`.
- Integration tests: `test_engine_pipeline.py` drives a raw `trades` frame all
  the way to the final Telegram message text; also `test_database.py`,
  `test_resilience.py`, `test_telegram_handlers.py`.
- Alembic migration: verified manually (`upgrade head` → `downgrade base` →
  `upgrade head`). The suite runs on SQLite and does not execute the migration
  scripts.
- Railway deployment: not yet performed — pending the user's own GitHub remote.
