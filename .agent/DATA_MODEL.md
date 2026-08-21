# DATA MODEL

Two layers: in-memory domain objects (`app/hyperliquid/models.py`,
`app/whale/events.py`) and persisted tables (`app/database/models.py`). Keep this
file in sync with those modules.

---

## Confidence wrapper

```
DataPoint (app/utils/formatting.py)
  value        float | str | None
  confidence   CONFIRMED | ESTIMATED | UNAVAILABLE
  note         str | None     # why it is estimated or unavailable
  available    bool
```

`DataPoint.confirmed(v)`, `.estimated(v, note)`, `.unavailable(note)`.
Rendering appends `(est.)` for ESTIMATED and prints the note (or `N/A`) for
UNAVAILABLE. This is the mechanism that keeps "not available" distinct from
"zero" and from "invented".

---

## Hyperliquid domain objects

```
Trade          coin, px, sz, side("B"/"A"), time, tid, users[2]
               → notional, taker, maker, participants, direction

BookLevel      px, sz, n            (aggregated, no wallet)
L2Book         coin, time, bids[], asks[]

Position       coin, size, side, entry_px, position_value, unrealized_pnl,
               leverage, leverage_type, liquidation_px, margin_used,
               max_leverage, return_on_equity
AccountState   address, time, account_value, total_margin_used,
               total_notional, withdrawable, positions{coin: Position}

OpenOrder      oid, coin, side, limit_px, sz, orig_sz, timestamp,
               order_type, reduce_only, is_trigger, trigger_px, trigger_condition
               → price prefers trigger_px when is_trigger, notional

OrderUpdate    oid, coin, side, limit_px, sz, orig_sz, status, timestamp, …
               → notional, original_notional

Fill           coin, px, sz, side, time, oid, hash, closed_pnl, fee, dir
```

## Detector inputs

```
PositionContext  position, trigger_orders[], account_value, first_seen, orders_known
OrderState       oid, coin, side, limit_px, size, orig_size, notional,
                 status, is_trigger, trigger_px, order_type, reduce_only, placed_at
```

`orders_known=False` means TP/SL were never fetched for that wallet, which
renders as "not publicly detectable" rather than "none set".

---

## WhaleEvent (in memory)

```
event_type    EventType   WHALE_TRADE | POSITION_OPENED | POSITION_INCREASED |
                          POSITION_DECREASED | POSITION_CLOSED | POSITION_FLIPPED |
                          ORDER_PLACED | ORDER_MODIFIED | ORDER_PARTIALLY_FILLED |
                          ORDER_FILLED | ORDER_CANCELLED | ORDER_REJECTED | BOOK_LEVEL
value_kind    ValueKind   TRADE_VALUE | ORDER_NOTIONAL | POSITION_NOTIONAL |
                          POSITION_DELTA | MARGIN | BOOK_LEVEL_NOTIONAL
coin          str
side          "LONG" | "SHORT" | "BUY" | "SELL" | None
wallet        str | None            (None for book walls — no attribution)
notional      float                 the number the threshold is applied to
event_time    datetime (UTC)
detection     str                   human-readable reason ("Large Position", …)
points        dict[str, DataPoint]  entry, leverage, liquidation, tp, sl,
                                    current, distance, size, position_value,
                                    margin, status_note, wallet_attribution, …
dedup_key     str                   set by Deduplicator.check()
```

Helpers: `set/point/value/numeric/has/confidence`, `is_position_event`,
`is_order_event`, `threshold_class`, `value_kind_label`, `detail_json`,
`db_fields`.

---

## Tables (PostgreSQL, all timestamps timezone-aware UTC)

**admins** — `id`, `telegram_id` (unique), `username`, `role` (`MAIN_ADMIN` |
`CO_ADMIN`), `added_by`, `note`, `created_at`, `updated_at`.

**users** — `telegram_id` (pk), `chat_id`, `username`, `first_name`,
`is_subscribed`, `is_blocked`, `alerts_received`, `created_at`, `last_seen_at`.

**settings** — `key` (pk), `value` (text), `updated_by`, `updated_at`.
Keys: monitoring, public mode, threshold(s), cooldown, coins mode, alert
toggles, window. The database wins over the environment after first boot.

**tracked_coins** — `coin` (pk), `enabled`, `added_by`, `created_at`.

**tracked_wallets** — `address` (pk), `label`, `added_by`, `created_at`.
Pinned into the focus slate.

**wallets** — `address` (pk), `first_seen`, `last_seen`, `event_count`,
`long_volume`, `short_volume`, `largest_position`, `largest_order`,
`total_notional`, `account_value`, `coins` (JSON per-coin counters).

**whale_events** — `id`, `event_type`, `coin`, `side`, `wallet`, `notional`,
`value_kind`, `price`, `size`, `entry_px`, `liquidation_px`, `leverage`,
`take_profit_px`, `stop_loss_px`, `position_value`, `order_id`, `status`,
`detail` (JSON: the full DataPoint set with confidences), `dedup_key`,
`event_time`, `created_at`, `alerted`.
Indexes: `(coin, event_time)`, `(event_type, event_time)`, `notional`, `wallet`.

**orders** — `id`, `oid`, `wallet`, `coin`, `side`, `limit_px`, `size`,
`orig_size`, `notional`, `orig_notional`, `order_type`, `is_trigger`,
`trigger_px`, `reduce_only`, `status`, `placed_at`, `closed_at`, `created_at`,
`updated_at`. Unique `(wallet, oid)`.

**positions** — `id`, `wallet`, `coin`, `side`, `size`, `entry_px`,
`position_value`, `liquidation_px`, `leverage`, `leverage_type`, `margin_used`,
`unrealized_pnl`, `take_profit_px`, `stop_loss_px`, `max_notional`, `is_open`,
`opened_at`, `closed_at`, `updated_at`. Unique `(wallet, coin)`.

**alert_history** — `id`, `event_id`, `dedup_key`, `chat_id`, `message_id`,
`ok`, `error`, `sent_at`.

**admin_audit** — `id`, `admin_id`, `action`, `target`, `old_value`,
`new_value`, `created_at`. (Spec §31.)

**bot_logs** — `id`, `level`, `source`, `message`, `context` (JSON),
`created_at`. Operational breadcrumbs only; never secrets.

---

## Runtime config snapshot

`RuntimeConfig` (frozen dataclass, `app/services/settings_service.py`) is what
the rest of the app reads: `monitoring_enabled`, `public_mode`,
`min_whale_value` + per-class minimums, `alert_cooldown_seconds`,
`monitor_all_coins`, `coins`, `tracked_wallets`, detector toggles, window.
Methods: `threshold_for(kind)`, `effective_thresholds()`, `lowest_threshold()`,
`coin_enabled(coin)`, `coin_label`, `detector_enabled(name)`.
