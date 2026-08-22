# NOW

CURRENT TASK:
Critical persistence + settings safety fix (34-section spec). Two production
symptoms: (1) changing one setting replaced other settings, (2) settings lost
after a Railway redeploy. Both addressed — see CURRENT STATE.

CURRENT STATE:
Implementation complete and green. Root cause of symptom 1 was ONE path: the
coin panel's "✏️ Set list" prompt called `settings.set_coins()`, so an admin
monitoring BTC/ETH/SOL who answered "HYPE" was left monitoring only HYPE. Now
additive (`add_coins`), button relabelled "➕ Add coins", prompt text says "add".
Symptom 2 was mostly already correct (DB is authoritative, env seeds first boot
only); hardened with a `bootstrapped_at` marker so an empty coin list is never
re-seeded, plus a diff-based `CoinRepository.replace` so unrelated rows are not
rewritten.

FILES CHANGED (this task):
  app/services/settings_service.py   KEY_BOOTSTRAPPED, first_boot, last_coin_diff,
                                     add_coins(), reset_to_defaults(),
                                     decode_stored(); load() seeds only when the
                                     marker is absent
  app/database/repository.py         CoinRepository.replace -> diff, returns
                                     (added, removed)
  app/bot/handlers/prompts.py        kind == "coins" ADDS instead of replacing
  app/bot/handlers/admin.py          cmd_config, cmd_resetsettings; setcoins /
                                     addcoin report added vs removed
  app/bot/handlers/callbacks.py      CB_RESET area, two-step coin clear
                                     (clear -> clearyes), set:config
  app/bot/keyboards/inline.py        CB_RESET, "➕ Add coins", confirm_clear_coins,
                                     confirm_reset_settings, 🗄 Stored Configuration
  app/bot/views.py                   config_view() reads the tables directly
  app/bot/messages/texts.py          coins_added/coins_replaced/coins_cleared,
                                     confirm_*, settings_reset, config_snapshot
  app/services/admin_service.py      Capability.RESET_SETTINGS (main admin only)
  app/container.py                   startup_summary()
  app/main.py                        plain-text startup block + SQLite warning
  app/bot/handlers/__init__.py       /config, /resetsettings
  app/bot/commands.py                menu entries for both
  tests/test_persistence.py          NEW — 29 tests
  README.md                          "Persistence and settings safety" section

TESTS RUN:
  ./.venv/Scripts/python.exe -m compileall -q app/ tests/
  ./.venv/Scripts/python.exe -m pytest -q

TEST RESULTS:
  compileall: clean
  pytest: 555 passed, 0 failed (was 526 before this task's tests)
  tests/test_persistence.py: 29 passed
  Mutation-checked: reverting prompts.py to set_coins() makes
  test_the_add_coins_prompt_adds_instead_of_replacing fail with
  ('HYPE',) != ('BTC','ETH','HYPE','SOL') — the exact reported bug.

KNOWN ISSUES:
- No new migration was needed: no new table or column. 0001_initial +
  0002_alert_thread_key remain the whole history.
- Deliberately NO UNIQUE constraint on whale_events.dedup_key. Trade identity
  already includes the exchange tid; a hard UNIQUE would permanently block a
  legitimate repeat of an identical position change ("do not over-deduplicate").
  Non-unique index + 1h IDENTITY_TTL is the mechanism.
- inline.alert_settings_panel still toggles `enable_order_detector` and has no
  `enable_order_alerts` switch — a user-visible mismatch inherited from the
  paused execution/lifecycle task, not from this one.

NEXT ACTION:
Resume the paused 39-section "verified execution + position lifecycle" task:
alert_service._render_trade relabelling (§4 format, footer 🐋 CLEXER WHALE
MONITOR, 💰 Executed / 📦 Quantity, 🔎 VERIFIED EXECUTION), /recent + /whales
filtered to EXECUTION_EVENTS, split EventRepository.summary metrics (§24),
diagnostics counters (§25), the order-alerts toggle UI, then the 26 §31
regression tests. Assertions to update when the format changes:
tests/test_detector_liquidations.py:262,264 and
tests/test_engine_pipeline.py:263,264,270,286,624.

RELEVANT TEST:
./.venv/Scripts/python.exe -m pytest -q     -> 555 passed
