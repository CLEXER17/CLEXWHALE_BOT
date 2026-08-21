"""Throwaway live probe: confirms the real data path end-to-end.

trades WS (buyer/seller) -> clearinghouseState (position, liq px, leverage)
                         -> frontendOpenOrders (real TP/SL trigger orders)
"""
import asyncio, json, sys

sys.path.insert(0, ".")
from app.hyperliquid.parser import (
    parse_clearinghouse_state, parse_open_orders, parse_trades,
)
from app.hyperliquid.rest import HyperliquidREST
from app.utils.ratelimit import WeightedRateLimiter
from websockets.asyncio.client import connect


async def main():
    biggest = None
    raw_sample = None
    async with connect("wss://api.hyperliquid.xyz/ws") as ws:
        for coin in ("BTC", "ETH", "SOL"):
            await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}))
        deadline = asyncio.get_event_loop().time() + 25
        count = 0
        while asyncio.get_event_loop().time() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            except asyncio.TimeoutError:
                break
            if msg.get("channel") != "trades":
                print("CHANNEL:", msg.get("channel"), str(msg.get("data"))[:160])
                continue
            if raw_sample is None:
                raw_sample = (msg["data"] or [None])[0]
            for t in parse_trades(msg["data"]):
                count += 1
                if biggest is None or t.notional > biggest.notional:
                    biggest = t
        print(f"\ntrades parsed: {count}")
        print("RAW TRADE SAMPLE:", json.dumps(raw_sample))

    if biggest is None:
        print("no trades captured")
        return
    print(f"\nBIGGEST: {biggest.coin} {biggest.taker_side} px={biggest.px} sz={biggest.sz} "
          f"ntl=${biggest.notional:,.0f}\n  buyer={biggest.buyer}\n  seller={biggest.seller}\n"
          f"  taker={biggest.taker}  time={biggest.time}")

    limiter = WeightedRateLimiter(600)
    async with HyperliquidREST("https://api.hyperliquid.xyz", limiter) as rest:
        for label, addr in (("buyer", biggest.buyer), ("seller", biggest.seller)):
            if not addr:
                continue
            raw = await rest.post_info("clearinghouseState", {"user": addr})
            state = parse_clearinghouse_state(addr, raw)
            print(f"\n--- {label} {addr} accountValue={state.account_value} positions={len(state.positions)}")
            for coin, p in list(state.positions.items())[:4]:
                print(f"    {coin:8s} {p.side:5s} szi={p.szi} entry={p.entry_px} "
                      f"posValue={p.position_value} liqPx={p.liquidation_px} "
                      f"lev={p.leverage_value}{p.leverage_type} margin={p.margin_used}")
            orders_raw = await rest.post_info("frontendOpenOrders", {"user": addr})
            orders = parse_open_orders(orders_raw)
            trig = [o for o in orders if o.is_trigger]
            print(f"    open orders={len(orders)} trigger(TP/SL)={len(trig)}")
            for o in orders[:3]:
                print(f"      oid={o.oid} {o.coin} {o.direction} limitPx={o.limit_px} sz={o.sz} "
                      f"type={o.order_type} trig={o.is_trigger} trigPx={o.trigger_px} "
                      f"cond={o.trigger_condition} posTpsl={o.is_position_tpsl} ro={o.reduce_only} "
                      f"kind={o.trigger_kind} ntl=${o.notional:,.0f}")
            if orders_raw:
                print("    RAW ORDER SAMPLE:", json.dumps(orders_raw[0]))
        book = await rest.l2_book("BTC")
        print(f"\nBTC book: {len(book.bids)} bids / {len(book.asks)} asks; "
              f"top bid {book.bids[0].px} sz {book.bids[0].sz} n={book.bids[0].n}")
        metas, ctxs = await rest.meta_and_asset_ctxs()
        print(f"perp universe: {len(metas)} assets; BTC mark={ctxs['BTC'].mark_px} "
              f"vol24h=${ctxs['BTC'].day_ntl_vlm:,.0f}")
        top = sorted(ctxs.values(), key=lambda c: c.day_ntl_vlm or 0, reverse=True)[:8]
        print("top by 24h notional:", [c.coin for c in top])
        print("rest stats:", rest.stats())


asyncio.run(main())
