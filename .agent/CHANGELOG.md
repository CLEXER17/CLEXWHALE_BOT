# CHANGELOG

Newest first. One entry per logical milestone; mirrors the git history.

---

## 2026-08-22 — Persistence and settings safety

Two symptoms reported from production: *"when I change one setting, the bot
unnecessarily replaces other existing settings"* and *"data/settings are lost after
Railway redeploys."* Both were investigated in the existing code rather than
answered with a rewrite, and the honest finding is that most of the settings layer
was already correct — `set_value` writes one row and patches one field, `/addcoin`
and `/removecoin` were already single-row, and `load()` already seeded only absent
keys. What follows is the one real bug, plus the hardening that makes the second
symptom impossible to reintroduce quietly.

**The bug (symptom 1).** The coin panel's `✏️ Set list` button opened a prompt
whose handler called `settings.set_coins()`. An admin monitoring BTC/ETH/SOL who
answered `HYPE` was left monitoring **only** HYPE — a whole-list replacement
wearing the label of an addition. Fixed in `app/bot/handlers/prompts.py` by
calling the new `add_coins()`; the button now reads `➕ Add coins` and the prompt
text says "add". Removal keeps its own explicit paths, none of which can be
reached by typing a coin name.

**The hardening (symptom 2).**

- **`bootstrapped_at`** (`app/services/settings_service.py`). A marker row written
  the first time the bot ever opens a database. Its presence turns "should
  defaults be applied?" from an inference about an empty table into a recorded
  fact, so an admin who deliberately monitors nothing does not find
  `DEFAULT_COINS` resurrected by the next redeploy. Deliberately excluded from
  `BOOL_KEYS`: it is a fact about the installation, not a preference.
- **`CoinRepository.replace` is now a diff** returning `(added, removed)` instead
  of `DELETE FROM tracked_coins` followed by re-inserts. Adding HYPE no longer
  rewrites the BTC row, `added_by` and `created_at` survive for coins that were
  not changing, and two admins editing at once can no longer resurrect rows the
  other just removed.
- **`add_coins()`** returns `(added, already_present)` so a repeated `/addcoin
  HYPE` is an explicit no-op with a clear answer rather than a silent success that
  leaves the admin wondering whether a duplicate was created.
- **`decode_stored()`** returns the raw text when JSON decoding fails, so a
  corrupt row is visible in `/config` instead of hidden behind a default.

**New safety surfaces.**

- **`/config`** (`views.config_view` → `texts.config_snapshot`). Reads `settings`,
  `tracked_coins`, `admins`, `tracked_wallets` and `users` directly and compares
  each field against the running cache, so drift is reported rather than
  self-confirmed. Prints the storage *kind* only — never a connection string.
- **`/resetsettings`** — two-step, and gated by a new
  `Capability.RESET_SETTINGS` in `MAIN_ONLY_CAPABILITIES`. A co-admin may change
  any individual setting, but one command that discards all of them at once is a
  different kind of act; a forged `reset:confirm` callback hits the same refusal.
  The reset restores setting rows and coins only: admins, users, watched wallets,
  whale events and alert history are records, not preferences, and are kept.
- **Two-step 🧹 Clear** for the coin list (`coin:clear` → `coin:clearyes`), and
  `/setcoins` now names every coin it removed.
- **`container.startup_summary()`** plus a plain-text block in `app/main.py`:
  storage backend, `durable` vs `ephemeral`, `LOADED` vs `SEEDED`, coins,
  threshold, mode, admin counts, watched wallets. Counts and states only — the
  redaction filter would catch a leak, but the right place not to log a secret is
  to not assemble one. A SQLite deployment gets an explicit warning that settings
  will not survive a redeploy.

**No migration was needed.** No new table and no new column: `bootstrapped_at` is
a row in the existing key/value `settings` table, and every item the spec asked to
persist already had a write site. `0001_initial` and `0002_alert_thread_key` remain
the whole history.

**Deliberately not done:** a UNIQUE constraint on `whale_events.dedup_key`. Trade
identity already includes the exchange `tid`, and duplicates from a reconnect are
caught by `seen_recently` within the 1-hour `IDENTITY_TTL`. A hard UNIQUE would
permanently block a legitimate repeat of an identical position change — losing real
events to avoid duplicate ones.

**Tests:** `tests/test_persistence.py` (new, 29 tests) — patch semantics, the §33
acceptance sequence, restart and redeploy survival for coins/threshold/mode/pause/
toggles/co-admin role/wallets, defaults-only-where-absent, every removal path's
confirmation, the co-admin refusal, `/config` drift detection, and the startup
summary's silence about credentials. Mutation-checked: reverting the prompt to
`set_coins()` reproduces the reported `('HYPE',)`. Suite: **555 passed, 0 failed**
(was 526).

`README.md` gained a "Persistence and settings safety" section documenting
`DATABASE_URL`, what survives a redeploy, the four operations that remove
anything, and the production fail-fast.

---

## 2026-08-22 — The admin/wallet/data-integrity audit, and a global pause

The 17 reported defects in the "ADMIN UI + WALLET DISPLAY + DATA INTEGRITY"
audit had already been fixed in the production code; what was missing was the
suite that proves each one stays fixed. Written against the *seam that caused*
each defect rather than the Telegram text that displayed it — a test that only
asserts on the rendered string passes just as happily when the root cause comes
back wearing different words.

- **`tests/test_admin_ui_integrity.py` (new, 32 tests).** Issues 1–14. Wallets:
  the canonical value is asserted at the store, at the round trip through the
  database, and in every list view and alert body, with `TRUNCATED =
  re.compile(r"0x[0-9a-fA-F]{2,10}(\.{2,3}|…)")` used as an explicit
  never-matches guard; `short_wallet` stays confined to button labels. Callbacks:
  all 19 `inline.py` builders checked for a wallet-free, ≤ 64-byte payload, and a
  wallet proved to be resolved from the database rather than the payload.
  Identity: `identity_key()` for one `tid` observed twice — once before
  enrichment (`BUY`) and once after (`LONG`) — plus the durable
  `_already_recorded` gate for a duplicate that outlived the memory cache.
  Never-invent-data: real `WhaleDetector` output through `container.alerts.render`
  for the aggregated book level with no owner, the position with no snapshot, and
  the absent liquidation price. Authority: every admin command driven *as a
  stranger* and refused server-side, `publish_command_menus()` inspected per
  Telegram scope, and a demoted co-admin's chat scope deleted.
- **`/recent` was advertised publicly but published only to admins** — the one
  real production bug the new suite found. `data.cmd_recent` requires only
  `VIEW_WHALES` and `texts.help_text` listed it for everyone, but
  `BotCommand("recent", …)` sat in `CO_ADMIN_EXTRA`, so it reached only the admin
  chat scopes. Fixed in `app/bot/commands.py` by moving `recent` into
  `PUBLIC_COMMAND_MENU`. The three hand-written sources of truth (menu, help
  text, handler capability) are now held together by
  `test_what_is_advertised_publicly_is_exactly_what_a_user_may_invoke`, which
  drives every advertised command as a stranger expecting no refusal and every
  admin command as a stranger expecting refusal — so drift is caught in both
  directions. One caller id per command, because the rate limiter is per user.
- **`tests/test_global_pause.py` (new, 15 tests).** `/pause` stops everything and
  `/go` starts it again. The gate is enforced once in the middleware — a
  per-handler check is a check somebody forgets to add — and the tests hold its
  four properties: every other command, inline button and half-finished prompt is
  refused; the exemptions are exactly `/go`, `/status`, `/panel` and `/stop`
  (refusing the read-only views would leave an admin unable to see *why*
  everything is refused, and being asked to stop messaging someone is not a
  privileged operation); `monitoring_enabled` is left exactly as configured, so
  `/go` restores rather than switches on; and the pause is a database setting, so
  two fresh `AppContainer.restore()` cycles come back paused rather than silently
  resuming. A normal user is told the bot is paused but never told `/go` exists.
- Suite is now **474 passed, 0 failed, 0 skipped** in 28.58 s (from 426), and
  `compileall app/ tests/` is clean.

---

## 2026-08-21 — Order state and position state are no longer allowed to bleed

An order and a position are separate objects with separate lifecycles. Live
alerts showed the two blurring together: a resting `SELL LIMIT` read as evidence
of a short, a bare `📐 Distance` printed by three different renderers measuring
three different things, and a closed position's last unrealised PnL printed as if
it were the realised result. The fixes are in the detection/state layer, not only
in the formatter.

- **`app/whale/lifecycle.py` (new).** The two state machines written down
  explicitly — `OrderStatus` (`PLACED → OPEN → PARTIALLY_FILLED → FILLED /
  CANCELLED / REJECTED`, plus `UNRESOLVED` for a disappearance the exchange never
  explained) and `PositionStatus` (`NO_POSITION → OPENED → ACTIVE → REDUCED →
  CLOSED`) — with the legal transitions and, most importantly,
  `may_modify_position(event)`: **False** for every order event, for
  `BOOK_LEVEL`, and for an executed trade that arrived without a verified
  `clearinghouseState` snapshot behind it. `position_status_of()` returns `None`
  for every order event by construction.
- **`WhaleEngine._persist` now routes on that gate.** Previously a `WHALE_TRADE`
  with no position context could write a `positions` row whose side was the
  *trade* side (`BUY`/`SELL`) — the actual conflation, in the database rather
  than the message. Order events go to `_persist_order`; position state is
  written only when `may_modify_position()` allows it.
- **Realised PnL is captured where Hyperliquid actually reports it.**
  `closedPnl` on the per-wallet `userEvents`/`userFills` frames is the only
  realised figure the API gives, so the engine accumulates it per
  (wallet, coin) — bounded at 500 keys — and attaches it to a `POSITION_CLOSED`
  event. The renderer then prints `Realized PnL` (confirmed) → `Final PnL (est.)`
  (the last observed unrealised PnL, now labelled `ESTIMATED`) → `Final PnL: N/A`.
  Nothing is fabricated and an estimate is never presented as realised.
- **A close is measured from the last verified non-zero snapshot**, and entry,
  leverage, liquidation and TP/SL are *not* reconstructed for a position that no
  longer exists — the alert says `ℹ️ Historical position details unavailable`
  instead of a wall of `N/A`.
- **Distance wording is unambiguous.** An order's distance from the mark reads
  `📐 Distance: +1.37% above` / `-0.36% below` (never a bare sign); a position's
  reads `📐 From Entry: -0.06%`; a bare execution with no position reads
  `📐 Fill vs mark:`. New `entry_distance_pct` data point keeps the two
  measurements from ever sharing a label.
- **`_order_side_line` refuses anything that is not literally BUY or SELL**, so a
  position word cannot leak into an order alert even if an upstream field is
  wrong.
- **New module `tests/test_order_position_separation.py`** (20 tests): the seven
  named regression cases (resting SELL with no position; short + resting SELL
  stay separate; size → 0 closes from the pre-close snapshot; a SELL placed after
  a close is not a short opening; a fill does not open a position by itself;
  resting BUY is never a long; a cancellation is not a close) plus the
  `lifecycle` invariants and the distance/PnL wording. Suite is now
  **426 passed, 0 failed, 0 skipped**.

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
