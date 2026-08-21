# NOW

CURRENT TASK:
Task B: admin UI + wallet display + data integrity audit (17 issues), then
Task C: global /pause and /go.

CURRENT FILE:
app/bot/views.py  (whale_list / order_list / position_list / wallet_list still
call short_wallet — that is the `0x3200...c407` truncation seen live)

CURRENT FUNCTION:
views.whale_list -> app/utils/formatting.py::short_wallet callers

LAST COMPLETED:
Order/position state fix: app/whale/lifecycle.py + may_modify_position gate in
engine._persist + realised PnL from closedPnl + distance wording. 426 passed.

CURRENT PROBLEM:
Task B issue 1 needs the full 42-char address in list views; callback_data has a
64-byte limit, so lists must show the full address while buttons carry an
internal id that resolves to it (issue 12).

NEXT ACTION:
1 grep short_wallet callers in app/bot/views.py, replace with full <code> address
2 co-admin list button: trace callback_data -> handler -> permission -> repo
3 Telegram command scopes (BotCommandScope*) so users never see admin commands
4 duplicate whale events: event identity from exchange oid/tid, not output dedup
5 threshold semantics ($4.60M shown under a $5M threshold) + alerts-delivered 0
6 /pause + /go global gate
Then item 3 TP/SL fetch, item 5 liquidation alert, item 6 durability log.

RELEVANT TEST:
./.venv/Scripts/python.exe -m pytest -q
