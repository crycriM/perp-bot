# Market-neutral perp MM deployment plan — Aster (hedge-mode accounts, crypto + stock arms)

Chosen architecture: **`dex_executor` (OPMS v1) + its native `AsterAdapter`**, driving the
unmodified `perp_bot.keeper.Keeper` / `mm_core` decision engine through
`perp_bot.OpmsClient` — REST `POST /api/v1/intents`, `GET /api/v1/positions/{exchange}/{symbol}`,
`GET /api/v1/accounts/{exchange}/{account_id}/equity`, WS `/ws/md/{exchange}/{symbol}` and
`/ws/fills/{exchange}`. Reason: the pinned Hummingbot checkout
(`/home/christian/sources/hummingbot`, v2.16.0, checked 2026-09-16) has **no
`aster_perpetual` connector**, so the HL plan's `hb-enhanced-opms` route is unavailable
for this venue, and per the solution-level AGENTS.md `dex_executor` remains the prod
seam for new venue/pair deploys until the HB migration is wired end-to-end. Recheck
upstream Hummingbot for an Aster connector at deploy time; if one appears, re-run the
seam decision before writing more adapter code.

Consequence: unlike the HL plan — which inherited leverage, order-type, and reconnect
handling from Hummingbot — **every venue-specific behavior this plan needs must be built
and proven in the Aster adapter itself** (§1.3, §7). The `AsterAdapter` that exists today
places plain GTC orders, reads single-signed positions, and has no leverage, margin-mode,
hedge-mode, post-only, or market-data surface.

This is the sibling of
[hl-market-neutral-mm-deployment-plan.md](hl-market-neutral-mm-deployment-plan.md) —
read §1.7 there first: Aster's hedge mode and cross-margin semantics were verified
against Aster's own docs there, and this plan builds on that verification instead of
repeating it. Venue-economics context (Aster MM rebates, productive collateral, hedge-mode
gross-margin caveat, RWA liquidity ranking) is in
[hyperliquid_aster_lighter_market_making_analysis_2026-09-08.md](hyperliquid_aster_lighter_market_making_analysis_2026-09-08.md) —
cited below as "the analysis memo".

---

## 1. Account topology & hedge-mode basket design

### 1.1 Hedge mode changes the topology problem

Hyperliquid's problem (HL plan §1.1) was **net** position mode: one signed position per
coin per subaccount, so holding both sides of a coin required two subaccounts and a
*correlated pair partner* to get per-account neutrality. Aster does not have that
constraint: hedge mode is confirmed to allow holding "both long and short positions at
the same time under the same contract" under one account, toggled per-account (One-Way vs
Hedge Mode in settings, and **the toggle cannot be switched while positions or orders are
open** — so it must be set at account setup, before any keeper starts). This is already
encoded in [venue_capabilities.py](../src/perp_bot/venue_capabilities.py)
(`aster -> position_mode="hedge"`), and `PerpPairConfig.position_mode` inherits it.

What the topology validator does **not** yet encode is the leg rule. Today
[topology.py](../src/perp_bot/topology.py) only rejects same-market duplicates on *net*
venues; for hedge venues it allows **any** number of same-`(exchange, coin, account_id)`
siblings. Two additions are needed (§1.3 item 3):

- the allowed shape is **at most one keeper per `(exchange, coin, account_id, leg)`**
  (leg = LONG/SHORT), so a LONG-leg and a SHORT-leg keeper pair on one account is fine —
  but two LONG-leg keepers on one account must be rejected exactly like a duplicate on a
  net venue, because they would double-write the same leg with independent, colliding
  intent streams;
- both legs of a coin must live on the **same** account to share cross margin and the
  same instrument instance — cross-account legs would just recreate HL's margin
  fragmentation.

### 1.2 Target structure — crypto arm and stock arm, one account each

| | `aster/crypto` (hedge mode, cross margin) | `aster/stock` (hedge mode, cross margin) |
|---|---|---|
| Instruments | deepest Aster crypto perps (e.g. BTC, ETH — pick by measured depth, not by habit; quote asset per `exchangeInfo`) | a small set of the most liquid Aster stock perps (exact symbols from `exchangeInfo` — see §1.7) |
| Per instrument | LONG-leg keeper, target `+q*`; SHORT-leg keeper, target `−q*`; both on this account | same |
| Net bias per instrument within the account | ≈ 0 **by construction** — the two legs are the same contract, so the offset is exact, not statistical | same |
| Combined | per-coin neutrality direct; no correlated-pair partner needed | same |

Three properties, and one honest cost:

- **Per-account neutrality is exact and per-instrument.** The HL basket paired correlated
  *different* coins (ETH vs SOL) on one subaccount, so neutrality was statistical and the
  residual was cross-coin basis risk. On Aster hedge mode the LONG and SHORT legs are
  the *same* instrument on the *same* account: a common move pays one leg and charges
  the other against the same cross-margin pool, tick for tick. There is no basis term
  between the legs.
- **Portfolio-level flatness per coin is trivial.** Both legs are on one account; there
  is no mirror subaccount to keep aligned, and no A↔B collateral true-up (HL plan §1.4)
  — only the much slower capital-allocation true-up between the two arms.
- **Coin selection is now a liquidity/edge decision, not a hedge-structure decision.**
  Don't pair BTC with ETH "for neutrality"; pair each coin with its own opposite leg.
  This is the main structural simplification vs HL §1.2.
- **Cost accepted** (analysis memo §13.3 decision rule): the account permanently carries
  `2|q*|` gross notional per coin, and — per Aster's margin docs as read in HL plan §1.7
  and analysis memo §6.4 — **hedge mode does not discount margin**: both legs charge full
  IM/MM. That trade buys per-coin tilt separation, a doubled quote surface (both legs
  quote), and clean attribution — the same trade the HL basket made, minus the
  correlated-pair basis risk.

Why two accounts (one per arm) instead of one: **asset-class risk isolation** — stock
perps gap on cash-market opens, earnings, and corporate actions, and that must not be
able to burn the crypto arm's collateral; plus independent leverage/margin settings per
arm, clean per-arm equity and PnL attribution (the OPMS equity endpoint is per
`account_id`), and a per-arm kill switch. The cost is margin fragmentation (analysis
memo §14): each pool carries its own buffer and cannot see the other's equity.

Sub-account mechanics: whether these two accounts are Aster-native sub-accounts under
one master or independent accounts is a deployment detail — each needs its own API
credential set (`ASTER_{ACCOUNT_ID}_API_KEY/_API_SECRET/_IS_TESTNET` per
[account-naming.md](account-naming.md)), its own margin pool, and its own hedge-mode
toggle. Verify Aster's actual sub-account support and key scoping against their docs
before creating them (§7).

### 1.3 Required `mm_core`/`perp_bot`/`dex_executor` extension: leg-aware execution — **Not implemented**

Status contrast with HL §1.3: the **structural tilt is done and reused as-is** —
`gueant_reservation_price(q_target=...)` in
[as_core.py](../../mm-core/src/mm_core/as_core.py), `target_inventory` in
[risk_policy.py](../../mm-core/src/mm_core/risk_policy.py)'s `RiskPolicy.evaluate`
(target-relative caps), threading through `Keeper._actuate`
([keeper.py](../src/perp_bot/keeper.py)) and `Backtest`. A LONG-leg keeper is just a
tilted keeper whose inventory lives in `[0, cap]` with `q* > 0`; a SHORT-leg keeper the
mirror. None of that math changes.

What is missing is the **leg dimension through the entire seam**. Everything below is
open; per perp-bot's AGENTS.md, write the tests first, one task per loop.

1. **Config** — `PerpPairConfig.position_side: str | None = None` (`"LONG"`/`"SHORT"`),
   required exactly when the venue's `position_mode == "hedge"`, forbidden otherwise
   (extend the existing `__post_init__` validation in
   [config.py](../src/perp_bot/config.py) that already checks `position_mode`).
2. **Intent contract** — `ExecIntent.position_side: str | None = None` in
   [contracts.py](../../mm-core/src/mm_core/contracts.py); `None` = net-mode
   leg-agnostic, so Hyperliquid/Lighter behavior is unchanged. A contract change
   triggers the solution-level AGENTS.md rule: check **both** OPMS implementations —
   `dex_executor` (router, positions, fills wire format) and `hb-enhanced-opms` (the
   controller must at least pass the field through, even though no Aster connector
   exists).
3. **Topology** — the §1.1 leg rule in
   [topology.py](../src/perp_bot/topology.py) (net-mode rule unchanged).
4. **Intent router — found while writing this plan, not fixed:**
   [intent_router.py](../../dex_executor/src/opms/service/intent_router.py) lines 62-75
   replaces the running `market_making` strategy per `(exchange, symbol, account_id)`.
   With two same-coin keepers on one Aster account, the SHORT-leg intent would **stop
   the LONG-leg's running MM strategy** on every quote refresh. The replace key, the
   emergency-stop scope, and `get_running_strategy_by_exchange_symbol` all need the leg.
5. **Positions API — found while writing this plan, not fixed:**
   [api.py](../../dex_executor/src/opms/service/api.py) `GET /positions/{exchange}/{symbol}`
   (lines 397-428) returns the **first** position row matching the symbol. In hedge mode
   the position-risk response has two rows per symbol (LONG and SHORT); each keeper would
   read whichever leg happened to be listed first. Add a `position_side` query parameter
   and response field; [opms_client.py](../src/perp_bot/opms_client.py) `get_positions`
   reads its own leg.
6. **Models & fills** — `Position.position_side` and `Order.position_side` on the OPMS
   models ([models/__init__.py](../../dex_executor/src/opms/models/__init__.py)), and the
   `/ws/fills/{exchange}` payload must carry the leg so each keeper's fill callback
   (`OpmsClient._fills_ws_loop`, which filters by symbol+account today) only sees its
   own leg's fills.
7. **`AsterAdapter`** ([aster.py](../../dex_executor/src/opms/adapters/aster.py)) —
   - `submit_order` (lines 101-150) has no `positionSide`: hedge-mode accounts must send
     it on every order. Reduce-only semantics in hedge mode need verification against
     Aster's API reference — Binance-style APIs generally reject `reduceOnly` combined
     with `positionSide`, because the leg itself enforces the no-flip property natively
     (a reducing order can zero the leg but never open the opposite one). Verify, then
     encode.
   - Post-only: `timeInForce` is hardcoded `"GTC"` (line 116). Add the maker-only
     variant (Binance-style `GTX` or Aster's documented equivalent) and prove it
     maker-only empirically the way HL's `Alo` was proven (HL plan §7) — a passive order
     rests, a crossing one is rejected without filling.
   - `get_positions` (lines 207-231) derives side from the sign of `positionAmt` and
     skips zero rows. On Binance-style hedge-mode responses (two rows per symbol, signed
     within the leg) this happens to produce per-leg records — but the leg identity is
     implicit and a flat leg vanishes. Make it explicit: one `Position` per leg with
     `position_side`, flat legs reported as zero rather than omitted.
   - **Provisioning calls do not exist**: per-symbol leverage, cross-vs-isolated margin
     type, and a hedge-mode state probe. Startup must apply the configured leverage per
     symbol and **fail closed if the account is not in hedge mode** (the toggle cannot
     be flipped with open positions, so a wrong-mode account must never start quoting).
   - **Market data does not exist**: `MDPublisher` calls `adapter.get_l1_data()` and
     optionally `get_funding_rate()` (md_publisher.py lines 88-98) — `AsterAdapter` has
     neither, so `/ws/md/aster/...` cannot serve the keeper until they are added
     (venue WS preferred; REST `bookTicker` polling must be rate-budgeted, §3).
   - Testnet: `testnet=True` without a custom `base_url` **silently uses mainnet**
     (lines 89-93 log a warning). Obtain the current demo/testnet base URL from
     Aster's docs; don't guess one (§7).
   - Filters: read `exchangeInfo` per symbol (tick size, step size, min notional, max
     leverage bracket) and enforce at quote sizing — the HL soak learned min-notional
     rejection the hard way (HL plan §7.1).
8. **Keeper** — no math changes; `Keeper._actuate` threads `position_side` into every
   `ExecIntent` it emits. `EMERGENCY_EXIT` still targets 0.0 — but **for its own leg
   only**: flatten MY leg, leave the sibling leg alone (the router's emergency path must
   likewise stop only the same leg's strategies, §1.3 item 4).
9. **Rebalancer** — `PositionProvider = (account_id, coin) -> (signed, equity)`
   ([rebalancer.py](../src/perp_bot/rebalancer.py) lines 28-30) is leg-blind: with two
   legs on one account it would read one ambiguous position per coin. Extend the
   provider to `(account_id, coin, position_side)`; per-coin portfolio net becomes the
   sign-correct sum over legs; per-subaccount imbalance sums per-leg drift; the rebalance
   intent carries `position_side`. The leverage-aware capacity guard
   (`max_gross_notional_multiple`, 2.5x gross at the reference leverage) is reused
   as-is — but note hedge mode **doubles target gross per coin** (two ±q* legs), so the
   guard now sees `2|q*|` per coin in its projected initial margin.
10. **Feeds** — [rebalancer_feeds.py](../src/perp_bot/rebalancer_feeds.py) is
    HL-specific (an `Info`-duck-typed SDK). Add `aster_rebalancer_feeds.py` binding the
    same provider interfaces to the Aster REST client (per-leg position rows, equity
    from balance, mids from ticker).
11. **Symbol / quote asset** — `OpmsClient` hardcodes `self.symbol = f"{coin}-USD"`
    (opms_client.py line 45), and the adapter's standard form is `BASE/QUOTE:SETTLE`
    ([symbol_mapper.py](../../dex_executor/src/opms/adapters/symbol_mapper.py)). Aster's
    crypto perps are predominantly USDT-quoted (USDC variants exist — see the adapter's
    `SYMBOL_SUFFIXES`), and the stock arm's quote asset is TBD from `exchangeInfo`. Make
    the symbol format per-config, pin **one quote asset per arm**, and hold that arm's
    collateral in the same asset.
12. **Funding cadence** — `PerpPairConfig.funding_interval_s = 3600.0` is HL's cadence.
    Aster's is per-market (Binance-compatible venues commonly run 8h with shorter
    intervals on selected markets). Read per symbol and set per config; the
    `PnLLedger`'s pro-rata accrual then works unchanged.

### 1.4 Collateral allocation

Aster uses **cross margin by default** per account (shared balance across all open
positions, PnL nets; or isolated per position) — same family as HL §1.4, verified in HL
plan §1.7. The same caveat applies, and it is *stronger* for hedge mode: **cross margin
nets PnL, not the margin requirement** — and hedge-mode long+short legs are offsetting
positions whose maintenance margin is still charged **gross on both legs** (analysis
memo §6.4). Concretely:

- Worst case (every leg at its cap, nothing netted at the venue):
  $$MM_{\text{sub}} = \sum_{\text{coin} \in \text{arm}} \sum_{\text{leg} \in \{L,S\}} \frac{\text{max\_position}_{\text{coin}} \times \text{price}_{\text{coin}}}{L_{\text{coin}}}$$
  where $L_{\text{coin}}$ is the **leverage-tier max at that notional** from Aster's
  bracket table per symbol — pull it from the venue, don't assume a flat max. For the
  stock arm expect materially lower brackets than crypto; verify per symbol before
  sizing anything.
- Residual risk is **leg-imbalance risk**, not the HL basket's cross-coin basis risk —
  same-instrument legs carry no basis between them. The imbalance the caps allow is the
  exposure; size the buffer from the coin's own vol and the rebalance horizon:
  $$\text{imbalance\_buffer} = k_\sigma \times \sigma_{\text{coin}} \times \sqrt{T_{\text{rebalance}}} \times \text{cap\_gap\_notional}$$
  with `cap_gap_notional` = the notional distance from target at which the caps/de-risk
  gates sit. For the **stock arm add the gap move**: while the cash market is closed, a
  name can re-mark through the next open (analysis memo §21 sizes single-stock earnings
  gaps at 10-20% — that is the design case). If a stock's gap distribution breaches the
  buffer at its caps, the caps are too big for that name; shrink them or drop the name.
- Total per account: $E_{\text{sub}} = MM_{\text{sub}} + \text{imbalance\_buffer} +
  \text{operational\_buffer}$ (funding, fees, slippage — 10-20% of $MM_{\text{sub}}$ is
  a reasonable start). Funding on **balanced** legs nets to ≈ 0 within the instrument
  (long pays short), so funding is a second-order buffer item here — but a persistently
  imbalanced book pays/receives like a directional one; treat persistent funding
  asymmetry as a drift signal (§6).
- **Productive collateral: not for v1.** Aster supports yield-bearing collateral
  (illustrative collateral ratios per the analysis memo §6.5: USDF ~99.99%, asBNB
  ~95%, ASTER ~80%). Execution margin stays plain USDT/USDC: USDF adds issuer,
  custody, and backing-strategy risk (memo §6.6), asBNB adds BNB delta plus wrapper
  risk (§6.7, §19 wrong-way risk). Revisit only after the arm is proven, as a separate
  hedged-carry decision — not as a default.
- **Fund the two accounts independently.** There is no mirror pair, so no HL-style
  A↔B symmetry to maintain — but each account's cross pool sees only its own balance
  (analysis memo §14: global delta neutrality is not local margin safety), so each arm
  must survive its own worst local mark on its own collateral. Re-allocate between arms
  on a slow cadence based on per-arm utilization, not on demand during stress.
- **Incentives never change sizing.** Aster's qualified-MM program (memo §6.2: maker
  rebates −0.25/−0.50 bp at MM1/MM2, ASTER reward pool) is a *revenue* input with a
  haircut — qualification can be lost, programs are discretionary (memo's own warning).
  Collateral is sized assuming zero rebate.

### 1.5 Netting / rebalancing controller — **Reused, leg-aware feeds open**

Reuse `perp_bot.rebalancer.BasketRebalancer` (the HL plan §1.5 v1 standalone periodic
job, its 7 tests, and the leverage-aware capacity guard) with the leg extensions from
§1.3 item 9:

1. **Metric**, per account, every 5-15 minutes:
   $$\text{imbalance}_{\text{sub}} = \sum_{\text{coin,leg}} \big(\text{position}_{\text{coin,leg}} - q^*_{\text{coin,leg}}\big) \times \text{price}_{\text{coin}}$$
   dollar-notional drift away from each leg's intended tilt.
2. **Trigger**: same defaults (18% of equity, 600 s cycle); plus the portfolio-level
   per-coin net across legs — that net is exactly "LONG leg ≠ |SHORT leg|" and is the
   quantity the whole design promises to keep near zero.
3. **Execution**: `passive_aggressive` unless the drift breaches the account's own
   `critical_position`/drawdown gates, then emergency urgency — leg-scoped in both cases.
4. **Logging**: the existing JSONL convention gains `position_side`.

v2 (folding into `Keeper` as a new `Decision`) stays deferred for the same reason as HL:
wait for live data on how often the natural leg drift needs correcting.

### 1.6 Generalizing beyond two arms

More coins per arm = more LONG/SHORT leg pairs on the same account — liquidity-gated,
not correlation-gated (§1.2). More asset classes = more accounts (a later commodity/FX
arm would repeat the stock arm's pattern). **Cross-venue hedging is explicitly out of
scope for v1** (Aster legs hedged on HL trade[XYZ]/Lighter, or Aster crypto vs HL
crypto): it reintroduces exactly the local-liquidation problem the analysis memo §14
describes — each venue must survive its own worst mark on its own collateral, with
transfer latency — and would need its own plan. The same-instrument leg design is what
makes the Aster book *internally* hedged without any of that.

### 1.7 Stock arm — additional structure the crypto arm doesn't need

1. **Symbol/market verification first.** Exact listed stock symbols, tick/step size, min
   notional, leverage brackets, funding interval, quote/settlement asset, and any
   per-market margin-mode restrictions come from Aster's `exchangeInfo` and product docs.
   Scale expectation from the analysis memo §2.2: Aster's RWA OI is ~$28M across ~108
   markets versus HL trade[XYZ]'s ~$3.7B — this arm is a **selective satellite**, not a
   capacity venue. Run a standing liquidity preflight per name (top-of-book depth at
   5/10/25 bps, median spread, realized daily turnover) and let names that fail sit out;
   prefer an index-like name or the single deepest stock first.
2. **Session gating.** Stock perps trade while the underlying cash market is closed —
   overnight/weekend books are thin and gap-prone, which is where markout goes to die.
   `mm_core`'s regime gate (`GateConfig`: half-life/Hurst) has no calendar concept.
   Add a session gate at the perp_bot layer (venue-agnostic policy object): default to
   STOP_QUOTING with the HL-soak flatten behavior (HL plan §7.1) outside cash-market
   hours unless the name passes a closed-session depth check; widen around the
   open/close auction windows. The exact policy is a §2 calibration output, not a
   constant.
3. **Corporate actions.** Dividends and splits adjust the venue's marks and sizes; the
   keeper's reconciled position can jump and entry prices shift under it. Policy:
   maintain a corporate-action calendar per name (earnings, ex-div, splits) — pause new
   quotes, resnapshot, re-derive target state, then resume. An earnings blackout window
   is mandatory for single names.
4. **Oracle/mark divergence.** The perp mark can diverge from the underlying reference
   while the cash market is closed (memo §17/§21 treats 2-5% single-stock venue-oracle
   basis as the stress case). Both legs of a pair see the same mark, so neutrality
   survives — but equity moves and the AS reservation price the keepers quote around
   moves with the mark. Treat a large mark-vs-reference divergence like the regime gate
   treats elevated volatility: widen or stop.
5. **Funding.** Stock-perp funding can embed borrow-like carrying costs and behave
   differently from crypto funding; read it per symbol. There is no inter-leg funding
   differential (same instrument), but an imbalanced leg pays/receives it in full.

### 1.8 Decision summary

| | Hyperliquid (HL plan) | Aster — crypto arm | Aster — stock arm |
|---|---|---|---|
| Position mode | net | hedge (confirmed, HL plan §1.7) | hedge (same account toggle) |
| Same-instrument long+short on one account | No → 2 subaccounts + correlated-pair partner | Yes → 1 account, 2 legs per coin | Yes |
| Structure for per-account neutrality | 2×2 cross-hedge basket (statistical, correlated pair) | per-coin LONG/SHORT legs (exact, same instrument) | same, plus session/corporate-action gating |
| Accounts needed | 2 per pair | 1 per arm | 1 per arm, separate from crypto |
| mm_core tilt extension | Implemented (HL §1.3) | Reused as-is | Reused as-is |
| Leg-aware seam extension | n/a | **Open — §1.3** | **Open — §1.3 + §1.7** |
| Margin model | cross, no discount | cross (default) or isolated; both legs charged gross, no hedge discount | same + lower leverage brackets, gap-sized buffer |
| Execution seam | hb-enhanced-opms + HB connector | dex_executor + `AsterAdapter` | same |

---

## 2. Parameter estimation — **Not calibrated** (Aster data, per symbol)

Nothing is calibrated yet. What it takes, mirroring HL §2:

1. **Data fetch** — `scripts/fetch_aster_data.py`: Binance-style klines REST per symbol,
   30 days of 15m candles plus funding history. Per the HL lesson
   (`fetch_hl_data.py` → `fetch_hl_data_v2.py`), prefer direct REST over SDK
   initialization paths.
2. **Calibrate per symbol on Aster's own data.** Do **not** reuse the HL-calibrated
   ETH/SOL parameters: AS `gamma`/`kappa` are scale- and flow-dependent (solution
   AGENTS.md), and Aster's depth/flow differs from HL's even for the same coin.
3. **Same gate checks** as HL §2: `net_edge_bps > 2`, `markout_ratio < 0.5`,
   `max_drawdown < 5%`, `liquidations = 0` (with the same caveat — the backtester
   hardcodes the last; it proves nothing about liquidations).
4. **Stock arm extras**: split the backtest into cash-open vs cash-closed sessions and
   score edge/markout separately for each name. If closed-session edge doesn't clear the
   gates, the §1.7 session gate must close the book overnight rather than quote a thin
   book. The synthetic candle-derived trades caveat from HL §2 applies, and is worse
   for stocks (overnight marks, discontinuous reference prices); if live diverges,
   replace with a WS trade-stream logger over the same window.

---

## 3. Order & inventory management

The HL §3 rules carry over per leg: nominal quote sizes 10%/5% of `max_position`; one
full fill stays inside the cap; the target-moving side cannot cross structural `q*`; the
reducing side cannot flip through flat. On Aster hedge mode the venue additionally
enforces the no-flip property **natively per leg** (a reducing order can zero the leg
but never open the other one), which is a second line of defense behind the keeper's
own clip — verify this empirically in the §5 demo gate before trusting it.

Both legs of a coin quote simultaneously on the same book: up to **four resting orders
per symbol** (two per leg), refreshed at ≥ 5 s cadence. Budget Aster's Binance-style
request weights (order placement, cancels, position polls, L1 refresh) per symbol before
adding names, and prefer a venue WS for L1 over REST polling once the adapter gains it.
The portfolio netting job (§1.5) remains the slower outer loop on top of each keeper's
own `RiskPolicy`.

---

## 4. Wiring / config — **Basket config to be written** (`scripts/aster_basket_config.py`)

Mirrors `scripts/basket_config.py` (HL) — two legs per coin instead of two subaccounts
per pair, all parameters from the §2 calibration:

```python
# Crypto arm, one account, hedge mode. Per coin: LONG leg (+q*) + SHORT leg (-q*).
# Example shown for one coin; parameters are placeholders until §2 calibration.
PerpPairConfig(
    coin="BTC",
    exchange="aster",              # venue_capabilities: position_mode="hedge"
    account_id="crypto",           # -> ASTER_CRYPTO_API_KEY / _API_SECRET / _IS_TESTNET
    position_side="LONG",          # §1.3 item 1; the sibling instance uses "SHORT"
    target_inventory=+q_star,      # SHORT leg: -q_star  (same |q*|, mirrored)
    leverage=<from Aster bracket>, # applied at startup by the adapter (§1.3 item 7)
    funding_interval_s=<per market>,
    caps=Caps(max_position=..., critical_position=...),
)
```

- `validate_account_topology` (after its §1.3 leg rule) must pass over the **full set
  of both arms** before any keeper starts — including the stock arm's configs.
- Credentials per [account-naming.md](account-naming.md):
  `ASTER_CRYPTO_API_KEY/_API_SECRET/_IS_TESTNET`, `ASTER_STOCK_API_KEY/_API_SECRET/_IS_TESTNET`
  in `dex_executor/.env` (solution AGENTS.md: credentials live in the exec layer, not
  the bots).
- Hedge mode is toggled **once at account setup** (it cannot be flipped with open
  positions/orders); the adapter's startup probe then verifies it every launch and
  fails closed (§1.3 item 7).
- Per-arm quote asset pinned per §1.3 item 11; symbols threaded consistently through
  `PerpPairConfig.coin`, `OpmsClient`, the adapter, and the `/ws/fills` filter.

---

## 5. Rollout sequence

Backtest → shadow → demo gate → micro-mainnet → scale, per the general plan
(`perp-bot/README.md` step 4), with these Aster-specific additions:

1. **Backtest each leg individually** (does tilted AS quoting behave sanely at ±q*?),
   then the rebalancer against **both legs' simulated positions** (does the per-coin
   net-across-legs actually stay bounded?).
2. **Read-only shadow** against the real Aster accounts (positions, equity, mids via
   `aster_rebalancer_feeds`) — no orders — mirroring the HL
   `run_rebalancer_shadow.py` pattern. Confirm the capacity guard reads the doubled
   hedge-mode gross correctly.
3. **Demo/testnet write gate** (obtain the demo base URL + keys first, §7): quote →
   rest → refresh → cancel → clean stop; one real fill; **leg-scoped** de-risk to flat;
   **emergency flatten of one leg leaving the sibling leg untouched** — that last check
   doubles as the end-to-end proof that the leg discriminator is wired everywhere
   (§1.3 items 4-8).
4. **Micro-mainnet on the crypto arm first**, at the smallest workable size; validate
   that the rebalancer fires and corrects a seeded leg imbalance within one full
   monitoring cycle before increasing size.
5. **Import the HL soak's lessons before any live run** (HL plan §7.1): quote-stop must
   cancel *and flatten* residual leg inventory (one bounded reduce-only child);
   venue-order-id liveness watchdog; cancel-then-create quote refresh; request
   deadlines with 429-aware exponential backoff; SIGINT/SIGTERM teardown to flat.
   Several of those were implemented HB-controller-side and several on the native OPMS
   path — verify each exists on the `dex_executor`-Aster seam specifically (the adapter
   currently wires into none of them), and re-prove them in the demo gate.
6. **Stock arm follows the same ladder only after** its §1.7 gating and §2 calibration
   pass — starting with an index-like or deepest single name, closed-session quoting
   disabled from the start.

---

## 6. Monitoring & kill switch

In addition to the general plan's per-keeper monitoring:

- **Per-leg imbalance** and **per-coin net-across-legs** as first-class dashboard
  metrics; alert if either persists past the rebalancer's threshold for more than one
  cycle (a stuck rebalancer is its own failure mode — HL plan §6).
- **Funding per leg**: balanced legs should net ≈ 0; persistent asymmetry is an early
  drift signal.
- **Margin health**: map an Aster available-after-maintenance equivalent (the
  position/balance endpoints expose maintenance-margin fields) into the existing
  fail-closed invariant — missing, failed, malformed, or non-finite reads are a
  **zero-margin hard breach** forcing `EMERGENCY_EXIT` (`perp_bot.margin_health`
  / `RiskPolicy` behavior), never a skip.
- **Stock arm**: session state (open/closed/blackout), perp-mark-vs-underlying-reference
  divergence, corporate-action calendar alerts, and per-name closed-session depth.

---

## 7. Known gaps to close before real capital

- **Leg-aware seam (§1.3)** — **Open, and the critical path**: `position_side` on
  `PerpPairConfig`, `ExecIntent`, OPMS `Order`/`Position`, positions API, fills WS,
  intent router, topology rule, rebalancer providers, keeper intent emission. Two
  concrete defects found while writing this plan and not yet fixed: the router's
  replace-per-coin stop (§1.3 item 4) and the first-match positions endpoint (§1.3
  item 5). TDD-first per perp-bot AGENTS.md.
- **`ExecIntent` contract change** — per solution AGENTS.md, check `hb-enhanced-opms`
  passes the new field through even without an Aster connector.
- **Aster adapter capabilities** — **Open**: `positionSide` on orders; hedge-mode-aware
  reduce semantics (verify against Aster's API reference); post-only (currently
  hardcoded GTC) with a maker-only empirical proof; leverage/margin-type application and
  hedge-mode startup probe; `get_l1_data`/`get_funding_rate` for `/ws/md`; per-symbol
  `exchangeInfo` filters enforced at quote sizing; testnet base URL handling (today
  `testnet=True` without `base_url` silently uses mainnet).
- **Demo/testnet environment** — **Open**: obtain the current demo/testnet base URL and
  a dedicated demo account's API keys; the HL deployment had no HL testnet credentials
  and had to run gates on mainnet at micro size — avoid repeating that if Aster's demo
  environment exists and is usable.
- **Data fetch + calibration (§2)** — **Open**: `fetch_aster_data.py`, per-symbol
  gamma/kappa grid, session-split scoring for the stock arm.
- **Stock symbol verification + liquidity preflight (§1.7)** — **Open**: exact symbols,
  filters, brackets, funding intervals, quote assets; per-name depth/turnover gates
  before any name is configured.
- **Session gating for the stock arm (§1.7)** — **Open**: calendar/depth gate in
  perp_bot; corporate-action pause policy.
- **Margin-health source for Aster** — **Open**: identify and map the venue's
  maintenance-margin/available fields into `margin_available` with the fail-closed
  invariant; verified read-only on demo before the first live run.
- **Sub-account mechanics on Aster** — **Verify**: multiple accounts per master with
  isolated margin pools and per-account hedge-mode toggles; key scoping (can one API key
  touch both accounts — if yes, split keys before running both arms concurrently).
- **Rate-limit budget** — **Open**: Binance-style request weights for the full per-symbol
  cadence (4 orders refresh + L1 + position polls), WS preferred once available; wire
  the adapter into the existing OPMS resilience/timeout machinery and prove 429/timeout
  behavior with injected faults, then live.
- **No Hummingbot Aster connector** — standing constraint of this plan; recheck
  upstream before any seam migration.
- **Incentives** — apply haircuts per the analysis memo §6.2; never capitalize
  qualification-dependent rebates in sizing or collateral.
- **From the HL plan's gap list, per-instance here too**: leverage tier selection per
  symbol (don't assume flat max leverage), the backtester's hardcoded
  `liquidations = 0` gate, and synthetic candle-derived backtest trades. HL §7's
  venue-agnostic items (quote-stop flatten, liveness watchdog, deadlines/429) must
  exist on the native Aster seam before the first live soak, not just on HB.

The first concrete step is the §1.3 leg seam, tests first, in `perp-bot` and
`dex_executor` together; everything else (adapter capabilities, demo environment, data
fetch, calibration) can proceed in parallel behind it. No Aster order should be sent
through this stack until the demo gate in §5 step 3 passes end-to-end, including the
leg-isolation emergency check.
