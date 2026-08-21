# NOW

READ THIS FIRST. Keep it short — no history.

CURRENT PHASE:
Phase 15 — Pre-deployment verification (§54). State: COMPLETED (awaiting the
user's GitHub remote).

CURRENT TASK:
Task 016 — final verification and the deployment-readiness report. Done. Task
017 (push + deploy) is **blocked on the user**: no Git remote exists and none may
be invented.

CURRENT FILE:
None. Code and documentation are complete and committed.

CURRENT FUNCTION:
n/a.

LAST COMPLETED ACTION:
Six logical commits, ending with `docs: add production README`:

1. `test: add fixtures and unit coverage for config, detection and permissions`
2. `test: add database persistence coverage`
3. `test: add resilience coverage`
4. `test: add end-to-end engine pipeline coverage` (includes the
   `WhaleEngine._write_lock` fix that test found)
5. `docs: add Hyperliquid API notes and test status`
6. `docs: add production README`

Verification actually run, not assumed:
* `./.venv/Scripts/python.exe -m pytest -q` → **379 passed, 0 failed,
  0 skipped in 43.00s**
* `compileall app tests` → clean; `import app.main` → ok
* `alembic upgrade head` → `downgrade base` → `upgrade head` → clean, single
  head `0001_initial`
* `railway.toml` and `pyproject.toml` parse as TOML; `Dockerfile` installs the
  `curl` its HEALTHCHECK uses
* Secret scan: no real token, no connection string, no hardcoded admin id. The
  only token-shaped strings are the obvious fakes in `tests/conftest.py` and
  `tests/test_config.py` that exist to test redaction.

CURRENT STATUS:
Green and committed. Nothing is pushed. Nothing is deployed.

NEXT ACTION:
Wait for the user. Deployment needs two things this repository cannot supply:
their own GitHub repository URL, and their `BOT_TOKEN` / `MAIN_ADMIN_ID` set as
Railway variables. When the user provides the remote:
`git remote add origin <their url>` → `git push -u origin main` → Railway
"Deploy from GitHub repo" → provision PostgreSQL → reference `DATABASE_URL` →
set `BOT_TOKEN` and `MAIN_ADMIN_ID` → deploy → watch `/health` → `/start` in
Telegram. The full sequence is in `README.md`.

DO NOT invent a remote URL. DO NOT push without being asked.

KNOWN GAPS (honest, not blockers):
* The suite runs on SQLite; the Alembic scripts are exercised separately (see
  above) but not by pytest, and PostgreSQL-specific behaviour is untested.
* The Docker image has never been built here — Docker is not installed on this
  machine. `Dockerfile` and `railway.toml` are verified by inspection and by
  config parsing only.
* No live Hyperliquid or Telegram traffic has ever been exercised end to end.
