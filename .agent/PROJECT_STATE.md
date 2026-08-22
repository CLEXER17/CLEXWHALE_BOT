# PROJECT STATE

Last updated: 2026-08-22

Read this file first. The repository is the source of truth; this file records
*why* the code looks the way it does and *what happens next*.

---

## CURRENT PHASE

**POST-DEPLOYMENT HARDENING**

Phases 1–15 are implemented and verified. The repository is published at
**<https://github.com/CLEXER17/CLEXWHALE_BOT>** and `main` tracks `origin/main`.
**The bot has been deployed on Railway and has run**: PostgreSQL migrations
applied, `/health` up, Hyperliquid websocket connected, whale events detected.

Work since then has been driven by defects reported from the live deployment
rather than by the original spec phases. Latest completed: the **verified
execution + position lifecycle** work (39-section spec), which stopped the bot
announcing a resting order as a whale trade and inferring a position side from a
fill side. Before it, the **persistence + settings-safety fix** (34-section spec)
addressed two reported production symptoms — a settings change replacing unrelated
settings, and configuration disappearing after a redeploy. Suite: **587 passed, 0
failed**.

No task is paused mid-flight. What remains needs the deployed bot: the §33 live
six-step scenario, and token rotation. See `NOW.md` and `HANDOFF.md`.

**Alert delivery has still not been confirmed live**; that needs a redeploy plus a
whale above the threshold, and the token rotation below.

No credential exists in this repository and none may be added to it.

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
- **Test suite (spec §36): 12 modules, 392 tests, all passing offline** — no
  Hyperliquid connection, no Telegram API, no PostgreSQL required
- **`WhaleEngine._write_lock`** — fix for a concurrent-write race found by the
  end-to-end pipeline test, which silently dropped alerts (see `DECISIONS.md`)
- **Documentation**: `README.md`, `.agent/API_NOTES.md`, `.agent/TEST_STATUS.md`
- **First live deploy on Railway**, and the two defects it exposed: the alert
  service was never given a bot (PTB does not run `post_init` under a manual
  lifecycle), and secret redaction broke `%d` log placeholders. Both fixed with
  regression tests proven to fail against the shipped code
- **Persistence + settings safety (34-section spec).** Every command is a patch;
  `bootstrapped_at` makes "should defaults apply?" a stored fact; diff-based
  `CoinRepository.replace`; `/config` reads the tables directly and flags drift;
  two-step 🧹 Clear and main-admin-only `/resetsettings`; `startup_summary()` plus
  the plain-text startup block. 29 new tests in `tests/test_persistence.py`
- **Verified execution + position lifecycle (39-section spec).** A resting order
  is never called a trade: executions (fills) are separated from order intentions
  throughout alerts, `/recent`, `/whales`, the wallet leaderboard and the
  statistics panel; `enable_order_detector` (tracking, on) split from
  `enable_order_alerts` (publication, off); a fill's threshold measured against
  the executed value; a position side read only from a `clearinghouseState`
  snapshot, with a flat snapshot treated as no position and a close valued from
  the last non-zero one. 28 new tests in `tests/test_verified_execution.py`

## CURRENTLY WORKING ON

- Nothing in progress. Full suite last measured **587 passed / 0 failed**
  (2026-08-22). The next actions are live ones, listed below, and belong to the
  user.

## NEXT TASK

1. **Revoke the bot token.** It was pasted into a chat transcript, so it must be
   treated as compromised: @BotFather → `/revoke` → put the new token in the
   Railway variable.
2. Confirm the redeploy: the log must contain `Telegram connected` and must not
   contain `Alert dropped: Telegram bot not attached yet`. The new startup block
   must read `CONNECTED (postgresql, durable)` and `Configuration .... LOADED`.
3. Confirm persistence live: `/addcoin HYPE`, `/setthreshold 5000000`, redeploy,
   then `/config` — HYPE still present, threshold still 5,000,000.
4. If alerts still do not arrive, look at recipients rather than wiring —
   `AlertService._resolve_recipients` sends to `admins.admin_ids`, so
   `MAIN_ADMIN_ID` must match the watching account.
5. After a clean live run, record any payload shape that differs from
   `API_NOTES.md` — and correct the file rather than the observation.

---

## FILES MODIFIED (recent work)

Persistence + settings safety:

- `app/services/settings_service.py` — `KEY_BOOTSTRAPPED`, `first_boot`,
  `last_coin_diff`, `add_coins()`, `reset_to_defaults()`, `decode_stored()`;
  `load()` seeds only absent keys, coins only while unbootstrapped
- `app/database/repository.py` — `CoinRepository.replace` is a diff returning
  `(added, removed)`
- `app/bot/handlers/prompts.py` — the coins prompt **adds** instead of replacing
  (the reported bug)
- `app/bot/handlers/admin.py` — `cmd_config`, `cmd_resetsettings`; addcoin/setcoins
  report added vs removed
- `app/bot/handlers/callbacks.py` — `CB_RESET` area, two-step coin clear,
  `set:config`
- `app/bot/keyboards/inline.py` — `➕ Add coins`, `confirm_clear_coins`,
  `confirm_reset_settings`, `🗄 Stored Configuration`
- `app/bot/views.py` — `config_view()` reads the tables directly
- `app/bot/messages/texts.py` — `coins_added`/`coins_replaced`/`coins_cleared`,
  the two confirmations, `settings_reset`, `config_snapshot`
- `app/services/admin_service.py` — `Capability.RESET_SETTINGS` (main admin only)
- `app/container.py` — `startup_summary()`
- `app/main.py` — plain-text startup block + SQLite-is-ephemeral warning
- `app/bot/handlers/__init__.py`, `app/bot/commands.py` — `/config`,
  `/resetsettings`
- `tests/test_persistence.py` (new, 29 tests)
- `README.md` — "Persistence and settings safety"

Earlier this phase:

- `app/whale/engine.py` — added `_write_lock` around the persistence write
- `app/bot/application.py` — attach the bot at build time; `post_init` made
  public, guarded and explicitly invoked
- `app/main.py` — `Runtime.start()` calls `post_init` after `initialize()`
- `app/utils/logging.py` — type-preserving `_scrub_arg`, `_safe_message`
- `README.md`, `.agent/API_NOTES.md`, `.agent/TEST_STATUS.md`, `.agent/NOW.md`,
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
  monitoring switch, global pause, thresholds, coins, cooldown, tracked wallets,
  alert toggles, users, events, orders, positions, alert history.
- `AppContainer.restore()` reloads all of that after a redeploy, and
  `startup_summary()` prints whether the configuration was `loaded` or `seeded` so
  an unexpected reset is visible rather than silent.
- Settings changes are **patches**: one row written, one field refreshed. Nothing
  reconstructs the configuration from defaults except `reset_to_defaults()`, which
  is reachable only from a confirmed `/resetsettings`.

## TEST STATUS

Authoritative detail in `.agent/TEST_STATUS.md`. Summary:

- **TOTAL 587 · PASSED 587 · FAILED 0 · SKIPPED 0** (49.82 s)
- Run with `./.venv/Scripts/python.exe -m pytest -q`. The bare `python` on PATH
  is not the project interpreter and lacks `pytest_asyncio`.
- Unit tests: all 16 items of the spec §36 list are covered; the mapping from
  requirement to test name is in `TEST_STATUS.md`.
- Integration tests: `test_engine_pipeline.py` drives a raw `trades` frame all
  the way to the final Telegram message text; also `test_database.py`,
  `test_resilience.py`, `test_telegram_handlers.py`, `test_persistence.py`.
- Persistence: `test_persistence.py` simulates a restart
  (`SettingsService(...).load()`) and a redeploy (`AppContainer(...).restore()`)
  over the same database, and mutation-checks the reported bug — reverting the
  prompt to `set_coins()` makes it fail with `('HYPE',)`.
- Alembic migration: verified manually (`upgrade head` → `downgrade base` →
  `upgrade head`). The suite runs on SQLite and does not execute the migration
  scripts. No new migration was needed for the persistence work: no new table or
  column was introduced.
- Railway deployment: **performed, and the process ran.** Migrations applied to
  PostgreSQL, `/health` served, Hyperliquid connected, whales detected. Alert
  *delivery* is still unconfirmed pending a redeploy.
