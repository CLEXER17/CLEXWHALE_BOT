#!/usr/bin/env sh
# Optional entry point for platforms that prefer a shell start command
# (Railway's Nixpacks builder, Heroku, a plain VPS).
#
# The Dockerfile does NOT use this file — it execs Python directly. Migrations
# are applied by app.main itself, so this script only needs to hand over the
# process.
#
# `exec` matters: it replaces the shell so PID 1 is Python and SIGTERM from a
# redeploy reaches the graceful-shutdown handlers instead of killing the shell
# and orphaning the bot.
set -eu

exec python -m app.main
