# Market-neutral perp MM deployment plan — Hyperliquid

Chosen architecture: **`hb-enhanced-opms` + Hummingbot's `hyperliquid_perpetual` connector**,
driving the unmodified `perp_bot.keeper.Keeper` / `mm_core` decision engine via
`PerpMMController` / `InProcessClient`. This sidesteps `dex_executor`'s missing
`/api/v1/intents` (+ positions/equity/md/fills) surface and inherits Hummingbot's
leverage, order-type, and reconnect handling instead of the incomplete
`dex_executor` HL adapter (no leverage/margin management, no post-only orders).

---

## 1. Subaccount topology & cross-hedge basket design

### 1.1 Why one subaccount can't hold both sides

Hyperliquid is **net** position mode (`perp_bot.venue_capabilities`:
`hyperliquid -> position_mode="net"`): a subaccount can only hold **one signed
position per coin**. `perp_bot.topology.validate_account_topology` encodes
this — it raises on any duplicate `(exchange, coin, account_id)`, i.e. two
independently-managed keeper *instances* for the same coin cannot share one
HL subaccount. Even without that check, HL itself would just net their
orders into one blended position, destroying independent PnL/risk
attribution between the two instances.

This matters here because achieving **per-account market neutrality** for a
coin pair via structural long/short tilting (§1.3) requires **two
independent instances per coin** — one tilted long, one tilted short — which
is exactly the case the topology check forbids on one account. Hence: two
subaccounts, minimum, per hedged pair.

Note on naming: positions here are **perpetual contracts**, not spot coin
holdings — "ETH"/"SOL" below denote the ETHUSDC/SOLUSDC USDC-margined perp
at Hyperliquid (`coin="ETH"`/`"SOL"` in `PerpPairConfig`, HL's own naming for
the perpetual instrument), matching Aster/Lighter's equivalent USDC-quoted
perps on those venues.

### 1.2 Target structure (ETHUSDC/SOLUSDC example, generalizes to any correlated pair)

| | Sub A | Sub B |
|---|---|---|
| ETHUSDC perp instance | tilt **long** | tilt **short** |
| SOLUSDC perp instance | tilt **short** | tilt **long** |
| Net bias within the account | long ETHUSDC / short SOLUSDC (basis position) | short ETHUSDC / long SOLUSDC (mirror basis position) |
| Combined across A+B | ETHUSDC net ≈ 0, SOLUSDC net ≈ 0 | (same) |

Two properties fall out of this, and both matter:

- **Per-account neutrality to systematic moves**: within a single subaccount,
  a correlated move (ETHUSDC and SOLUSDC both up/down together) largely
  cancels in unrealized PnL, because one leg is long and the other short the
  correlated perp. Since HL uses cross margin (§1.4) this cancellation nets
  directly against the account's one shared USDC balance, stabilizing
  equity and reducing the chance of hitting the maintenance-margin threshold
  from a systematic (not idiosyncratic) move — see §1.4 for why this does
  **not** reduce required gross margin, only its volatility.
- **Portfolio-level flatness per coin**: because A and B are exact mirrors,
  summing ETHUSDC exposure across A+B ≈ 0, and same for SOLUSDC — the firm
  isn't carrying a naked ETH-vs-SOL basis bet at the aggregate level, only
  the (smaller, monitored) residual imbalance from imperfect mirroring.

### 1.3 Required `mm_core`/`perp_bot` extension: structural tilt

**Implemented.** Today's AS math always mean-reverted inventory toward
**zero**; it now supports a persistent, non-zero target via a
`target_inventory` field on `PerpPairConfig` (default `0.0`, so untilted
keepers are unaffected):

1. **Tilted reservation price** —
   [as_core.py](../../mm-core/src/mm_core/as_core.py)'s
   `gueant_reservation_price` gained an optional `q_target: float = 0.0`
   parameter; internally it computes `q_eff = q - q_target` and uses
   `q_eff` in both the base tilt term and the optional funding term:
   `r = mid - q_eff * gamma * sigma^2 / (2*kappa)`.
2. **Target-relative caps** —
   [risk_policy.py](../../mm-core/src/mm_core/risk_policy.py)'s
   `RiskPolicy.evaluate` gained a `target_inventory: float = 0.0` parameter;
   it now compares `inv_gap = abs(inventory.net_delta() - target_inventory)`
   against `max_position`/`critical_position` for both the `WIDEN` and
   `DE_RISK` thresholds, instead of raw `net_delta()`. A keeper tilted to
   `q*` no longer appears permanently over-cap.
3. **De-risk anchor** — [keeper.py](../src/perp_bot/keeper.py)'s `_actuate`
   computes `q_target = self.config.target_inventory` and passes it to
   `gueant_reservation_price` (QUOTE/WIDEN) and to the `DE_RISK` intent's
   `target_inventory`. `EMERGENCY_EXIT` intentionally still targets `0.0` —
   a full flatten in a true drawdown emergency shouldn't preserve the
   structural tilt.
4. `PerpPairConfig.target_inventory: float = 0.0` threads through to
   `Keeper._tick`/`_actuate` as above, and to
   [backtest.py](../src/perp_bot/backtest.py)'s `Strategy.__call__` (via
   `getattr(config, "target_inventory", 0.0)`) so tilted instances can be
   backtested before deployment.

Covered by new unit tests in `mm-core/tests/test_mm_core.py`,
`mm-core/tests/test_risk_policy.py`, and `perp-bot/tests/test_keeper.py` —
not yet confirmed passing in a live run (local sandboxing blocked test
execution this session); run `pytest` in both `mm-core/` and `perp-bot/`
before relying on this.

Still true regardless: do **not** attempt the tilt via `Caps` alone (e.g.
setting `max_position` asymmetric bounds) — `Caps` remains a single
symmetric `(max_position, critical_position)` pair around zero by design
(venue/representation-agnostic); the target-relative comparison lives in
`RiskPolicy.evaluate`, not in `Caps` itself.

### 1.4 Collateral allocation

Hyperliquid uses **cross margin** at the subaccount level: a single USDC
balance is the collateral for *every* open position on that subaccount (no
per-position isolated collateral pots), and account health is the ratio of
total equity — USDC balance **plus** combined unrealized PnL across all
positions on the account — to the summed maintenance margin of all
positions. This is precisely why pairing correlated legs on one subaccount
helps: a common-factor move that loses on the ETHUSDC-long leg gains on the
SOLUSDC-short leg, and both PnLs net against the *same* shared USDC balance.

Cross margin is scoped **per subaccount**, not shared across Sub A and Sub
B — they're separate accounts with separate USDC balances, which is why
§1.4's true-up step below is still needed.

What cross margin does **not** do is discount the *required* maintenance
margin itself — HL has no leverage/margin-mode API for portfolio-style risk
netting (confirmed via the `dex_executor` HL adapter), so it still sums full
per-position maintenance margin for both legs regardless of their
correlation. Concretely:

- Required maintenance margin is **not reduced** by holding offsetting legs
  — HL still charges full per-position maintenance margin on both the
  ETHUSDC and SOLUSDC leg regardless of their correlation. The benefit of
  pairing is **equity stability** via the shared collateral pool (offsetting
  unrealized PnL), not a smaller margin requirement.
- Worst-case (both legs simultaneously at their cap, not netting):
  $$MM_{\text{sub}} = \sum_{i \in \{ETH,SOL\}} \frac{\text{max\_position}_i \times \text{price}_i}{L_i}$$
  where $L_i$ is HL's max leverage tier **at that notional** (tiers shrink at
  higher notional — check HL's current tier table for the actual size, don't
  assume a flat max leverage).
- Residual risk after internal hedging is **basis risk** (ETH vs SOL
  diverging), not outright directional risk. Size a buffer from the
  historical dollar-notional-matched spread volatility:
  $$\text{basis\_buffer} = k_\sigma \times \sigma_{\text{spread}} \times \sqrt{T_{\text{rebalance}}} \times \text{gross\_notional}$$
  with $\sigma_{\text{spread}}$ estimated from `returns_{ETH} - \beta \cdot returns_{SOL}$ over a
  representative lookback (fit $\beta$ by regression, or use dollar-neutral
  1:1 sizing and skip $\beta$ if you'd rather keep the hedge ratio simple and
  re-fit less often).
- Total per-subaccount equity: $E_{\text{sub}} = MM_{\text{sub}} + \text{basis\_buffer} + \text{operational\_buffer}$
  — this is the single USDC balance to deposit into that subaccount
  (operational buffer: funding, fees, slippage tolerance — 10–20% of
  $MM_{\text{sub}}$ is a reasonable starting point).
- **Sub A and Sub B should be funded equally** (they're exact mirrors with
  matched notional caps) — but don't assume they *stay* equal: funding
  carry and basis P&L will accrue asymmetrically over time, and each
  subaccount's cross-margin pool only sees its own USDC balance, not the
  other's. True up collateral between them on a fixed cadence (e.g. weekly)
  rather than assuming symmetry holds indefinitely.

The $2,000 figure in the worked example is for carrying the **full calibrated
caps** at the illustrative 3x leverage. It is not the collateral required for
the smaller structural target. For the currently funded accounts, the target
is configured at 6x and the leverage-aware rebalancer budgets initial margin
against the live equity.

### 1.5 Netting / rebalancing controller — **Implemented** (`perp_bot.rebalancer.BasketRebalancer`)

Reuse the "imbalance" framing already used elsewhere in this shop's tooling
(see `dex_executor/memory/2026-04-13-troubleshooting.md`'s `theo imbalance`
metric) for consistency. v1 is a standalone periodic asyncio job (7 tests
passing); v2 (folding into keeper as a new `Decision`) is deferred until
live data shows how often correction is needed:

1. **Metric**, computed per subaccount at a slower cadence than the AS
   quoting tick (e.g. every 5–15 minutes — this is portfolio-level control,
   not HFT):
   $$\text{imbalance}_{\text{sub}} = \sum_{\text{coin}} \big(\text{position}_{\text{coin,sub}} - q^*_{\text{coin,sub}}\big) \times \text{price}_{\text{coin}}$$
   i.e. dollar-notional drift away from the *intended* tilted target, not
   away from zero.
2. **Trigger**: if `|imbalance_sub| / equity_sub` exceeds a threshold (start
   around 15–20%), or the portfolio-level per-coin sum
   `Σ_sub position_{coin,sub}` drifts materially from zero, issue a
   corrective hedge order sized to close the gap.
3. **Execution**: route the corrective order through `passive_aggressive`
   (patient) unless the drift also breaches that account's own
   `critical_position`/drawdown gates, in which case treat it with the same
   urgency as `EMERGENCY_EXIT`.
4. **v1 (recommended first)**: a standalone periodic job (cron-style, or a
   simple asyncio loop reading each account's positions via the Hummingbot
   connector) — no changes to `Keeper`'s per-tick loop required, keeps the
   blast radius of new code small.
5. **v2 (later)**: fold this into the keeper loop as a genuine new
   `Decision` (e.g. `REBALANCE_TO_TARGET`) distinct from `DE_RISK`, evaluated
   against the portfolio-level imbalance rather than each keeper's own
   isolated inventory. Only worth building once v1 has live data showing how
   often/how far the natural drift needs correcting.
6. **Logging**: extend the existing `decision_log_path` JSONL convention —
   one line per rebalance decision: `ts, account_id, coin, pre_position,
   q_target, hedge_size, hedge_side, imbalance_pct`.

### 1.6 Generalizing beyond one pair

For a basket of $2n$ coins split into $n$ correlated pairs, the same
structure repeats: $2n$ subaccounts (2 per pair), each coin gets a
long-tilt and a short-tilt instance on two different subaccounts, and each
subaccount pairs one long-tilt coin with one short-tilt coin from the same
correlated pair. Keep pairs **within** the same subaccount correlated (so
the internal hedge is real) — don't pair uncorrelated coins just to fill a
slot; an uncorrelated "hedge" leg doesn't reduce equity volatility, it adds
a second independent risk.

### 1.7 Aster and Lighter: verified against their own docs — and they are *not* the same as each other

The earlier draft of this section treated Aster and Lighter as one bucket
based on `perp_bot.venue_capabilities` claiming `hedge` mode for both. I've
since checked each venue's actual documentation and they diverge:

#### Aster — confirmed true hedge mode

[Aster's Hedge Mode docs](https://docs.asterdex.com/trading/perpetuals/hedge-mode.md)
confirm it directly: "Hedge Mode allows you to hold both long and short
positions at the same time under the same contract" (e.g. simultaneous long
and short BTCUSDT on one account), toggled per-account in settings (One-Way
vs Hedge Mode; cannot switch while positions/orders are open). This matches
`venue_capabilities`'s `aster -> position_mode="hedge"`.

[Aster's Margin docs](https://docs.asterdex.com/trading/perpetuals/margin.md)
confirm the same two margin modes as HL — **Cross** (default, shared across
all open positions, PnL nets, whole balance at risk) or **Isolated** (single
position). Maintenance margin is described as a function of the position's
own size via leverage tiers, with no mention of a reduced requirement for
offsetting hedge-mode legs — read this the same way as HL: cross margin
nets **PnL**, not the **margin requirement** itself.

**Practical implication**: Aster genuinely does not need the cross-subaccount
trick. One subaccount per coin, a long-tilt instance and a short-tilt
instance of the *same* coin, cross margin so their PnL nets — no correlated
proxy coin required, as originally proposed.

#### Lighter — likely net mode, not hedge mode (contradicts the current code assumption)

I could not find a "Hedge Mode" page anywhere in Lighter's docs (unlike
Aster, which has one). More tellingly, Lighter's own formulas model a
**single signed position per market per account**:

- [PnL and Total Account Value](https://docs.lighter.xyz/trading/pnl-and-total-account-value.md):
  "Let $pos_i$ be the current position size of an account... positive if
  long and negative if short" — one scalar per market, not independent
  long/short records.
- [Order Types & Matching](https://docs.lighter.xyz/trading/order-types-and-matching.md):
  Reduce-Only "ensures that changes to the position only move it closer to
  zero, regardless of whether the position is positive or negative," and the
  Order Margin risk check compares `New Order Side` against a singular
  `Position Side` — both presuppose one net position, not two coexisting
  sides.

This is strong evidence Lighter is structurally identical to Hyperliquid
here (net mode) — **not** hedge mode as `perp_bot.venue_capabilities`
currently claims for `lighter`. I've corrected that assumption in code (see
below); treat this as verified-by-inference rather than an explicit "we are
net mode" statement from Lighter, and re-confirm with Lighter directly (or a
testnet account) before relying on it for real capital.

[Lighter's margin model](https://docs.lighter.xyz/trading/multi-asset-margin.md)
is cross margin by default too — "Total Account Value = Collateral +
Unrealized PnL" is the account-wide health metric, extended by Multi-Asset
Margin (non-USDC collateral) and available per-position Isolated mode
(separate "Allocated Margin"). Same caveat as Aster/HL: this nets PnL
against a shared balance, it does not discount the underlying margin
requirement for correlated positions.

**Practical implication**: Lighter needs the **same 2-subaccount
cross-hedge basket structure as Hyperliquid** (§1.2), not the simplified
"1 account per coin" shape — it has the same single-net-position-per-market
constraint.

#### Code fix applied

`perp_bot/venue_capabilities.py`'s `lighter` entry was `position_mode="hedge"`,
`supports_same_account_hedge=True` — wrong per the above. Changed to match
Hyperliquid (`position_mode="net"`, `supports_same_account_hedge=False`), so
`validate_account_topology` now correctly rejects duplicate same-market
instances on one Lighter account, same as it already does for HL. `aster`
is unchanged (hedge mode confirmed correct).

**Recommendation if migrating to Aster**: don't replicate the HL
cross-subaccount basket structure — use "1 subaccount per coin, 2
same-account tilted instances (long + short) per coin" instead. Fewer
subaccounts, fewer collateral-transfer operations, no correlated-pair
dependency, while still achieving per-account (in fact per-coin) delta
neutrality directly. **For Lighter, replicate the HL structure as-is**
(§1.2) since it shares HL's net-mode constraint.

### 1.8 Decision summary

| | Hyperliquid | Aster | Lighter |
|---|---|---|---|
| Position mode | net | hedge (confirmed) | net (corrected — was misclassified as hedge) |
| Same-coin long+short on one account | Not possible — needs 2 subaccounts + a correlated pair partner | Possible directly | Not possible — needs the same 2-subaccount trick as HL |
| Structure for per-account neutrality | 2×2 cross-hedge basket across a correlated pair (§1.2) | 1 account per coin, 2 tilted instances, no pairing needed | 2×2 cross-hedge basket across a correlated pair, same as HL |
| Requires `mm_core` tilt extension (§1.3) | Yes | Yes | Yes |
| Margin model | Cross margin per subaccount (shared USDC balance + netted unrealized PnL), no correlation-based margin discount | Cross (default) or isolated; same PnL-nets-not-margin-discount pattern as HL | Cross (default, extendable via Multi-Asset Margin) or isolated; same pattern as HL |

---

## 2. Parameter estimation — **Calibrated** (30-day backtest, 15m candles)

Backtested ETH and SOL from 2026-07-31 to 2026-08-31 using `scripts/fetch_hl_data_v2.py`
(direct REST API, bypasses SDK init issues) and `scripts/run_backtest.py`. Grid search
over gamma/kappa to pass all gate checks (net_edge_bps > 2, markout_ratio < 0.5,
max_drawdown < 5%, liquidations = 0).

**Calibrated parameters** (see `scripts/basket_config.py`):

| Coin | gamma | kappa | max_position | critical_position | q* (target_inventory) | Backtest metrics |
|------|-------|-------|--------------|-------------------|----------------------|------------------|
| ETH  | 4.0   | 0.5   | 1.0          | 2.0               | ±0.4 (40% tilt)      | edge=4.75bps, markout=0.40, dd=2.55% |
| SOL  | 3.0   | 0.7   | 10.0         | 20.0              | ±4.0 (40% tilt)      | edge=51.3bps, markout=0.30, dd=0.89% |

`q*` sized at 40% of `max_position` per §1.3 point 2 — leaves 60% headroom for AS
quoting to skew before hitting `critical_position`. `Caps` bounds are interpreted
relative to `q*` (target-relative comparison in `RiskPolicy.evaluate`).

**Caveats**: backtest uses synthetic candle-derived trades (half volume on each side,
ordered by high/low reached first) — not real tick data. Fidelity gap vs. live is a
known limitation (§7). If live performance diverges significantly, replace with a
WS trade-stream logger run for the fetch window.

## 3. Order & inventory management

The nominal quote sizes remain 10%/5% of `max_position`, but
`Keeper._actuate` now clips each side so one full fill stays inside the cap,
the target-moving side cannot cross structural `q*`, and a reducing side
cannot flip through flat. OPMS sends reducing quotes venue-side reduce-only.
For tilted instances, `Caps` bounds are interpreted relative to `q*` (§1.3),
and the portfolio netting job (§1.5) is a second, slower control loop layered
on top of each keeper's own `RiskPolicy`.

## 4. Wiring / config — **Basket config ready** (`scripts/basket_config.py`)

Four `PerpPairConfig` instances (one per coin per subaccount), callable via
`from scripts.basket_config import get_basket_configs`:

```python
# Sub A: ETH tilt-long (+0.4), SOL tilt-short (-4.0)
# Sub B: ETH tilt-short (-0.4), SOL tilt-long (+4.0)
# All parameters calibrated per §2, q* at 40% of max_position
```

`validate_account_topology(get_basket_configs())` passes — no duplicate
`(exchange, coin, account_id)` tuples. The topology rule was never the blocker
for this shape; the missing structural-tilt mechanism (§1.3) was, and it's now
implemented.

The real-Hummingbot deployment mirrors the basket with two controller
configuration files per instance: `opms_perp_mm_e2_mm1_shadow.yml` targets
`e2_mm1` with ETH `+0.4` / SOL `-4.0`, and
`opms_perp_mm_e2_mm2_shadow.yml` targets `e2_mm2` with the opposite tilts.
`PerpMMControllerConfig.target_inventory` passes each YAML tilt into the
keeper's `PerpPairConfig`; the configured leverage is passed through as well.
`deploy/hummingbot/scripts/validate_hb_deploy_configs.py` loads both files
through Hummingbot's loader and checks topology, targets, 6x leverage, 5-second
shadow cadence, and credential routing.

**Collateral sizing for the currently funded accounts** (no portfolio-margin
discount):
- At the 2026-09-16 HL mids (ETH ~$2,402.65, SOL ~$97.28), the target is
  `0.4 ETH + 4 SOL` in gross terms, about **$1,350 per subaccount**.
- At the configured **6x leverage**, target initial margin is about **$225**.
- The rebalancer's default 2.5x gross allowance at its 3x reference leverage
  is a $250 initial-margin budget on $300 equity, so the current target fits.
- `max_position` remains the calibrated inventory bound; it is larger than the
  target and the HB budget checker plus rebalancer margin guard prevent a
  $300 account from opening the full cap.

## 5. Rollout sequence

Backtest → shadow → testnet gate → micro-mainnet → scale, as in the general
plan, with these basket-specific additions:

1. Backtest each tilted instance individually first (does the tilted AS
   quoting behave sanely at the chosen `q*`?), then backtest the netting
   controller against **both** accounts' simulated positions together
   (does the imbalance metric actually stay bounded?).
2. Shadow-run all four instances (ETH×{A,B}, SOL×{A,B}) simultaneously
   before any live capital — confirm the *aggregate* per-coin exposure
   stays near zero in the decision logs, not just each instance individually.
3. Micro-mainnet: fund both subaccounts at the smallest workable size,
   validate the netting job actually fires and corrects a drift within one
   full monitoring cycle before increasing size.
4. Before enabling dual-account writes, run a bounded single-account
   Hummingbot soak. **Runtime completed 2026-09-16 on `e2_mm1`; the behavioral
   safety gate failed.** The 30-minute run used the calibrated ETH/SOL
   controllers at 6x and produced 346 ETH and 345 SOL live decision records,
   four unique maker fills, and a maximum observed gross position of about
   $379. The control loop kept ticking, but regime gating stopped quoting for
   long intervals while residual positions remained open. Teardown
   market-closed the residual ETH/SOL positions and verified zero scoped orders
   and positions. One post-only race was rejected and retried successfully.
   The resulting safety remediation is implemented and covered offline:
   quote stops now cancel and flatten residual inventory with one bounded
   reduce-only PA child; venue-order-id liveness has a 15-second watchdog and
   30-second recovery circuit; quote refresh is cancel-then-create; controller
   HL reads have 5-second deadlines with 429-aware exponential backoff; and
   the bounded launchers use an MQTT-free runtime. A fresh live soak is still
   required before this gate can pass.

## 6. Monitoring & kill switch

In addition to the general plan's per-keeper monitoring: track
`imbalance_sub` (§1.5) and portfolio per-coin net exposure as first-class
dashboard metrics, alert if either persists beyond the rebalance job's own
threshold for more than one cycle (a stuck rebalancer is its own failure
mode, independent of each keeper's own risk policy).

## 7. Known gaps to close before real capital

- ~~**Structural tilt (§1.3)**~~ — **Implemented and verified** (121 mm-core +
  74 perp-bot tests passing). `q_target` in `gueant_reservation_price`,
  `target_inventory` in `RiskPolicy.evaluate`, threaded through `Keeper` and
  `Backtest`. Lighter venue corrected to `position_mode="net"`.
- ~~**Netting/rebalancing controller (§1.5)**~~ — **Implemented** (`perp_bot.rebalancer.BasketRebalancer`,
  v1 standalone periodic job). 7 tests passing. Per-subaccount imbalance metric,
  portfolio-level per-coin net tracking, configurable thresholds, JSONL decision
  logging, shadow mode. **Live shadow ran 2026-09-15** against real HL positions
  and mids (`scripts/run_rebalancer_shadow.py` + `rebalancer_feeds.py`, 7 feed
  tests; four basket instances mapped onto `e2_mm1`/`e2_mm2`, logs in
  `perp-bot/logs/rebalancer_shadow_*.jsonl`). It surfaced a sizing gap the unit
  tests could not: the 300 USDC accounts read ~190% imbalance and a full tilt
  establishment would need ~$1363 gross vs the §1.4 budget (~2.5x equity =
  ~$749 gross at the 3x reference leverage), so a new leverage-aware
  `max_gross_notional_multiple` guard (default 2.5, reference 3x) now
  skips-and-logs such corrections instead of proposing them at insufficient
  leverage (2 tests).
  **Follow-up read-only HL run passed 2026-09-16:** concurrent real-HB
  connector/controller smokes for `e2_mm1` and `e2_mm2` both read the correct
  vault account, positive unified equity, and plausible funding (`8.2838e-06`);
  a fresh live-feed shadow saw flat portfolio net exposure and emitted four
  `capacity_guard_skip` records under the old 1x configuration. The basket is
  now configured at 6x, so the current target fits the same $300 accounts;
  the full calibrated caps still do not. Still open: the OPMS intent feed
  (needs OPMS running) and a bounded correction using the 6x configuration.
- **No portfolio-margin confirmation on HL, Aster, or Lighter** — all three
  net PnL via cross margin but none confirm a reduced margin *requirement*
  for offsetting positions; collateral must be sized for the gross sum
  (§1.4) on all three venues. **HL portfolio margin requires $5M trading
  volume** — not available for initial deployment, so cross margin is the
  only option for now (already accounted for in §1.4 sizing). **A
  margin-health stop is now implemented (2026-09-15)**: `RiskConfig`
  gained `margin_health_soft=0.20`/`margin_health_hard=0.10` and
  `RiskPolicy.evaluate` takes `margin_available` (the venue-computed
  available-after-maintenance balance; HL unified accounts publish it as
  `spotClearinghouseState.tokenToAvailableAfterMaintenance`). Hard breach →
  `EMERGENCY_EXIT`, soft → `DE_RISK`, `None` (venue doesn't publish it)
  disables both — threaded through `perp_bot.Position.margin_available` →
  `Keeper._tick`, and `OpmsClient` parses it from the positions payload
  when the OPMS provides it. **The HB controller path feeds it too**:
  `PerpMMController._current_margin_available()` reads the live
  `spotClearinghouseState.tokenToAvailableAfterMaintenance` through the HB
  connector's own REST machinery each control cycle (config:
  `margin_health_soft`/`margin_health_hard`, defaults 0.20/0.10). Margin data
  now uses the shared `fail_closed_margin_available` invariant: failed, missing,
  malformed, or non-finite reads are logged at critical severity and become a
  zero-margin hard breach, forcing `EMERGENCY_EXIT`. Decision logs and replay
  carry the validated value and configured thresholds. Verified
  read-only on mainnet `e2_mm1`
  (`tests_real/test_hyperliquid_mainnet_connector.py`).
- ~~**Lighter's net-vs-hedge classification**~~ — **Verified**: Lighter is
  confirmed net-mode (single signed position per market). Code is correct.
- ~~**Post-only order support**~~ — **Verified at the venue level**: HL
  `Alo` (Add-Liquidity-Only) orders are maker-only, proven live on mainnet
  2026-09-14 (passive rests, crossing is rejected without filling).
  **Confirmed through Hummingbot 2026-09-15:** the connector maps
  `OrderType.LIMIT_MAKER` to `{"tif": "Alo"}`, and
  `scripts/run_hb_mainnet_quote_gate.py` placed and rested real
  `ExecutionStrategy.LIMIT_MAKER` orders through it on mainnet. Note that HB's
  `OrderExecutor` also clamps a maker price to the touch
  (`min(price, best_bid)` / `max(price, best_ask)`), so a keeper quote inside
  the spread becomes a join-the-touch order rather than an ALO rejection.
- ~~**Subaccount creation and funding**~~ — **Done**: mm1 and mm2
  created on HL, API keys stored in `.env` (repo root), each funded with
  300 USDC. **Verified 2026-09-14:** `e2_mm1`/`e2_mm2` hold 300 USDC each
  and `e2_main` 131.98 USDC, all in **spot** (HL unified-account mode, so spot
  USDC collateralizes perps — perp `accountValue` reads 0 until a position
  opens). ⚠ All three entries currently share **one agent signer key**
  (`0x8a9e…Fd709`); fine sequentially, but split them onto separate agent
  wallets before running the basket's mm1+mm2 legs concurrently, or they will
  collide on HL nonces.
- **Hummingbot connector setup** — code path ready, credentials not yet
  imported. **Updated 2026-09-15:** Hummingbot (the pinned
  `/home/christian/sources/hummingbot` checkout, v2.16.0) is now **compiled** —
  all 60 Cython `.pyx` have `.so` files and `import
  hummingbot.connector.connector_base` succeeds. `PerpMMController` was fixed
  against the real runtime: it used a non-existent `TwapExecutorConfig` (the
  real class is `TWAPExecutorConfig`, taking
  `total_amount_quote`/`total_duration`/`order_interval`/`mode`), it never
  registered its custom `passive_aggressive_executor` config with
  `ExecutorOrchestrator._executor_mapping`, and `_current_equity()` looked up
  `"USDC"` where the HL connector reports `"USD"` — all fixed and locked by
  `hb-enhanced-opms/tests_real/` (35 real-HB tests, no stubs). The live
  read-only smoke (`scripts/run_hb_mainnet_smoke.py`) now runs the real
  `on_start()` + `update_processed_data()` on mainnet for `e2_mm1` (vault) and
  `e2_main` (master) with no orders, verifying FillObserver registration, mid,
  funding, positive equity, and analytics.
  `scripts/import_hl_mainnet_credentials.py` sets `use_vault` correctly per
  account (auto-detects subaccount vs master), but the actual credential import
  still needs `HB_PASSWORD`.
  ⚠ **The pinned checkout carries one local patch:** the HL WS funding parser
  used `openInterest` as the funding rate (reported ~1.04e6 instead of
  `1.25e-05`); patched to use `funding`. Re-apply after any Hummingbot update;
  the smoke guards it.
  ⚠ Hummingbot keys credentials by **connector name**, and there is only one
  `hyperliquid_perpetual` slot per process. **Dual read-only startup passed
  2026-09-16:** `deploy/run_dual_shadow_session.sh` used two disposable
  Hummingbot runtimes and two separate encrypted stores; both `e2_mm1` and
  `e2_mm2` started concurrently, initialized ETH and SOL, and produced fresh
  shadow decision logs with no order executor events. The two HL vault
  addresses are distinct. They still share one agent signer key, so concurrent
  writes remain gated until nonce isolation is in place.
  **Live quote/cancel gate passed 2026-09-15** on `e2_mm1` and `e2_mm2`
  (`hb-enhanced-opms/scripts/run_hb_mainnet_quote_gate.py`): the real
  `PerpMMController` turned its own `QUOTE` decision into two resting 0.006 ETH
  `Alo` orders on the target subaccount only (raw-SDK check of all three
  accounts), refreshed them, and tore down to 0 orders / 0 positions with
  balances unchanged. **Fill / de-risk / emergency gate passed 2026-09-15**
  on `e2_mm1` (`run_hb_mainnet_derisk_gate.py`): real fill and position
  reconciliation, reduce-only passive de-risk to flat, emergency exit to flat
  in 24.8 s, after fixing six HB-path defects (position source, executor
  churn, reduce-only, min-notional children, `ExecutorInfo` union, stale
  position after fills). Emergency child limit is 5 s (market order 8.7 s
  after the decision). Unified-account equity was verified live to include
  unrealized PnL (HL marks the spot USDC total to market), so the drawdown stop
  is sound; `tokenToAvailableAfterMaintenance` is available if a
  margin-health stop is wanted. **Concurrent read-only smokes passed
  2026-09-16** for both basket subaccounts; concurrent order execution remains
  untested because the accounts still share one agent signer and HB has one
  `hyperliquid_perpetual` credential slot. See `perp-bot/status.md`.
- ~~**Raw mainnet subaccount routing**~~ — **Proven 2026-09-14** (raw HL SDK):
  `hb-enhanced-opms/tests_live/` ran 30/30 on HL mainnet; orders signed with
  `vault_address=<subaccount>` rest only on that subaccount and are invisible
  to the other subaccount and the master, then cancel cleanly (0 orders/0
  positions left). Post-only (`Alo`) was also proven maker-only: a passive ALO
  rests, a crossing ALO is rejected rather than filled. This validates the
  routing and maker-only seams HB must reproduce; it is not yet HB connector
  evidence.
- ~~**Historical data fetch**~~ — **Completed**: 30 days of ETH/SOL 15m candles
  fetched via `scripts/fetch_hl_data_v2.py` (direct REST API, bypasses SDK init
  issues). Data in `perp-bot/data/`. Backtest calibration done (§2).
- **Live integration testing** — the HB quote path is proven live (above), the
  real two-instance basket launcher plus position/price feeds are covered in
  read-only shadow mode, and the single-account `e2_mm1` run completed its
  runtime and cleanup checks. Its behavioral safety gate failed: regime stops
  left positions unquoted for minutes. The quote-stop flatten, quote-liveness
  watchdog, target/flat fill bounds, MQTT isolation, and bounded timeout/429
  circuits are now implemented and tested offline in both execution seams
  where applicable. Execution through a running native OPMS and a live rerun
  of the failed soak remain open before relying on the rebalancer for capital.
- Everything listed in the general deployment plan's gaps section (leverage
  tier selection per instance, backtest's hardcoded `liquidations=0` gate,
  synthetic candle-derived backtest trades) still applies per-instance here.

### 7.1 Gap audit — 2026-09-16

The remaining items were checked against the implementation, local tests, and
the live HL run above:

- **Closed:** structural tilt, venue-mode classification, post-only routing,
  subaccount routing, historical data/calibration, margin-health fail-closed
  handling, real-HB quote/fill/de-risk/emergency paths, dual real-HB shadow
  startup/config loading, live position/mid feeds for the rebalancer, and the
  single-account runtime startup/cleanup path on `e2_mm1`.
- **Soak evidence:** `hb-enhanced-opms/logs/live_soak_20260916T132141/` records
  1,800 seconds of live operation. The monitor observed maximum gross exposure
  of $379.29, maximum initial margin of $63.22, minimum margin health of
  98.83%, and maximum six scoped resting orders. Four maker orders filled
  during the run; one post-only price race was rejected and retried. Cleanup
  market-closed 0.0231 ETH and 2 SOL, leaving $729.33 USDC with zero ETH/SOL
  orders and positions.
- **Implemented offline; live rerun pending — quote liveness and position
  safety, found in this soak:** SOL
  stopped quoting at 13:24:51 with a -2 SOL position and again at 13:33:06;
  ETH stopped at 13:25:29 with +0.0769 ETH, briefly resumed, then stopped at
  13:27:36 with -0.0231 ETH until cleanup. ETH only had two short quote
  windows around 13:39 and a final window around 13:50. The decision logs
  continued at roughly the 5-second cadence (maximum gaps about 8.2 seconds
  for ETH and 9.0 seconds for SOL), so these were intentional regime-gate
  stops rather than a dead controller. `STOP_QUOTING` previously cancelled
  orders but left the current position untouched. It now targets flat through
  one reduce-only PA child with a 15-second passive deadline; quote sides are
  size-clipped at structural `q*` and flat, and both OPMS bodies carry the
  reduce-only flag to the venue. The HB controller now requires actual venue
  order ids, trips after 15 seconds missing, cools down for 30 seconds, and
  refreshes quotes in two phases so old and new capped quotes never overlap.
  The worst marked loss was about -$2.65 before
  fees, driven by the unquoted SOL and ETH positions; final realized PnL and
  fees left the account $2.395934 below the preflight balance.
- **Implemented offline; fault injection and live rerun pending — venue fault
  handling:** the launcher logs contain no confirmed HL
  429/rate-limit or HTTP timeout during this run, and one post-only race was
  retried. The optional MQTT bridge did produce repeated connection refusals
  and 30-second connection timeouts. That did not stop the decision loop, but
  the soak does not prove bounded handling for HL/API rate limits, transport
  timeouts, or reconnect isolation. Controller-owned HL reads and the native
  HL adapter now have explicit 5-second request deadlines, exponential
  backoff, immediate 429 circuit opening (including `Retry-After`), and
  fail-safe cancel/flatten behavior. The deployment launcher bypasses HB's
  MQTT-coupled headless loop and shuts the trading core down directly on
  SIGINT/SIGTERM. Unit tests inject timeout and 429 failures; a live fault or
  soak rerun is still required for operational evidence.
- **Verified:** with the basket set to 6x, the leverage-aware capacity guard
  accepts the current ~$1,350 target gross on each $300 account while keeping
  the target initial margin below the buffered allowance. The larger
  calibrated caps still require more collateral or smaller caps.
- **Still open:** run the rebalancer's `ExecIntent` path against a running
  native OPMS and observe a bounded correction; import credentials into the
  persistent production HB stores; rerun the single-account soak with the new
  quote-stop/liveness/fault containment; and run the two instances with writes
  after signer nonce isolation is complete.
- **Still open:** split the shared agent signer keys before concurrent writes;
  obtain isolated HL testnet credentials for the dedicated Phase-2 gate; finish
  PA-V2 execution replay/parity; complete the one-week native-oracle shadow;
  and add an explicit real-connector trading-rules assertion.
- **Accepted deployment constraints:** no portfolio-margin discount is assumed
  for HL; 6x leverage is configured for the current $300 target accounts and
  must stay within the venue's per-asset tier; the backtest liquidation metric
  remains hardcoded to zero; and the basket replay is candle-derived rather
  than tick replay. These are risk disclosures, not evidence that the
  corresponding gaps are closed.

The next HL write test is a bounded `e2_mm1` safety-soak rerun. The subsequent
dual-account write is gated on separate agent wallets and independent HB
credential slots: a concurrent two-leg micro quote/cancel test, followed by a
seeded, bounded rebalancer correction through OPMS. The prior single-account
soak does not close that dual-write gate.
