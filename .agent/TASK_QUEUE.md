# TASK QUEUE

Numbered against the 14-phase build order in spec §39. A task is only ✓ when
the code exists **and** has been exercised (run, migrated, or tested).

```
[✓] 001 — Project structure and packaging (pyproject, requirements, Dockerfile)
[✓] 002 — Configuration from environment variables + fatal startup validation
[✓] 003 — Logging with secret redaction, backoff, caches, rate limiters
[✓] 004 — Database models, repositories, Alembic migration
[✓] 005 — Hyperliquid REST client (weighted, budgeted)
[✓] 006 — Hyperliquid WebSocket client (reconnect + resubscribe)
[✓] 007 — Response parsers and typed models
[✓] 008 — Whale detector (trades, positions, orders, book)
[✓] 009 — Filters + deduplication + wallet tracker
[✓] 010 — Ingest engine (discovery → enrichment → focus wallets)
[✓] 011 — Settings / admin / alert services
[✓] 012 — Telegram bot: commands, panel, callbacks, prompts, permissions
[✓] 013 — Entry point: migrations on boot, /health, graceful shutdown
[✓] 014 — Test suite (unit + integration, spec §36) — 392 passed, 0 failed
[✓] 015 — README + API notes + test status (spec §51/§52)
[✓] 016 — Production checklist pass (spec §54)
[✓] 017 — Push to GitHub and deploy on Railway
         [✓] GitHub: origin = https://github.com/CLEXER17/CLEXWHALE_BOT
             main -> origin/main. The remote was verified empty before the
             first push, so nothing was overwritten.
         [✓] Railway: one service + PostgreSQL add-on, DATABASE_URL as a
             reference, BOT_TOKEN / MAIN_ADMIN_ID as Railway variables. The
             container started, migrations applied, /health served, Hyperliquid
             connected. No credential lives in this repository.
[~] 018 — Confirm live alert delivery
         [✓] Defect: every alert dropped ("Telegram bot not attached yet").
             Fixed in aa910bc; regression test proven to fail without it.
         [✓] Defect: secret redaction broke %d log placeholders into traceback
             storms. Fixed in c965da9; same proof.
         [ ] Redeploy shows "Telegram connected" and no "Alert dropped".
         [ ] A whale above the threshold actually arrives in Telegram.
         [ ] Token rotated after being exposed in a chat transcript (user).
```

## 018 breakdown — in progress

```
[✓] root-caused from the Railway log rather than guessed: PTB calls post_init
    only from run_polling/run_webhook (Application.initialize docstring)
[✓] fix verified necessary by stashing it and watching the new test fail with
    the production symptom (container.alerts.bot is None)
[✓] logging fix reproduces the exact production TypeError before the fix
[✓] production log path smoke-tested with LOG_JSON=true and registered secrets:
    clean JSON, token and DB password both ***REDACTED***
[✓] full suite re-run: 392 passed, 0 failed, 0 skipped
[ ] live confirmation — only the running deployment can give this
```

## 014 breakdown — complete

```
[✓] tests/conftest.py          — SQLite database, Settings(app_env="test"),
                                 AppContainer, fake Update/Context, state reset
[✓] tests/factories.py         — Trade/Position/Order/L2Book builders
[✓] tests/test_config.py       — missing vars fatal, sqlite-in-prod fatal, redaction
[✓] tests/test_detector_trades.py     — threshold, LONG, SHORT, coin filter
[✓] tests/test_detector_positions.py  — open/increase/decrease/close/flip, TP/SL
[✓] tests/test_detector_orders.py     — limit order, modify, partial fill, cancel
[✓] tests/test_filters.py      — per-class thresholds, monitoring off, coins
[✓] tests/test_dedup.py        — identity + cooldown + magnitude bucket
[✓] tests/test_permissions.py  — main admin, co-admin limits, users, public mode
[✓] tests/test_telegram_handlers.py — commands, crafted callback data, invalid input
[✓] tests/test_database.py     — repositories round-trip, main admin protection
[✓] tests/test_resilience.py   — WS reconnect/backoff, REST failure, rate limits
[✓] tests/test_engine_pipeline.py — end-to-end trade → alert integration
[✓] tests/test_bot_application.py — the real PTB Application: bot attached to
                                 the alert service, container published, every
                                 command registered, post_init idempotent
[✓] run: ./.venv/Scripts/python.exe -m pytest -q  → 392 passed in 33.45s
```

Task 014 also produced the only `app/` change of this phase:
`WhaleEngine._write_lock`, fixing a concurrent-write race that silently dropped
alerts. Recorded in `TEST_STATUS.md` and `DECISIONS.md`.

## 015 breakdown — complete

```
[✓] .agent/API_NOTES.md        — per-source fields available / NOT AVAILABLE
[✓] .agent/TEST_STATUS.md      — actual results, coverage map, limitations
[✓] 12 tables / 11 repositories correction in PROJECT_STATE.md + CHANGELOG.md
[✓] README.md                  — features, install, env table, BotFather,
                                 GitHub, Railway, Hyperliquid limitations
```

## 016 breakdown — complete

```
[✓] complete suite green (379 passed, 0 failed, 0 skipped)
[✓] compileall clean; `import app.main` ok
[✓] alembic upgrade head → downgrade base → upgrade head, single head
[✓] missing production vars raise one aggregated ConfigError (proven by running)
[✓] no secrets in the repository or in .agent/; no .env tracked
[✓] no duplicate implementations under app/
[✓] railway.toml + pyproject.toml parse; Dockerfile installs curl for HEALTHCHECK
[✓] seven logical commits (tests ×4, docs ×3)
[✓] deployment-readiness report delivered
```

## Deferred / explicitly out of scope

- Multi-replica deployment (would double-send alerts; `numReplicas = 1`)
- Wallet identity attribution to real-world people or companies (spec §20)
- Candle-based signals (the windows are event windows, not candles)
