# NOW

CURRENT TASK:
Verified execution + position lifecycle (39-section spec). The primary Telegram
feed must carry EXECUTIONS ONLY. Order placed/resting/modified/cancelled become
internal tracking; ORDER FILLED / EXECUTED becomes the user-facing WHALE TRADE.

CURRENT FILE:
app/services/settings_service.py (adding enable_order_alerts)

CURRENT FUNCTION:
RuntimeConfig / SettingsService._build

LAST COMPLETED:
Reconnaissance of engine.py, settings_service.py, dedup.py, detector.py,
alert_service.py, views.py, handlers/data.py, EventRepository. Liquidation work
(items 3 + 5) committed and pushed as 8d48846.

DECISION (settled, implement as written):
1 WHALE_TRADE is *additionally* sourced from userEvents fills — not replaced.
  The public `trades` feed already reports executions, so it is already verified
  and only needs relabelling; fills add order-ID/fill-ID anchored executions.
2 identity_key for WHALE_TRADE is ("trade", tid, role) already, so a fill sets
  context["role"] = "taker" if fill.crossed else "maker" and one execution seen
  on both feeds alerts exactly once (§7 one alert per delivery, §30 no
  over-dedup).
3 enable_order_detector KEEPS its meaning = INTERNAL order tracking (default
  True, required by §27 and by the focus slate that also carries fills). A NEW
  enable_order_alerts (default False) gates user-facing order events in the
  filter with reason REASON_ORDER_ALERTS_OFF. Defaults: TRADE ON, ORDER OFF,
  POSITION ON.
4 Deviation to disclose in the §39 report: §4's example trade block shows no
  position-enrichment lines; keeping verified enrichment (TP/SL, item 3) as
  extras below the execution core, because deleting it would undo working
  functionality.

CURRENT PROBLEM:
none.

NEXT ACTION:
1 config.py + settings_service.py: enable_order_alerts (default False)
2 filters.py: REASON_ORDER_ALERTS_OFF
3 detector.from_fill + engine _on_user_message ordinary-fill branch
4 alert_service labels (WHALE TRADE / Executed / VERIFIED EXECUTION / footer
  CLEXER WHALE MONITOR), split counters, /recent + /whales execution filter
5 the 26 §31 regression tests + §32 integration + §33 scenario

RELEVANT TEST:
./.venv/Scripts/python.exe -m pytest -q     -> 524 passed (baseline, pre-change)
