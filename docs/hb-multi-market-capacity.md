# Hummingbot capacity for multi-market perp MM (HL + Lighter + Aster)

**Date:** 2026-09-15 · **Status:** analysis only, not measured under load.

Scenario: AMM-style quoting on 10 perps on Hyperliquid (long/short legs split
across 2 subaccounts per the basket design) plus 10 perps across Lighter and
Aster. Question: can Hummingbot (`hb-enhanced-opms` + `PerpMMController`) hold it?

**Short answer:** the HB process is probably not the bottleneck. Hyperliquid's
rate limits are, because the controller cancels and replaces every quote on
every 5 s cycle. Aster can't run on HB at all today.

## 1. Topology the scenario implies

- **One account per connector name per HB instance.** Credentials are one file
  per connector (`conf/connectors/hyperliquid_perpetual.yml`; last import
  wins), `ConnectorManager.connectors` is keyed by name, and controllers, the
  markets dict and the trade DB all key on the connector name. Different
  exchanges can share an instance; two accounts on the same exchange cannot.
  Two instances from one checkout share `conf/`, so each instance needs its own
  conf directory (second checkout or one container per bot).
- **Hyperliquid:** 10 coins × {long-tilt on sub A, short-tilt on sub B} = 20
  controllers, 10 per subaccount → **≥ 2 HB instances**.
- **Aster:** **no Aster connector** in the pinned HB checkout (v2.16.0). Stays on
  `dex_executor`, as Phase 2 plan §8.2 already assumes.
- **Lighter:** `lighter_perpetual` connector exists (untested here). Lighter is
  net mode (§1.7 of the deployment plan), so separate long/short legs need 2
  Lighter accounts, not 1. Lighter connectors can share an instance with an HL
  subaccount.

## 2. Hyperliquid limits (HL docs, rate-limits-and-user-limits)

- IP: 1,200 weight/min. Exchange actions weight 1 (+1 per 40 in a batch).
  `l2Book`, `clearinghouseState`, `orderStatus`, `spotClearinghouseState`
  weight 2; most other info requests (e.g. `userAbstraction`, `userFills`) 20.
- Address (per user; **subaccounts count separately**): 10,000 initial
  requests + 1 request per 1 USDC traded (cumulative); when exhausted, 1 request
  per 10 s. Cancels: separate allowance `min(limit + 100000, 2 × limit)`.
  Batched n orders = 1 request for IP limits, n for address limits.
- Open orders: 1,000 (+1 per $5M volume, max 5,000).
- WebSocket: 10 connections, 1,000 subscriptions, 10 unique users, 2,000
  messages/min per IP.

## 3. Load per HL subaccount (10 controllers, 5 s cycle, current behaviour)

| Budget | Usage | Limit |
|---|---|---|
| IP weight (both HL instances on one host share it) | Orders + cancels: 2 × 240 = 480/min per sub → 960/min for two. Connector polling per instance (every 5–12 s): clearinghouseState ×2, spotClearinghouseState, userAbstraction (20), userFills (20+), orderStatus (2) per open order (~20) ≈ 86 per poll → ~400–1,000/min | 1,200/min |
| Address budget | 240 orders/min = 14,400/h | 10,000 once, then 1 per $1 traded |

Consequences:
- **IP limit exceeded** (~1,800–2,900/min vs 1,200). HB's HL throttler counts
  every request as weight 1 against 1,200, so it won't back off before HL
  rejects.
- **Address budget is the killer.** Initial allowance lasts ~40 min; staying
  unthrottled needs ~$14k traded/hour (~$345k/day) per subaccount. Once
  throttled, *de-risk and emergency orders* also get 1 request / 10 s (only
  cancels have the larger allowance).
- Each quote replacement is a new executor persisted to HB's SQLite DB
  (~14,400 records/hour per instance at this rate).

## 4. Problems that grow with controller count

- **Nonce collisions.** HB's HL auth uses `int(time.time() * 1000)` as the
  nonce with no uniqueness guard. Ten controllers firing on the same cycle
  boundary can sign two actions in one millisecond; HL rejects the duplicate.
  Worse across instances sharing an agent key (all `e2_*` accounts share one
  today).
- **Risk is per controller, margin is per account.** All controllers on a
  subaccount read the same cross-margin equity, so one coin's loss trips every
  controller's drawdown stop at once, and none sees aggregate exposure.

## 5. HB process itself

Not measured. One asyncio loop per instance; per instance ~10 WS order books,
10 keeper ticks per 5 s (O(200) regime math), and the executors. HB commonly
runs this many pairs. Measure with a 10-controller shadow session on `e2_mm1`
(`deploy/run_shadow_session.sh`; shadow mode creates no executors, so it
measures the read side only).

## 6. What would make it viable

1. **Requote policy in `PerpMMController`:** replace a quote only when the
   target price moves beyond a tolerance or the quote exceeds a max age; use HL
   `modify` and batching where possible. Controller code, not an HB change.
2. **Size the order rate from expected volume.** Even 1 replacement per side
   per minute on 10 coins ≈ 1,200 orders/h ≈ $29k/day traded per subaccount to
   stay unthrottled. The request budget is earned by volume.
3. **Account-level supervisor per subaccount:** aggregate drawdown, remaining
   address budget, kill switch.
4. **One agent key per subaccount**, plus a nonce uniqueness guard.
5. **Aster:** keep on `dex_executor` or build/port an HB connector.
6. **Lighter:** decide 1 vs 2 accounts; check Lighter's own rate limits
   (not verified).
