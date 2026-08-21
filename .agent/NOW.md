# NOW

READ THIS FIRST. Keep it short — no history.

CURRENT PHASE:
Phase 15 — Deployment. State: **DEPLOYED ON RAILWAY, first run debugged.**

CURRENT TASK:
Task 018 — confirm the redeploy that carries the two production fixes actually
delivers alerts to Telegram. The user watches the Railway log and their chat;
nothing in the repository is waiting on an edit.

CURRENT FILE:
None. Working tree clean, `main == origin/main`.

CURRENT FUNCTION:
n/a.

LAST COMPLETED ACTION:
Fixed the two defects the first live deploy exposed, with regression tests that
were **verified to fail** against the code that shipped, then pushed:

1. `fix: attach the Telegram bot to the alert service at build time` (`aa910bc`)
   — the log said `Alert dropped: Telegram bot not attached yet` for every whale.
   `attach_bot()` was only reachable from `post_init`, and PTB calls `post_init`
   only from `run_polling` / `run_webhook`; `app.main.Runtime` drives the
   lifecycle by hand, so it never ran. `build_application()` now attaches the bot
   directly (`application.bot` exists as soon as the builder returns), and
   `post_init` is public, explicitly called by `Runtime.start()`, guarded against
   a double call. New module `tests/test_bot_application.py`.
2. `fix: stop secret redaction from corrupting %-style log arguments` (`c965da9`)
   — `SecretRedactor` coerced every `record.args` entry to `str`, so uvicorn's
   `"Started server process [%d]"` raised `TypeError` inside the formatter and
   Railway got screens of logging tracebacks. `_scrub_arg()` now preserves types;
   `_safe_message()` degrades a malformed call to one line. Redaction coverage is
   unchanged and tested.

Verification actually run, not assumed:
* `./.venv/Scripts/python.exe -m pytest -q` → **392 passed, 0 failed, 0 skipped**
* both fixes proven necessary: with the fix stashed, the new tests fail with the
  exact production symptoms (`container.alerts.bot is None`; `TypeError: %d
  format: a real number is required, not str`)
* `compileall app tests` clean; `import app.main` ok
* production log path smoke-tested with `LOG_JSON=true` and registered secrets:
  clean single-line JSON, token and DB password both `***REDACTED***`

CURRENT STATUS:
Green, committed, pushed, deployed. Alert delivery **not yet confirmed live** —
that needs the redeploy plus a whale above the threshold.

NEXT ACTION:
1. **The user must revoke the bot token** — it was pasted into a chat, so it is
   compromised. @BotFather → `/revoke` → new token into the Railway variable.
2. Confirm the redeploy: the log must now contain `Telegram connected` (that line
   only appears if `post_init` ran) and must **not** contain
   `Alert dropped: Telegram bot not attached yet`.
3. If alerts still do not arrive, the next suspect is recipients, not wiring:
   `AlertService._resolve_recipients` sends to `admins.admin_ids`, so check
   `MAIN_ADMIN_ID` matches the account watching, and that the user has not
   blocked the bot.

Never paste a token or a connection string into this repository, a commit
message, or any `.agent/` file.

KNOWN GAPS (honest, not blockers):
* No test drives `Runtime.start()` — a real database, Telegram login and bound
  port are needed. Defect 1 above lived precisely in that gap, so treat any
  change to the startup order in `app/main.py` as untested until it has run on
  Railway.
* The suite runs on SQLite; the Alembic scripts are exercised separately but not
  by pytest, and PostgreSQL-specific behaviour is untested.
* The Docker image has never been built here — Docker is not installed on this
  machine. Railway builds it.
* Live Hyperliquid payload shapes are documented in `API_NOTES.md` from the
  official docs; the first live run is the first real check of them.
