# CHANGELOG

Newest first. One entry per logical milestone; mirrors the git history.

---

## 2026-08-21 — First Railway deploy, and the two defects it exposed

The container started, migrations ran against PostgreSQL, `/health` came up and
the whale engine connected to Hyperliquid — but two defects only a live process
could show up.

- **Every alert was dropped.** The log repeated `Alert dropped: Telegram bot not
  attached yet`, and the command menu never appeared in Telegram.
  `AlertService.attach_bot()` was only reachable from the application's
  `post_init` hook, and python-telegram-bot calls `post_init` **only** from
  `run_polling()` / `run_webhook()`; `app.main.Runtime` drives the lifecycle step
  by step, so the hook never ran. `build_application()` now attaches the bot
  directly, and `post_init` is public, called explicitly by `Runtime.start()`
  after `initialize()`, and guarded so it cannot publish the menu twice.
- **Secret redaction corrupted `%`-style log arguments.** `SecretRedactor`
  coerced every `record.args` entry to `str`, so uvicorn's
  `"Started server process [%d]"` raised `TypeError: %d format: a real number is
  required, not str` inside the formatter — and Python answers a formatter
  exception with a full traceback, so two startup lines became screens of noise.
  `_scrub_arg()` now preserves argument types and only substitutes a non-string
  when it actually carries a secret; both formatters resolve the message through
  `_safe_message()`. Redaction coverage is unchanged and now tested with args.
- **New module `tests/test_bot_application.py`** (6 tests): nothing had ever built
  the real `Application`, which is exactly why defect 1 reached production. Suite
  is now **392 passed, 0 failed, 0 skipped**. Both fixes were verified to be
  necessary by stashing them and watching the new tests fail with the production
  symptoms.

## 2026-08-21 — Published to GitHub

- Remote connected and `main` pushed:
  <https://github.com/CLEXER17/CLEXWHALE_BOT>. The remote was verified empty
  (`git ls-remote` returned no refs) before the first push, so nothing was
  overwritten, and a pre-push check confirmed no `.env` and no key material is
  tracked — only `.env.example`, with every value blank.
- `README.md` now names the real clone URL and the real Railway source repo
  instead of a placeholder.
- Railway deployment is **not** done: it needs the user's account, a PostgreSQL
  add-on, and `BOT_TOKEN` / `MAIN_ADMIN_ID` as Railway variables. No credential
  is in the repository and none may be added.

## 2026-08-21 — Documentation for release

- `README.md`: features, command table, permission matrix, local installation,
  the full environment-variable table (values taken from `.env.example`, never
  real credentials), BotFather setup, the GitHub flow with **no invented remote
  URL**, the Railway flow (connect repo → variables → provision PostgreSQL →
  deploy → automatic migration on boot → `/health` → verify in Telegram), and an
  explicit "what Hyperliquid does and does not provide" section.
- `.agent/API_NOTES.md`: per data source — subscription/endpoint, purpose, fields
  available, fields written as `NOT AVAILABLE FROM THIS DATA SOURCE`,
  authentication (none anywhere), rate limits, implementation location and
  verification date.
- `.agent/TEST_STATUS.md`: actual results, per-module timings, a §36
  requirement → test-name map, the defect the suite found, and five honest
  limitations of the suite.
- `.agent/NOW.md`, `.agent/HANDOFF.md`, `.agent/FILE_INDEX.md` added; the
  project-memory set is now all eleven files.

## 2026-08-21 — Test suite (spec §36)

- 11 test modules, **379 tests, 0 failures, 0 skips**, entirely offline: no
  Hyperliquid connection, no Telegram API, no PostgreSQL. Only two seams are
  substituted (`HyperliquidREST._client`, `websocket.ws_connect`) and the bot is
  a fake.
- `tests/test_engine_pipeline.py` (22 tests) pushes a raw `trades` websocket
  frame through the real pipeline and asserts the final Telegram message text
  plus the persisted rows — including the three distinct TP/SL states, so an
  unavailable value can never be printed as a real one. Timing is asserted with
  queue joins, never `asyncio.sleep`.
- **Fixed a real alert-losing defect** the suite exposed: `WhaleEngine._persist`
  ran in three concurrent workers, and two events for the same wallet raced on
  the read-then-write of `wallets` / `positions`. The losing transaction failed,
  the event was never persisted, and **its alert was never sent**. Now
  serialised by `WhaleEngine._write_lock`, held only around the database write.

## 2026-08-21 — Project memory + git history

- Initialised the git repository and split the existing tree into eight logical
  commits (scaffolding → database → hyperliquid → whale → services → bot →
  entry point → deployment) instead of one bulk commit.
- Added `.agent/` documentation: `PROJECT_STATE.md`, `TASK_QUEUE.md`,
  `ARCHITECTURE.md`, `DECISIONS.md`, `DATA_MODEL.md`, `CHANGELOG.md`.
- Added `.gitattributes` pinning `*.sh` and `Dockerfile` to LF so a Windows
  checkout still produces a Linux-runnable image.
- Extended `.gitignore` with `.claude/` and `render_preview.txt`.

## 2026-08-21 — Deployment surface

- `Dockerfile`: slim Python base, non-root user, SIGTERM/SIGINT handled by
  execing Python as PID 1.
- `railway.toml`: one service, `numReplicas = 1`, `/health` health check with a
  120 s timeout, `ON_FAILURE` restarts.
- `start.sh` for shell-start platforms (`exec python -m app.main`).
- `.env.example` documenting every variable with safe placeholder values only.

## 2026-08-21 — Entry point

- `app/main.py`: environment validation → PostgreSQL wait → Alembic upgrade →
  service restore → Telegram → Hyperliquid ingest, with graceful shutdown in
  reverse order.
- FastAPI `/health` returning healthy / degraded / unhealthy plus reasons,
  uptime, database stats, alert queue stats and configuration warnings.

## 2026-08-21 — Telegram surface

- 29 commands, inline control panel, callback router, prompt flow, error handler.
- Single authorization gate with a per-user token bucket; every callback
  re-authorised against `update.effective_user.id`.
- Verbatim spec message formats (§4A, §4B, §5, §16, §17, §18, §20) in
  `app/bot/messages/texts.py` and `app/services/alert_service.py`.

## 2026-08-21 — Services

- `SettingsService`: env-seeded, database-authoritative `RuntimeConfig` with
  audit rows and change listeners.
- `AdminService`: the §29 permission matrix, main admin unremovable, co-admins
  cannot manage admins.
- `AlertService`: render-at-enqueue, queued delivery, blocked-user handling,
  `alert_history` recording.

## 2026-08-21 — Whale pipeline

- `WhaleDetector` (pure): trades, position diffs, order lifecycle, book walls,
  TP/SL extraction from resting trigger orders.
- `WhaleFilter` (per-`ValueKind` thresholds, coin filter, detector toggles),
  `Deduplicator` (identity + magnitude-bucketed cooldown), `WalletTracker`
  (decayed scoring, focus slate).
- `WhaleEngine`: global discovery socket, weight-budgeted REST enrichment,
  per-wallet focus sockets, bounded queue with three workers.

## 2026-08-21 — Hyperliquid clients

- `HyperliquidREST`: `POST /info` with per-endpoint weights against a
  1200/min IP budget; never raises, failures are observable via counters.
- `HyperliquidWebSocket`: subscribe/resubscribe, heartbeat, exponential backoff
  with jitter, reconnect counter.
- Parsers producing typed models; constants for endpoint weights and WS limits.

## 2026-08-21 — Database

- 12 tables, 11 repositories, hand-written Alembic `0001_initial.py` verified by
  `upgrade head`, `alembic check` and a `downgrade base` → `upgrade head`
  round-trip.
- `env.py` honours `configure_logging` so migrations keep the secret-redacting
  log filter.

## 2026-08-21 — Foundation

- `Settings` from environment variables only; `validate_runtime()` raises a
  single aggregated `ConfigError` listing every fatal problem, and refuses
  SQLite in production.
- Logging with secret redaction, UTF-8 console reconfiguration, JSON mode.
- Backoff, TTL cache, weighted rate limiter, token bucket, formatting helpers.
