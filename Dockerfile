# ─────────────────────────────────────────────────────────────
# Hyperliquid Whale Intelligence Telegram Bot
#
# One image, one process, one Railway service: Telegram bot,
# Hyperliquid ingestion and the /health endpoint all run in
# `python -m app.main`. No dev server, no sidecar, no manual step.
# ─────────────────────────────────────────────────────────────
FROM python:3.13-slim AS base

# PYTHONUNBUFFERED: Railway must see log lines as they happen, not on flush.
# PYTHONFAULTHANDLER: a hard crash prints a traceback instead of vanishing.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl is used by the container healthcheck below; nothing else needs a compiler
# because every dependency ships a manylinux wheel for CPython 3.13.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first: this layer is cached until requirements.txt changes.
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY alembic.ini ./
COPY app ./app

# Run unprivileged. Nothing is written to the filesystem at runtime — all state
# lives in PostgreSQL — so no writable volume is needed.
RUN useradd --create-home --uid 10001 whale \
    && chown -R whale:whale /app
USER whale

# Documentation only; Railway injects the real PORT and app.config reads it.
EXPOSE 8080

# Railway probes this too, but an in-image check makes `docker run` honest.
# `degraded` returns 200 on purpose: a reconnecting websocket is not a reason to
# restart a working bot.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/health" > /dev/null || exit 1

# exec form: PID 1 is Python itself, so SIGTERM from a Railway redeploy reaches
# the signal handlers in app.main instead of being swallowed by a shell.
CMD ["python", "-m", "app.main"]
