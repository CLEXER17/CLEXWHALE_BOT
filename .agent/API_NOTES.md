# API NOTES — Hyperliquid

Purpose: record, per data source, **exactly** what this project can and cannot
obtain. Nothing here is remembered; every row was checked against the client code
in `app/hyperliquid/` and against Hyperliquid's public API documentation.

Rules this file exists to enforce:

* Never claim Hyperliquid provides TP, SL, liquidation price, margin, or pending
  orders unless the specific data source below is verified to provide it.
* If a field cannot be obtained from a source, it is written as
  **`NOT AVAILABLE FROM THIS DATA SOURCE`**. It is never estimated into
  existence, and never hardcoded.
* A value that this project *derives* is labelled DERIVED and is rendered with an
  "estimated" marker, never as a trader-set value.

Authentication: **none**. Every source used is a public read endpoint. This
project never signs, never trades, and holds no key material. Nothing here needs
an API key, and none is accepted.

Verification date: **2026-08-21** (all rows).
Base URLs: `https://api.hyperliquid.xyz` (REST, `POST /info`) and
`wss://api.hyperliquid.xyz/ws`. Both are configurable via
`HYPERLIQUID_API_URL` / `HYPERLIQUID_WS_URL`; no host is hardcoded in logic.

---

## 0. Global limits that shape the architecture

| Limit | Value | Where enforced |
|---|---|---|
| Aggregated REST weight | 1200 / minute / IP | `app/utils/ratelimit.py` (`WeightedRateLimiter`), consumed by `HyperliquidREST.post_info` |
| Weight: `userRole` | 60 | `constants.info_weight` |
| Weight: `l2Book`, `allMids`, `clearinghouseState`, `orderStatus`, `spotClearinghouseState`, `exchangeStatus` | 2 | `constants.CHEAP_INFO_REQUESTS` |
| Weight: everything else (incl. `meta`, `metaAndAssetCtxs`, `frontendOpenOrders`, `openOrders`, `userFills`, `candleSnapshot`) | 20 | `constants.WEIGHT_DEFAULT` |
| WS connections per IP | 10 | `constants.MAX_WS_CONNECTIONS` |
| WS subscriptions per connection | 1000 | `constants.MAX_WS_SUBSCRIPTIONS`, engine caps itself lower |
| **WS unique user addresses per IP** | **10** | `constants.MAX_WS_UNIQUE_USERS`; this is why per-wallet streams are a small "focus slate" and everything else is REST-polled |
| WS inbound messages | 2000 / minute | `constants.MAX_WS_MESSAGES_PER_MINUTE` |

Consequence, recorded so it is never re-litigated: **there is no public global
feed of positions, of resting orders, or of liquidations.** Only `trades` is
global *and* attributed. Everything position-shaped is per-wallet and therefore
rate-limited. This is the single most important constraint in the project.

---

## 1. WS `trades` — the discovery backbone

* **Subscription**: `{"type": "trades", "coin": "<COIN>"}`, one per monitored coin.
* **Purpose**: the only global, wallet-attributed feed. Every whale candidate
  enters the system here.
* **Implementation**: subscribed in `WhaleEngine._desired_market_subscriptions`
  (`app/whale/engine.py`), parsed by `parser.parse_trades`, handled by
  `WhaleEngine._on_trade`.
* **Fields available**: `coin`, `side`, `px`, `sz`, `time` (ms), `tid`, `hash`,
  `users: [buyer, seller]`.
* **Side semantics — verified**: `side` is the **taker** side. `"B"` = the buyer
  was the aggressor (`users[0]` is the taker), `"A"` = the seller was
  (`users[1]` is the taker). See `constants.side_label`, `models.Trade.taker`.
* **Fields NOT AVAILABLE FROM THIS DATA SOURCE**: leverage, entry price,
  position size, position notional, margin, liquidation price, TP, SL, order id
  of the resting side, whether the trade opened / increased / reduced / closed a
  position, and whether either side was liquidated. A trade is a *trade value*
  (`px × sz`) and nothing more.
* **Derived here**: notional USD = `px × sz` (CONFIRMED — both inputs are
  exchange-reported). Direction shown as BUY/SELL unless a position snapshot is
  available to say LONG/SHORT.
* **Limitations**: no historical backfill on subscribe; frames can be replayed
  after a reconnect, which is why `tid` is the dedup identity
  (`app/whale/dedup.py`).

## 2. WS `allMids` — reference prices

* **Subscription**: `{"type": "allMids"}` (one, global).
* **Purpose**: current mid price per coin, used for distance-from-price and for
  valuing sizes.
* **Implementation**: `parser.parse_all_mids`, cached in `WhaleEngine._prices`.
* **Fields available**: `mids: {coin: price}`.
* **NOT AVAILABLE FROM THIS DATA SOURCE**: mark price, oracle price, funding,
  open interest, volume (those come from `metaAndAssetCtxs`, §6).

## 3. WS `l2Book` — aggregate resting depth

* **Subscription**: `{"type": "l2Book", "coin": "<COIN>"}`, only when the
  `enable_book_scanner` toggle is on.
* **Purpose**: detect unusually large *aggregate* resting size at one price.
* **Implementation**: `parser.parse_l2_book`, `WhaleEngine._process_book`.
* **Fields available**: per level `px`, `sz`, `n` (number of orders at that
  level). At most 20 levels per side.
* **Fields NOT AVAILABLE FROM THIS DATA SOURCE**: **wallet attribution** —
  `l2Book` never says whose orders these are, and `n > 1` means the size is
  several traders. Also no order ids, no order types, no TP/SL, no reduce-only
  flag.
* **How the project stays honest**: a book event is emitted as `BOOK_LEVEL` with
  the trader line rendered as `Trader: N/A — <reason>` and is never labelled a
  single whale's order. Full depth beyond 20 levels is unavailable.

## 4. WS `orderUpdates` — order lifecycle (per wallet)

* **Subscription**: `{"type": "orderUpdates", "user": "<address>"}` — consumes
  one of the **10 unique-address slots**.
* **Purpose**: real placement / fill / cancellation / rejection events for the
  admin-tracked focus wallets.
* **Implementation**: one dedicated socket per focus wallet in
  `WhaleEngine._apply_focus_slate`; frames are attributed by connection in
  `WhaleEngine._on_user_message` and parsed by `parser.parse_order_update`.
* **Fields available**: `order: {coin, oid, side, limitPx, sz, origSz, timestamp,
  cloid}`, `status`, `statusTimestamp`. The full status vocabulary is recorded in
  `constants.ALL_ORDER_STATUSES` (30 values, including the many `*Canceled` and
  `*Rejected` variants).
* **Verified quirk**: an `orderUpdates` frame does **not** name the user it
  belongs to. That is why the project opens **one socket per focus wallet** and
  attributes by connection rather than by payload.
* **Fields NOT AVAILABLE FROM THIS DATA SOURCE**: `orderType` (so TP/SL cannot be
  classified from this feed alone), `reduceOnly`, `triggerPx`,
  `isPositionTpsl`, and the resulting position state. Cross-reference
  `frontendOpenOrders` (§8) for those.
* **Limitation**: only 10 addresses total across *all* user subscriptions, and
  `userEvents` (§5) shares that budget. This feed can never cover the whole
  market.

## 5. WS `userEvents` — fills, funding, and the only liquidation signal

* **Subscription**: `{"type": "userEvents", "user": "<address>"}`. Arrives on
  channel **`user`**, not `userEvents` (`constants.CHANNEL_ALIASES`).
* **Purpose**: per-wallet fills; used to detect liquidations for focus wallets.
* **Implementation**: `parser.parse_fills`, `WhaleEngine._process_liquidation`.
* **Fields available**: per fill `coin`, `px`, `sz`, `side`, `time`, `oid`, `tid`,
  `hash`, `dir`, `closedPnl`, `startPosition`, `crossed`, `fee`, and
  `liquidation` (present only when that fill was a liquidation).
* **Liquidation — verified**: a liquidation is only observable as a fill carrying
  a `liquidation` object, i.e. **per wallet, after the fact**. There is
  **NO public global liquidation feed** and no way to see a liquidation for a
  wallet that is not one of the 10 subscribed addresses.
* **Fields NOT AVAILABLE FROM THIS DATA SOURCE**: the liquidated wallet's
  leverage or margin at the moment of liquidation, and any forward-looking
  liquidation risk.

## 6. REST `metaAndAssetCtxs` — the perpetual universe (weight 20)

* **Purpose**: which coins exist, their `szDecimals` / `maxLeverage`, and per-coin
  market context.
* **Implementation**: `HyperliquidREST.meta_and_asset_ctxs`, consumed by
  `WhaleEngine._refresh_universe`; also used to reject a configured coin that is
  not a Hyperliquid perp.
* **Fields available**: `universe: [{name, szDecimals, maxLeverage}]` and, index-
  aligned, `[{markPx, midPx, oraclePx, funding, openInterest, prevDayPx,
  dayNtlVlm, …}]`. Of those the parser keeps exactly `markPx`, `midPx`,
  `oraclePx`, `prevDayPx`, `funding`, `openInterest`, `dayNtlVlm`
  (`models.AssetContext`); anything else the endpoint returns is discarded rather
  than half-understood.
* **NOT AVAILABLE FROM THIS DATA SOURCE**: anything per-wallet.
* **Note**: `funding` here is the current funding *rate* for the asset. It is not
  a wallet's paid funding — that is `cumFunding` in §7. The two are never mixed.

## 7. REST `clearinghouseState` — the only source of position truth (weight 2)

* **Request**: `{"type": "clearinghouseState", "user": "<address>"}`.
* **Purpose**: the wallet's live perp positions and account margin. Every
  position-shaped field in an alert comes from here.
* **Implementation**: `HyperliquidREST.clearinghouse_state` →
  `parser.parse_clearinghouse_state` → `WhaleTracker` → `WhaleDetector`.
* **Fields available**:
  * `marginSummary`: `accountValue`, `totalNtlPos`, `totalMarginUsed`;
    `withdrawable`; `time`.
  * per `assetPositions[].position`: `coin`, `szi` (signed size — the sign is the
    direction), `entryPx`, `positionValue`, `unrealizedPnl`, `returnOnEquity`,
    **`liquidationPx`**, **`marginUsed`**, `maxLeverage`,
    `leverage: {type: cross|isolated, value}`, `cumFunding: {sinceOpen, …}`.
* **Verified**: liquidation price **is** available here — but Hyperliquid returns
  it as `null` for some cross positions. When it is absent the alert prints
  `Liquidation: N/A`; it is never computed and never guessed.
* **Verified**: margin (`marginUsed`) and position notional (`positionValue`) are
  **different numbers** and are reported separately. Trade value (`px × sz` from
  §1) is a third, unrelated number. The alert labels each for what it is.
* **Fields NOT AVAILABLE FROM THIS DATA SOURCE**: TP, SL (see §8), the wallet's
  resting orders, when the position was actually opened, and any position history.
* **`positions with szi == 0` are dropped** by the parser — a zero position is
  not a position.
* **Position-open time**: `NOT AVAILABLE FROM THIS DATA SOURCE`. What the bot
  shows as `Observed:` is DERIVED — time since *this monitor* first saw the
  position — and says so in the message text.

## 8. REST `frontendOpenOrders` — the only source of real TP / SL (weight 20)

* **Request**: `{"type": "frontendOpenOrders", "user": "<address>"}`.
* **Purpose**: the wallet's resting orders **including trigger metadata**. This is
  the only verified source of trader-set take-profit and stop-loss levels.
* **Implementation**: `HyperliquidREST.frontend_open_orders` →
  `parser.parse_open_orders` → `detector.extract_tpsl` → detector.
* **Fields available**: `coin`, `oid`, `side`, `limitPx`, `sz`, `origSz`,
  `timestamp`, **`orderType`** (e.g. `"Take Profit Market"`, `"Stop Limit"`,
  `"Limit"`), **`reduceOnly`**, **`isTrigger`**, **`triggerPx`**,
  `triggerCondition`, **`isPositionTpsl`**, `tif`, `cloid`, `children`.
* **TP/SL classification**: by `orderType` only —
  `constants.TAKE_PROFIT_TYPES` / `STOP_LOSS_TYPES` /
  `constants.classify_trigger`. A far-away limit order is **not** a TP or SL.
* **Three distinct states the bot must not blur** (spec §34):
  1. this wallet was fetched and has a TP → show the price (CONFIRMED);
  2. this wallet was fetched and has none → `TP: N/A`;
  3. this wallet was **not** fetched (weight budget, or the request failed) →
     `TP: N/A (not checked)`.
  Tested in `tests/test_engine_pipeline.py`.
* **Fields NOT AVAILABLE FROM THIS DATA SOURCE**: TP/SL for any wallet not
  individually queried. There is no bulk or global variant of this request, so
  TP/SL **cannot** be reported for the whole market — only for wallets the budget
  allowed us to check.
* **Cost note**: weight 20 vs 2 for a position fetch, which is why
  `WhaleEngine._enrich` only spends it when the remaining budget is comfortable.

## 9. REST `openOrders` — resting orders without trigger metadata (weight 20)

* **Request**: `{"type": "openOrders", "user": "<address>"}`.
* **Available**: `coin`, `oid`, `side`, `limitPx`, `sz`, `origSz`, `timestamp`.
* **NOT AVAILABLE FROM THIS DATA SOURCE**: `orderType`, `reduceOnly`,
  `isTrigger`, `triggerPx`, `isPositionTpsl` — i.e. **no TP/SL detection**.
  Implemented (`HyperliquidREST.open_orders`) but deliberately not the primary
  path; §8 supersedes it at the same weight.

## 10. REST `orderStatus` — cancel vs fill (weight 2)

* **Request**: `{"type": "orderStatus", "user": "<address>", "oid": <int>}`.
* **Purpose**: when a tracked order *disappears*, this is what distinguishes
  "cancelled by the trader" from "filled" from the many exchange-initiated
  cancels. Without it the bot would be guessing.
* **Implementation**: `WhaleEngine._resolve_order_status`; labels from
  `constants.status_label`.
* **Response shape**: `{"status": "order"|"unknownOid", "order": {"order": {...},
  "status": "<one of ALL_ORDER_STATUSES>", "statusTimestamp": ms}}`.
* **Limitation**: returns `unknownOid` for orders outside its retention window.
  When the status cannot be resolved the alert says so
  (`⚠️ <reason>` line) instead of asserting a cause.

## 11. REST `userFills` (weight 20) and `allMids` / `l2Book` REST variants

* `userFills` — `{"type": "userFills", "user": …}`; same `Fill` shape as §5,
  used for backfill. Carries `liquidation` and `closedPnl`.
* `allMids` (weight 2) and `l2Book` (weight 2) exist as REST fallbacks with the
  same fields and the same limitations as §2 / §3.
* `candleSnapshot` (weight 20) — implemented in `HyperliquidREST.candle_snapshot`.
  Supported intervals are exactly `constants.CANDLE_INTERVALS`
  (`1m 3m 5m 15m 30m 1h 2h 4h 8h 12h 1d 3d 1w 1M`).
  **`2m`, `4m`, `10m`, `20m` candles do NOT exist.** The bot's 2M/3M/4M/5M/10M/
  20M/30M/1H/4H selectors are therefore **event/position observation windows**
  (`constants.MONITOR_WINDOWS`) and are never labelled as candle timeframes.

---

## 12. Consolidated field availability

| Field an alert may show | Source | Availability |
|---|---|---|
| Coin, price, size, trade value | WS `trades` | CONFIRMED |
| Wallet address of a large trade | WS `trades` (`users`) | CONFIRMED (may be absent → `Trader: N/A`) |
| Taker vs maker side | WS `trades` (`side`) | CONFIRMED |
| Position side / size / notional | REST `clearinghouseState` | CONFIRMED when the wallet was enriched |
| Entry price | REST `clearinghouseState` | CONFIRMED |
| Leverage + cross/isolated | REST `clearinghouseState` | CONFIRMED |
| Margin used | REST `clearinghouseState` | CONFIRMED |
| Unrealised PnL | REST `clearinghouseState` | CONFIRMED |
| Liquidation price | REST `clearinghouseState` | CONFIRMED when present; `null` for some cross positions → `N/A`. Never computed |
| Take profit / stop loss | REST `frontendOpenOrders` | CONFIRMED only for individually-fetched wallets; otherwise `N/A` / `N/A (not checked)` |
| Resting limit order + trigger metadata | REST `frontendOpenOrders` | CONFIRMED |
| Order placed / modified / filled / cancelled / rejected | WS `orderUpdates` + REST `orderStatus` | CONFIRMED for the ≤10 focus wallets only |
| Liquidation of a wallet | WS `userEvents` fill with `liquidation` | CONFIRMED for the ≤10 focus wallets only. **No global feed** |
| Aggregate resting size at a price | WS/REST `l2Book` | CONFIRMED as an aggregate; wallet attribution `NOT AVAILABLE` |
| Distance from current price | derived from `allMids` + level price | DERIVED (marked) |
| Time the position was opened on-chain | — | **NOT AVAILABLE FROM ANY PUBLIC SOURCE.** Shown as `Observed:` (DERIVED, since first seen by this monitor) |
| Real-world identity of a wallet | — | **NOT AVAILABLE.** No identity is ever claimed (spec §20) |
| Estimated capital / margin for a wallet not fetched | — | `NOT AVAILABLE FROM THIS DATA SOURCE` |
| Global list of all open positions | — | **NOT AVAILABLE.** Per-wallet only, budget-limited |
| Global list of all pending orders | — | **NOT AVAILABLE.** `l2Book` is aggregate; `frontendOpenOrders` is per-wallet |

## 13. Failure behaviour (verified by tests)

`HyperliquidREST.post_info` never raises. On failure it returns `None` and the
typed wrappers degrade to `None` / `{}` / `[]`, which the detector renders as
"unavailable" — never as a zero or a placeholder. `429` honours `Retry-After`;
`5xx` and unparseable bodies are retried with jittered exponential backoff; other
`4xx` are not retried. The websocket reconnects with the same backoff policy and
replays its full subscription set. Covered by `tests/test_resilience.py`
(50 tests), in particular
`test_failing_endpoints_return_empty_never_invented_values`.
