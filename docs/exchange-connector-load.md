The Hummingbot process itself is probably not the bottleneck. Hyperliquid's rate limits are, and they break first because our controller cancels and replaces every quote on every 5 s cycle. Separately, HB can't host your Aster leg at all.

**The setup your plan implies**
- **Hyperliquid:** 10 coins, each with a long-tilted controller on sub A and a short-tilted one on sub B. That's 20 controllers, 10 per subaccount, so at least 2 HB instances (one Hyperliquid account per instance).
- **Aster:** this Hummingbot checkout has no Aster connector (I checked both connector folders). Aster stays on `dex_executor`, as the Phase 2 plan already assumes.
- **Lighter:** a `lighter_perpetual` connector exists, but I haven't tested it. Lighter is net mode like Hyperliquid, so one account can't hold separate long and short legs. You'd need 2 accounts there too, or give up long/short separation on Lighter. Lighter connectors can share an instance with a Hyperliquid subaccount.

**Hyperliquid load per subaccount** (10 controllers, 5 s cycle, current cancel/replace-every-cycle behaviour; limits from HL's rate-limit docs)

| Limit | What we'd use | HL limit |
|---|---|---|
| IP weight (both instances on one host share it) | Orders and cancels: 960/min. Connector polling: about 400–1,000/min per instance (per-order status checks are the biggest part) | 1,200/min |
| Address budget (each subaccount counts separately) | 240 orders/min = 14,400/hour | 10,000 starting allowance, then 1 request per $1 traded |

- **The IP limit is exceeded.** HB's throttler counts every Hyperliquid request as weight 1, but some polling calls cost 20. So it won't slow down before Hyperliquid starts rejecting.
- **The address budget is the real killer.** The starting allowance lasts about 40 minutes. After that, each subaccount must trade about $14k per hour ($345k/day) or be throttled to one request every 10 s. That throttle also applies to de-risk and emergency orders; only cancels get a larger separate allowance.

**Two more problems that grow with controller count**
- **Nonce collisions.** The connector uses the millisecond timestamp as the signing nonce, with no uniqueness guard. Ten controllers firing on the same cycle boundary can sign two orders in the same millisecond, and Hyperliquid rejects the duplicate. It gets worse if instances share an agent key.
- **Risk is per controller, but margin is per account.** All 10 controllers on a subaccount read the same account equity. One coin's loss trips all 10 drawdown stops at once, and no controller sees the combined exposure. The account needs its own supervisor.

**What would make it viable**
1. **Requote only when the price moves beyond a tolerance, or the quote gets too old.** Use Hyperliquid's order modify and batching where possible. This is controller code, not a Hummingbot change.
2. **Size the quoting rate from expected volume.** Even one replacement per side per minute across 10 coins is about 1,200 orders/hour, which needs about $29k/day traded per subaccount to stay unthrottled.
3. **Add an account-level supervisor** per subaccount: aggregate drawdown, remaining request budget, kill switch.
4. **Give each subaccount its own agent key**, and add a nonce guard.

I haven't measured HB's CPU. Hummingbot commonly runs 10 pairs per instance, but a 10-controller shadow session on `e2_mm1` would measure the order-book and keeper-tick load without placing orders; I can set that up once `HB_PASSWORD` is in `.env`. I also haven't checked Lighter's own rate limits.