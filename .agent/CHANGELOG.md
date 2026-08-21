# CHANGELOG

Newest first. One entry per logical milestone; mirrors the git history.

---

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

- 12 tables, 12 repositories, hand-written Alembic `0001_initial.py` verified by
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
