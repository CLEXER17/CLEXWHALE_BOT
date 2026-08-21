# NOW

READ THIS FIRST. Keep it short — no history.

CURRENT PHASE:
Phase 15 — Deployment. State: PUSHED TO GITHUB, awaiting Railway.

CURRENT TASK:
Task 017 — GitHub half **done**. Railway half is blocked on the user: it needs
their Railway account, a PostgreSQL add-on, and their `BOT_TOKEN` /
`MAIN_ADMIN_ID` set as Railway variables. No credential exists in this
repository and none may be added to it.

CURRENT FILE:
None. Code and documentation are complete, committed and pushed.

CURRENT FUNCTION:
n/a.

LAST COMPLETED ACTION:
Connected the remote the user supplied and pushed:

```
origin  https://github.com/CLEXER17/CLEXWHALE_BOT.git
main -> origin/main   (HEAD == origin/main)
```

The remote was verified **empty** (`git ls-remote` returned no refs) before
pushing, so nothing was overwritten. Pre-push check confirmed no `.env` and no
key material is tracked — only `.env.example`, with every value blank.

Seven commits are now on `main`:

1. `test: add fixtures and unit coverage for config, detection and permissions`
2. `test: add database persistence coverage`
3. `test: add resilience coverage`
4. `test: add end-to-end engine pipeline coverage` (includes the
   `WhaleEngine._write_lock` fix that test found)
5. `docs: add Hyperliquid API notes and test status`
6. `docs: add production README`
7. `docs: record the GitHub remote`

Verification actually run, not assumed:
* `./.venv/Scripts/python.exe -m pytest -q` → **379 passed, 0 failed,
  0 skipped**
* `compileall app tests` → clean; `import app.main` → ok
* `alembic upgrade head` → `downgrade base` → `upgrade head` → clean, single
  head `0001_initial`
* Missing-var behaviour proven by execution: `APP_ENV=production` with no vars
  raises one aggregated `ConfigError` naming `BOT_TOKEN`, `MAIN_ADMIN_ID` and
  `DATABASE_URL`. No fallback to a placeholder credential.
* `railway.toml` and `pyproject.toml` parse as TOML; `Dockerfile` installs the
  `curl` its HEALTHCHECK uses.

CURRENT STATUS:
Green, committed, pushed. **Not deployed.**

NEXT ACTION:
Railway, by the user:
*New Project → Deploy from GitHub repo → `CLEXER17/CLEXWHALE_BOT`* →
*New → Database → PostgreSQL* → set `DATABASE_URL` as a reference to
`${{Postgres.DATABASE_URL}}` → set `BOT_TOKEN` and `MAIN_ADMIN_ID` → deploy →
watch `/health` → `/start` in Telegram. Full sequence in `README.md`.

Never paste a token or a connection string into this repository, a commit
message, or any `.agent/` file.

KNOWN GAPS (honest, not blockers):
* The suite runs on SQLite; the Alembic scripts are exercised separately (see
  above) but not by pytest, and PostgreSQL-specific behaviour is untested.
* The Docker image has never been built here — Docker is not installed on this
  machine. `Dockerfile` and `railway.toml` are verified by inspection and by
  config parsing only.
* No live Hyperliquid or Telegram traffic has ever been exercised end to end.
