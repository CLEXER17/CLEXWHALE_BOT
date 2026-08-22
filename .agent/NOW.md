# NOW — resume here, read this file first

Updated: 2026-08-22

---

## CURRENT TASK

**LIVE VERIFICATION PHASE.** Move OFFLINE VERIFIED → LIVE VERIFIED for the chain
Railway → PostgreSQL → Hyperliquid WS/REST → detector → alert service → Telegram.
Runbook: `.agent/LIVE_VERIFICATION.md` (the only other file worth opening).

The implementation phase is over. Observe → record → minimal fix → test.

## CURRENT SUBTASK

Step 2 (configuration audit) is done offline. Step 3 onward is blocked — see
BLOCKED BY.

## STATUS

| Step | Item | State |
|---|---|---|
| 1 | Read current state (`NOW.md`, `git status`, `git diff --stat`) | done — clean at `d0194d7` |
| 2 | Configuration audit: required vars, no hardcoded credentials, `.env` ignored, migrations on boot, healthcheck, single replica | **PASS** (offline) |
| 2 | Token rotation actually performed | **NOT VERIFIED** — only @BotFather can confirm |
| 3–12 | Startup, persistence, execution, lifecycle, wallet, toggle, permissions, co-admin, `/recent`, `/whales` | **NOT VERIFIED** — blocked |

## FILES BEING MODIFIED

None. No code change is expected in this phase unless a live failure proves one.

## FILES ALREADY COMPLETED

- `.agent/LIVE_VERIFICATION.md` — **new**: the Step 2 audit results, the exact
  startup lines to look for, the Telegram scripts for steps 4–12, and an evidence
  table to fill in.
- Offline work is committed and pushed at `d0194d7` (587 passed / 0 failed).

## TESTS PASSED

- `./.venv/Scripts/python.exe -m pytest -q` → **587 passed in 49.82 s** (2026-08-22)
- `./.venv/Scripts/python.exe -m compileall -q app/ tests/` → clean

## TESTS FAILED

None.

## BLOCKED BY

**No CLEXWHALE_BOT service exists on Railway.** Checked with the authenticated
CLI (`clexer123@gmail.com`, workspace *clexer2's Projects*):

- `noble-creation` → `Postgres`, `CENTRAL API` (builds `CLEXER17/CLEXER_BOT`),
  `CO3` (an Aerolink/Gemini app). None has `BOT_TOKEN`, `MAIN_ADMIN_ID`,
  `MIN_WHALE_VALUE` or `HYPERLIQUID_WS_URL`. Variable **names** only were read.
- `devoted-unity` → **no services at all**.

So there is nothing to deploy the current commit to, and nothing to read logs
from. Creating the service needs `BOT_TOKEN` and `MAIN_ADMIN_ID` typed into the
Railway dashboard by a human — a token must never enter a chat, a commit, or an
`.agent/` file, and this one was already exposed once.

Steps 4–12 additionally need three Telegram accounts (main admin, co-admin,
stranger) and real Hyperliquid fills arriving live. An agent cannot supply those.

## NEXT EXACT ACTION

Waiting on the user for one decision: **which Railway project/service should host
`CLEXER17/CLEXWHALE_BOT`** — a new service in the empty `devoted-unity`, a new
service in `noble-creation` alongside the existing Postgres, or a deploy the user
performs themselves in an account this CLI cannot see.

Once a service exists with `BOT_TOKEN`, `MAIN_ADMIN_ID` and
`DATABASE_URL = ${{Postgres.DATABASE_URL}}`:

1. `railway logs --service <name> | tail -60` → confirm
   `Database ......... CONNECTED (postgresql, durable)` and
   `Configuration .... LOADED from database`, no reconnect loop, no
   `Alert dropped: Telegram bot not attached yet`.
2. Work through `.agent/LIVE_VERIFICATION.md` steps 4–12, filling the evidence
   table. `NOT VERIFIED` is a valid answer; never record `PASS` without output.

## DO NOT START

- **Persistence / settings work — COMPLETE and accepted (`6a0d677`).**
- **Verified execution / position lifecycle — COMPLETE (`d0194d7`). 587 passed.**
- Do not redo checkpoints 1–10; do not redesign the architecture.
- No new migration unless a real live failure proves one is required.
- No UNIQUE constraint on `whale_events.dedup_key`.
- Do not change working logic because it could be improved. This phase observes.
- Never run `git reset --hard`, `git checkout .`, or `git clean -fd`.

## NOT VERIFIED

Everything live: startup, PostgreSQL durability, settings surviving a restart,
BTC/ETH/SOL preserved when HYPE is added, threshold persistence, the Hyperliquid
websocket, execution detection, position lifecycle, `/recent`, `/whales`, the
order-alert toggle, main-admin/co-admin/normal-user permissions, and full wallet
formatting on a real screen. No deployment was observed.

## USER-OWNED (not mine to do)

1. Confirm the exposed `BOT_TOKEN` was revoked via @BotFather `/revoke`.
2. Create/point the Railway service and set its variables in the dashboard.
3. Drive the Telegram steps from three accounts and capture the evidence.
