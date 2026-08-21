# FILE INDEX

Map of the repository. Search here before creating any file — one clear
implementation per responsibility. Update this file whenever a file is added,
renamed, or changes responsibility; record deletions in `CHANGELOG.md`.

Line counts are approximate and only meant to signal "big file / small file".

---

## Root

| Path | Purpose |
| --- | --- |
| `README.md` | Public documentation: features, commands, permission matrix, local install, env table, BotFather, GitHub, Railway, Hyperliquid limitations |
| `pyproject.toml` | Packaging, pytest config (`asyncio_mode=auto`), ruff config |
| `requirements.txt` / `requirements-dev.txt` | Pinned runtime / dev dependencies |
| `alembic.ini` | Alembic config; the URL is injected at runtime from `DATABASE_URL` |
| `Dockerfile` | Production image; execs `python -m app.main` as PID 1 |
| `.dockerignore` | Keeps `.env`, caches and the venv out of the build context |
| `railway.toml` | One Railway service, `/health` check, `numReplicas = 1` |
| `start.sh` | Optional shell entry point (`exec python -m app.main`) |
| `.env.example` | Every variable documented; placeholder values only |
| `.gitignore` | Excludes `.env*`, keys, caches, venvs, local DBs |
| `.gitattributes` | Forces LF on `*.sh` / `Dockerfile` |

## Core

| Path | Purpose | Key symbols |
| --- | --- | --- |
| `app/main.py` | Entry point: validation → DB → migrations → restore → Telegram → ingest → `/health` | `main`, `run`, `create_http_app` |
| `app/config.py` | All configuration from environment variables | `Settings`, `ConfigError`, `validate_runtime`, `get_settings`, `reload_settings` |
| `app/container.py` | Owns every long-lived service and start/stop order | `AppContainer` (`.env .db .settings .admins .alerts .rest .engine`), `restore`, `start_ingest`, `health` |

## Hyperliquid (`app/hyperliquid/`)

| Path | Purpose | Key symbols |
| --- | --- | --- |
| `constants.py` | Endpoint weights, WS limits, statuses, trigger classification, windows | `info_weight`, `side_label`, `is_cancel_status`, `is_reject_status`, `classify_trigger`, `TriggerKind`, `MAX_WS_UNIQUE_USERS`, `window_seconds` |
| `models.py` | Typed domain objects | `Trade`, `BookLevel`, `L2Book`, `Position`, `AccountState`, `OpenOrder`, `OrderUpdate`, `Fill`, `AssetMeta`, `AssetContext` |
| `parser.py` | Raw JSON → models, defensively | `parse_trades`, `parse_l2_book`, `parse_all_mids`, `parse_clearinghouse_state`, `parse_open_orders`, `parse_order_updates`, `parse_fills` |
| `rest.py` | `POST /info` client, weight-budgeted, never raises | `HyperliquidREST`, `post_info`, `clearinghouse_state`, `frontend_open_orders`, `order_status`, `l2_book`, `stats` |
| `websocket.py` | Reconnecting WS client with resubscribe + ping | `HyperliquidWebSocket`, `subscribe`, `replace_subscriptions`, `_run`, `_dispatch`, `stats` |

## Whale engine (`app/whale/`)

| Path | Purpose | Key symbols |
| --- | --- | --- |
| `events.py` | Event vocabulary and the event object | `EventType` (13), `ValueKind` (6), `THRESHOLD_CLASS`, `WhaleEvent`, `db_fields` |
| `detector.py` | Pure detection — no I/O, no database | `WhaleDetector.from_trade / from_position_change / from_order_update / from_open_order / from_order_disappearance / from_book`, `extract_tpsl`, `PositionContext`, `OrderState` |
| `filters.py` | The only place that decides "is this a whale" | `WhaleFilter.evaluate`, `FilterResult`, `DETECTOR_OF_EVENT`, `REASON_*` |
| `dedup.py` | Identity + cooldown duplicate suppression | `Deduplicator.check/forget/warm`, `identity_key`, `cooldown_key`, `magnitude_bucket` |
| `tracker.py` | Wallet scoring, poll scheduling, focus slate | `WalletTracker`, `TrackedWallet`, `order_state_from_open_order` |
| `engine.py` | Three-tier ingest orchestration | `WhaleEngine`, `_on_trade`, `_enrich`, `_ingest_account_state`, `_ingest_open_orders`, `_emit`, `stats` |

## Telegram (`app/bot/`)

| Path | Purpose | Key symbols |
| --- | --- | --- |
| `application.py` | Builds the PTB app and registers every handler | `build_application`, `register_handlers` |
| `views.py` | Renders panels and read-only screens | `panel_view`, `status_view`, `settings_view`, `stats_view`, `coins_view`, `admins_view` |
| `middleware/permissions.py` | The single authorization gate | `requires`, `get_container`, `respond`, `notify`, `refuse`, `throttle`, `actor_of`, `reset_state` |
| `handlers/common.py` | `/start`, `/help`, `/about`, `/panel`, error handler | `cmd_start`, `cmd_help`, `cmd_about`, `cmd_panel`, `cmd_unknown`, `on_error` |
| `handlers/admin.py` | All mutating commands + argument parsers | `parse_usd`, `parse_coins`, `parse_on_off`, `parse_user_id`, `cmd_setthreshold`, `cmd_setcoins`, `cmd_addadmin`, `cmd_removeadmin`, `cmd_public`, `apply_add_admin` |
| `handlers/data.py` | Read-only data commands (`/whales`, `/orders`, …) | `cmd_whales`, `cmd_recent`, `cmd_orders`, `cmd_positions`, `cmd_wallets` |
| `handlers/callbacks.py` | Inline-button router; re-authorises every press | `on_callback`, `_AREA_CAPABILITY`, `_ACTION_CAPABILITY`, `_parse` |
| `handlers/prompts.py` | Multi-step text answers | `on_text`, `_REQUIRED` |
| `keyboards/inline.py` | Keyboards + callback-data namespaces | `CB_PANEL`, `CB_MON`, `CB_THRESH`, `CB_COIN`, `CB_ADMIN`, `CB_PUBLIC`, `CB_SET`, `CB_STATS`, `CB_DATA` |
| `messages/texts.py` | Verbatim spec message templates | `PRIVATE_NOTICE`, `HELP_*`, `rate_limited`, panel/threshold/admin texts |

## Services (`app/services/`)

| Path | Purpose | Key symbols |
| --- | --- | --- |
| `settings_service.py` | Env-seeded, DB-authoritative runtime config | `RuntimeConfig`, `SettingsService.load/set_threshold/set_coins/set_monitoring/set_public_mode/toggle`, `KEY_*` |
| `admin_service.py` | Roles and the §29 capability matrix | `Capability`, `Actor`, `AdminService.can/require/add_co_admin/remove_co_admin`, `AdminError`, `CO_ADMIN_CAPABILITIES`, `MAIN_ONLY_CAPABILITIES` |
| `alert_service.py` | Renders and delivers alerts | `AlertService.enqueue/render/start/stop`, `HEADERS`, `SIDE_BADGES`, `FOOTER` |

## Database (`app/database/`)

| Path | Purpose | Key symbols |
| --- | --- | --- |
| `base.py` | Engine/session management, `create_all` for tests | `Database`, `Base`, `set_database`, `get_database` |
| `models.py` | 12 SQLAlchemy tables | `Admin`, `User`, `Setting`, `TrackedCoin`, `TrackedWallet`, `Wallet`, `WhaleEvent`, `OrderRecord`, `PositionRecord`, `AlertHistory`, `AdminAudit`, `BotLog` |
| `repository.py` | All SQL lives here | `SettingsRepository`, `AdminRepository`, `UserRepository`, `CoinRepository`, `WalletRepository`, `EventRepository`, `OrderRepository`, `PositionRepository`, `AlertRepository`, `AuditRepository`, `LogRepository` |
| `migrations/env.py` | Alembic runtime; honours `configure_logging` | — |
| `migrations/versions/0001_initial.py` | Hand-written initial schema | `upgrade`, `downgrade` |

## Utilities (`app/utils/`)

| Path | Purpose | Key symbols |
| --- | --- | --- |
| `formatting.py` | Confidence wrapper + every display formatter | `DataPoint`, `Confidence`, `fmt_usd`, `fmt_price`, `fmt_pct`, `pct_distance`, `short_wallet`, `utc_now`, `from_ms`, `fmt_ago`, `escape_html`, `DIVIDER` |
| `logging.py` | Structured logging, secret redaction | `setup_logging`, `register_secrets`, `REDACTOR`, `SecretRedactor`, `get_logger` |
| `ratelimit.py` | REST weight budget + per-user throttle | `WeightedRateLimiter`, `TokenBucket` |
| `backoff.py` | Exponential backoff with jitter | `ExponentialBackoff` |
| `cache.py` | TTL cache used by dedup and recipient caching | `TTLCache` |

## Tests (`tests/`)

| Path | Purpose |
| --- | --- |
| `conftest.py` | SQLite database fixture, `Settings(app_env="test")`, container, fake Telegram `Update`/`Context`, per-test state reset |
| `factories.py` | Builders for `Trade`, `Position`, `OpenOrder`, `OrderUpdate`, `L2Book`, `OrderState` |
| `test_config.py` | Missing/invalid env vars are fatal; SQLite refused in production; secret redaction |
| `test_detector_trades.py` | Threshold, LONG, SHORT, taker/maker attribution |
| `test_detector_positions.py` | Open / increase / decrease / close / flip, TP/SL extraction, no-fabrication rules |
| `test_detector_orders.py` | Limit order placed / modified / partially filled / filled / cancelled, unresolved outcome |
| `test_filters.py` | Per-class thresholds, coin filter, detector toggles, monitoring off |
| `test_dedup.py` | Identity gate, cooldown gate, magnitude escalation, forget/warm |
| `test_permissions.py` | Main admin, co-admin limits, users, public/private mode, audit |
| `test_telegram_handlers.py` | Commands, crafted callback data refusal, invalid input, unknown commands |
| `test_database.py` | Repository round-trips, main-admin protection, settings persistence |
| `test_resilience.py` | WS reconnect/backoff, REST failure paths, rate limiting, bad frames |
| `test_engine_pipeline.py` | Integration: trade frame → detect → filter → dedup → persist → alert |
| `test_bot_application.py` | The real PTB `Application`: bot attached to the alert service, container published on `bot_data`, every command registered, `post_init` idempotent |

## Scripts (`scripts/`)

| Path | Purpose |
| --- | --- |
| `live_probe.py` | Manual read-only probe of the real Hyperliquid API (never used in production) |
| `render_preview.py` | Renders every alert template locally to eyeball the formats |

## Project memory (`.agent/`)

All eleven files exist. Read `NOW.md` first — it is the smallest recovery file
and names the current file, function and next action.

| Path | Purpose |
| --- | --- |
| `NOW.md` | Smallest recovery file: phase, task, current file, last action, next action |
| `PROJECT_STATE.md` | What is built, what is not, phase-by-phase |
| `HANDOFF.md` | Compaction handoff: completed / remaining / do-not-redo |
| `TASK_QUEUE.md` | Numbered tasks; ✓ only when implemented **and** exercised |
| `FILE_INDEX.md` | This file — map of the repository, search here before creating anything |
| `ARCHITECTURE.md` | Module boundaries and data flow |
| `DECISIONS.md` | Append-only log of decisions expensive to rediscover |
| `DATA_MODEL.md` | Tables, columns, relationships, event vocabulary |
| `API_NOTES.md` | Per-source Hyperliquid fields available / `NOT AVAILABLE FROM THIS DATA SOURCE` |
| `TEST_STATUS.md` | Actual test results, §36 coverage map, known limitations |
| `CHANGELOG.md` | What changed, in order |

None of these files contains a secret, token, key or credential, and none may.
