# Competitive & Profitability Analysis: Hyperliquid, Aster and Lighter
## Crypto Perpetuals vs RWA Perpetuals

**As of:** 8 September 2026  
**Scope:** Market-making economics, capital efficiency, collateral yield/staking, portfolio/cross margin, hedge-vs-net position accounting, funding/basis carry, and venue-specific tail risks.

> This memo is an analytical framework, not an audited return forecast. Fee schedules, incentive programs, collateral factors, risk parameters, API limits and funding rates can change quickly. Live venue configuration should be treated as the execution-time source of truth.

---

## 1. Executive conclusion

The three venues should not be ranked on spread capture alone. A market maker's true economics are better represented by:

\[
\Pi =
V_m e_{\text{execution}}
+
\bar N\,c_{\text{funding}}
+
C_y y_{\text{collateral}}
+
S y_{\text{stake}}
+
I_{\text{rewards}}
-
C_{\text{fixed}}
-
L_{\text{tail}}
\]

where:

- \(V_m\) = maker filled notional;
- \(e_{\text{execution}}\) = net execution edge per maker dollar;
- \(\bar N\) = average held hedge/carry notional;
- \(c_{\text{funding}}\) = net funding/basis carry;
- \(C_y\) = collateral that remains yield-generating;
- \(y_{\text{collateral}}\) = yield on that collateral;
- \(S\) = separately staked treasury capital;
- \(y_{\text{stake}}\) = staking yield;
- \(I_{\text{rewards}}\) = MM/token/incentive rewards;
- \(C_{\text{fixed}}\) = engineering, infra, custody, monitoring and operational costs;
- \(L_{\text{tail}}\) = expected/tail-loss reserve for liquidation, oracle, venue and basis events.

Two corrections materially affect the original venue comparison:

1. **Yield on collateral and staked treasury assets can add meaningful ROE.**  
   Aster is strongest in this category because some yield-bearing assets can remain usable as trading collateral. Hyperliquid now has an economically meaningful borrow/lend yield on idle assets in Portfolio Margin, while native HYPE staking remains separate from trading collateral. Lighter's LIT staking improves fees and funding economics and earns staking APR, but LIT is not currently UTA trading collateral.

2. **Account-level position and margin architecture matters.**  
   Aster has an explicit **Hedge Mode** that can maintain separate LONG and SHORT positions in the same contract and account. Hyperliquid and Lighter use a signed/net position model per market; deliberately holding simultaneous long and short positions in the same contract generally requires separate subaccounts. On Hyperliquid, those subaccounts are separately margined, so the second subaccount can destroy much of the margin-offset benefit one might expect from "portfolio margin."

This leads to four different rankings rather than one:

| Dimension | 1st | 2nd | 3rd |
|---|---|---|---|
| Crypto-perp liquidity/capacity | **Hyperliquid** | Aster | Lighter |
| Explicit MM / collateral-yield economics | **Aster** | Hyperliquid | Lighter |
| Same-contract long + short position flexibility | **Aster** | Hyperliquid / Lighter via separate subaccounts | — |
| RWA-perp liquidity/capacity | **Hyperliquid trade[XYZ]** | Lighter | Aster |

For **crypto perps**, Hyperliquid remains the best core venue for scalable, lower-edge/high-turnover MM. Aster can produce the best unit economics when the firm qualifies for MM rebates and combines them with productive collateral. Lighter can be attractive when its zero-fee Standard account or low-latency Premium account produces superior markout after accounting for latency and LIT staking economics.

For **RWA perps**, trade[XYZ] on Hyperliquid is overwhelmingly the capacity venue, but its current market-level margin restrictions reduce the value of Hyperliquid's broader Portfolio Margin architecture. Lighter is the credible second RWA venue. Aster has useful account features and many listed markets, but current RWA liquidity is much smaller.

The central operational principle is:

> **Global delta neutrality is not local liquidation neutrality.**

A $10 million long on one venue and $10 million short on another can have near-zero consolidated delta while still liquidating one leg if venue-specific margin equity, oracle marks or basis diverge.

---

## 2. Market snapshot

### 2.1 Venue-wide perpetual activity

A current normalized-volume snapshot gives approximately:

| Venue | Normalized 30d perp volume | Active liquidity | Share of these three by normalized 30d volume |
|---|---:|---:|---:|
| Hyperliquid | ~$244.8B | ~$2.49B | ~72.8% |
| Aster | ~$52.6B | ~$0.43B | ~15.6% |
| Lighter | ~$39.0B | ~$0.92B | ~11.6% |

The purpose of normalized volume is to reduce the influence of suspected wash/non-economic activity. These are venue-level figures and should not be read as pure "crypto-only" volume, but they are useful as a liquidity/capacity proxy.

### 2.2 RWA perpetual activity

For RWA perps, the concentration is much more extreme:

| Venue | RWA OI | RWA 24h volume | RWA markets | Share of OI among these three |
|---|---:|---:|---:|---:|
| Hyperliquid / trade[XYZ] | ~$3.74B | ~$1.32B | 89 | ~96.8% |
| Lighter | ~$95.5M | ~$131.7M | 76 | ~2.5% |
| Aster | ~$28.2M | ~$31.5M | 108 | ~0.7% |

On the sampled day, trade[XYZ] also represented roughly 89% of RWA daily volume among these three venues.

This distinction is crucial:

- **Crypto MM:** all three are economically relevant.
- **RWA MM:** Hyperliquid/trade[XYZ] is currently the only venue of the three with broad institutional-scale OI; Lighter is a useful second leg; Aster is selective rather than the natural core hedge venue.

---

## 3. Definitions that matter for profitability

### 3.1 Net / one-way mode

In a net position model, each market has one signed position:

\[
q > 0 \Rightarrow \text{long}, \qquad
q < 0 \Rightarrow \text{short}
\]

A sell against an existing long reduces or closes it rather than creating an independent short object.

This is usually **good for pure market making** because offsetting fills naturally recycle inventory and reduce gross margin consumption.

It is less convenient when the firm wants to preserve two economically distinct strategies—for example:

- a persistent funding-carry long;
- and an independent short inventory generated by the MM engine.

### 3.2 Hedge mode

Hedge mode stores separate LONG and SHORT positions in the same contract.

Economically:

\[
\Delta_{\text{net}} \approx \Delta_L + \Delta_S
\]

but the venue may still calculate initial and maintenance margin from **gross** positions:

\[
N_{\text{gross}} = |N_L| + |N_S|
\]

Therefore hedge mode should not automatically be interpreted as a 100% capital offset.

Its main advantages are:

- strategy separation;
- avoiding accidental netting of a persistent carry leg;
- independent entry-price/accounting history;
- simpler attribution and stop logic.

Its main disadvantage is potentially higher gross IM/MM.

### 3.3 Cross margin

Cross margin means positions share an account equity pool. A profitable position can economically support a losing position within that margin account.

It does **not** necessarily mean that the venue gives a risk-based margin offset for negatively correlated or delta-neutral positions.

### 3.4 Portfolio margin

"Portfolio margin" is venue-specific.

A true risk-based portfolio-margin engine would calculate requirements based on stressed portfolio risk, covariance or scenario loss rather than summing position-by-position gross requirements.

Hyperliquid's current Portfolio Margin is economically valuable because it unifies eligible balances/PnL and supports borrow/lend, but its published maintenance framework should not be assumed to provide a full SPAN/VaR-style offset of gross long and short risk. Market-level isolated-only rules can also override the broader account architecture.

### 3.5 Yield-bearing collateral

There are two very different cases:

1. **The asset earns yield while still serving as margin.**  
   This directly increases trading-capital ROE.

2. **A treasury asset is staked separately and gives yield/fee benefits.**  
   This improves total enterprise economics, but the staked capital must still be included in the denominator when calculating ROE.

The second case is often overstated because the fee saving is counted while the opportunity cost of the staked token is ignored.

---

## 4. Capital-efficiency matrix

| Feature | Hyperliquid | Aster | Lighter |
|---|---|---|---|
| One signed/net position per market | Yes | Optional; One-Way Mode | Yes |
| Native same-contract Hedge Mode | **No documented same-account dual leg** | **Yes** | **No documented same-account dual leg** |
| Two same-contract directions possible via subaccounts | Yes | Yes, but usually unnecessary | Yes |
| Subaccounts share margin equity | **No** | Separate account assets/positions | Separate |
| Cross/shared collateral | Yes by account mode | Yes in Cross / Multi-Asset Mode | Yes via UTA for eligible products |
| Full risk-based gross-position offset | Do not assume | Do not assume | No; requirements are gross-position based |
| Yield on trading collateral | PM idle borrowable assets can earn supply yield | **Yes: supported productive collateral such as USDF/asBNB** | No documented yield-bearing UTA collateral |
| Native-token staking separately | HYPE | ASTER | LIT |
| Staking affects trading economics | Fee discounts | VIP eligibility / ecosystem rewards | Fee discounts + funding rebate |
| Key HFT limitation | Unified/PM action cap vs Standard | Program/risk parameter dependence | Standard latency vs Premium fees |
| RWA cross/portfolio margin | Most trade[XYZ] markets currently isolated-only | Contract-specific; verify each market | Opening RWA positions under Cross currently restricted |

---

# Part I — Venue analysis

## 5. Hyperliquid

### 5.1 Why it remains the core crypto venue

Hyperliquid has the strongest combination of:

- normalized perp volume;
- open interest;
- consistent two-sided flow;
- inventory recycling;
- deep major-coin books;
- low fee tiers for high-volume traders;
- maker rebates for sufficiently large maker share.

At high rolling volume, standard perp maker fees can reach zero; high maker-share participants can receive maker rebates of approximately 0.1-0.3 bp.

For a market maker, this favors a **low-edge/high-turnover** model.

### 5.2 HYPE staking economics

HYPE staking serves two roles:

1. native staking rewards;
2. trading-fee discounts.

A recent September 2026 indication places native HYPE staking around **~2.17% annualized**, with rewards automatically accruing/redelegating under the native staking design.

The important accounting treatment is:

> **Staked HYPE is not simultaneously ordinary perp trading collateral.**

HYPE moves to the staking account. Therefore it should be treated as treasury capital \(S\), not as margin capital \(C_y\).

If the same user/address or a valid staking-linked trading account receives a fee discount, the economic return on the stake is:

\[
R_{\text{HYPE stake}}
=
S y_{\text{HYPE}}
+
V_{\text{aggressive}}
\Delta f_{\text{taker}}
+
V_{\text{other eligible}}
\Delta f
-
C_{\text{HYPE hedge}}
\]

where \(C_{\text{HYPE hedge}}\) includes:

- HYPE short-perp funding;
- basis;
- execution;
- liquidation buffer;
- staking/unstaking liquidity mismatch.

A market-neutral firm should not call the 2.17% staking yield "free yield" if it is carrying unhedged HYPE price risk.

#### HYPE staking fee tiers

The published fee-discount ladder currently includes approximately:

| HYPE staked | Fee discount |
|---:|---:|
| >10 | 5% |
| >100 | 10% |
| >1,000 | 15% |
| >10,000 | 20% |
| >100,000 | 30% |
| >500,000 | 40% |

The fee benefit is most valuable when the MM has meaningful taker/hedge flow. A maker that rarely crosses the spread should not allocate millions of dollars of HYPE purely for a marginal fee discount without calculating capital return.

### 5.3 Portfolio Margin and yield on idle collateral

Hyperliquid now has Portfolio Margin / borrow-lend functionality under which eligible idle borrowable assets can earn supply yield.

A live HyperCore lending snapshot on 8 September 2026 showed roughly:

- USDC supplied: ~$416.4M;
- USDC borrowed: ~$262.7M;
- utilization: ~63.1%;
- borrow APY: ~5%;
- **USDC supply APY: ~2.84%**.

The published lending mechanism allocates borrower interest to suppliers after a protocol retention component.

This is economically important because idle stable collateral has historically had a near-zero return on many perp venues.

If \(C_{\text{idle}}\) remains eligible for supply yield:

\[
P\&L_{\text{collateral yield}}
=
C_{\text{idle}} y_s
\]

At 2.84%, $5M continuously earning would produce a theoretical ~$142k/year. In practice the realized amount will be lower if some capital must remain immediately liquid, outside PM, or in separately margined subaccounts.

### 5.4 Why Portfolio Margin is not automatically best for an HFT MM

Hyperliquid's account modes create a genuine trade-off.

**Standard Mode** is recommended for high-volume automated users and separates balances/margin domains in a more traditional way.

**Unified / Portfolio Margin** unifies eligible balances and supports richer collateral/borrow-lend behavior, but currently has a finite user-action allowance that can be material for a high-frequency quoting engine.

Therefore a firm should distinguish:

- **execution capital** that needs unrestricted high-rate order/cancel activity;
- **treasury/slow inventory capital** that can benefit from PM yield and collateral unification.

This creates a capital-allocation frontier:

\[
\text{extra yield from PM}
\quad \text{vs} \quad
\text{lost execution flexibility / additional account fragmentation}
\]

The highest-yield account mode is not necessarily the highest-P&L MM account mode.

### 5.5 Hyperliquid same-contract long and short

Hyperliquid's perps model exposes one signed position per market in an account.

Consequently, maintaining two independent same-contract directions generally requires:

- Subaccount A: LONG;
- Subaccount B: SHORT.

This is materially different from Aster Hedge Mode.

Most importantly:

> **Hyperliquid subaccounts are separately margined.**

Thus if:

- Subaccount A is +$10M BTC;
- Subaccount B is -$10M BTC;

the business is globally delta-neutral, but the venue does not treat those two subaccounts as one zero-risk margin portfolio.

That produces:

- duplicated margin buffers;
- separate liquidation paths;
- operational collateral transfer requirements;
- no automatic cross-subaccount PnL offset.

Fee-volume tiering is more favorable because subaccount activity can contribute to the master/user fee tier, but **margin is still fragmented**.

### 5.6 Best use of net mode on Hyperliquid

For a pure MM engine, the signed/net model is usually desirable.

Example:

1. MM sells $1M BTC perp and becomes -$1M.
2. Later MM buys $600k from another taker.
3. Position naturally moves to -$400k.

This recycles inventory without paying a taker hedge fee.

Creating two subaccounts simply to preserve both sides would often worsen capital efficiency.

Two subaccounts are justified when the firm needs to preserve economically distinct exposures, such as:

- persistent funding carry;
- customer-flow/MM inventory;
- different risk mandates;
- different liquidation limits.

### 5.7 RWA caveat: trade[XYZ] margin structure

This is the largest reason not to over-credit Hyperliquid Portfolio Margin in an RWA analysis.

Current trade[XYZ] documentation indicates that **XYZ markets are presently isolated-only**, with distinctions between normal-isolated and strict-isolated implementations.

Therefore:

- Hyperliquid has the best RWA liquidity of the three;
- but most of that RWA liquidity does **not** currently receive the full capital-pooling benefit one might infer from Hyperliquid's Portfolio Margin headline.

For RWA MM this is a major capital-efficiency penalty.

### 5.8 Hyperliquid-specific risks

#### Crypto perps

- **ADL / liquidation-system risk.**
- **Thin-market manipulation risk.**
- **Validator/governance intervention risk in extreme incidents.**
- **Cross-venue basis risk.**
- **API/order-rate/account-mode constraint risk.**
- **HYPE staking concentration and token-price risk if used to earn fee discounts.**
- **Borrow/lend utilization/yield risk:** current supply APY is floating, not guaranteed.

#### HIP-3 / RWA

Additional risks include:

- deployer-specific oracle design;
- market halt/settlement rules;
- feed divergence from TradFi reference;
- isolated-margin capital inefficiency;
- stock/index corporate-action and market-hours discontinuity;
- sudden mark jumps when the underlying cash market reopens;
- deployer fee/risk-parameter changes.

The 2026 SK Hynix oracle anomaly is a useful reminder that a portfolio can be economically hedged against the underlying while still being exposed to the venue's **specific mark/oracle path**.

---

## 6. Aster

### 6.1 Aster's competitive advantage

Aster has three features that can compound:

1. explicit market-maker rebates/incentives;
2. Hedge Mode;
3. yield-bearing multi-asset collateral.

That combination makes Aster potentially the strongest venue for **capital-adjusted unit economics**, even though its absolute liquidity is well below Hyperliquid.

### 6.2 Market-maker economics

The current qualified MM program has published tiers around:

| MM tier | Qualification example | Taker fee | Maker fee/rebate |
|---|---|---:|---:|
| MM1 | ≥$500M monthly volume or ≥1.5% maker share | ~2.0 bp | **-0.25 bp** |
| MM2 | ≥$1B monthly volume or ≥3% maker share | ~1.6 bp | **-0.50 bp** |

Aster has also advertised a monthly ASTER reward pool for qualifying market makers.

These incentives should not be capitalized at 100% of face value in a long-term model because:

- qualification can be lost;
- token value can move;
- venue rules can change;
- reward programs are discretionary.

A conservative internal budget should separate:

\[
e_{\text{core}}
\quad \text{from} \quad
e_{\text{incentive}}
\]

and apply a haircut to \(e_{\text{incentive}}\).

### 6.3 Hedge Mode

Aster explicitly supports:

- `positionSide = LONG`;
- `positionSide = SHORT`;

within the same contract/account when Hedge Mode is active.

This is useful for a business that wants to keep:

- a structural/funding leg;
- and a market-making inventory leg

separate without creating multiple accounts solely for direction.

Example:

- Persistent basis strategy: LONG 5M ETH perp;
- MM fills create: SHORT 2M ETH perp.

In One-Way Mode the position could simply appear as LONG 3M, destroying clean strategy attribution.

In Hedge Mode the two books remain separate.

### 6.4 Critical margin caveat

Hedge Mode is not the same as a free zero-margin hedge.

Aster's margin documentation continues to make total/gross position size relevant to maintenance requirements.

Therefore an equal long and short can have:

\[
\Delta_{\text{net}} \approx 0
\]

while still having:

\[
IM > 0,\qquad MM > 0
\]

on the gross legs.

The benefit is primarily:

- common account equity;
- operational simplicity;
- strategy separation;
- potentially shared liquidation treatment in cross margin;

not an assumption that a $10M long + $10M short consumes the margin of a zero position.

For production risk limits, the actual margin API response should be treated as authoritative for each contract and position mode.

### 6.5 Multi-Asset Mode and productive collateral

Aster's Multi-Asset Mode is particularly relevant because supported assets can contribute collateral value across positions.

Published BSC examples include approximate collateral ratios such as:

| Asset | Illustrative collateral ratio |
|---|---:|
| USDF | ~99.99% |
| asBNB | ~95% |
| BTC / ETH / BNB | ~95% |
| WBETH | ~90% |
| ASTER | ~80% |

The exact current contract/account configuration must be checked before sizing.

### 6.6 USDF as productive collateral

USDF is designed so that capital can participate in Aster's ecosystem yield/reward system while retaining utility as trading margin.

This creates a direct enhancement to MM ROE if:

- the asset remains accepted at a high collateral factor;
- its redemption/depeg risk remains controlled;
- the yield/incentive exceeds the extra tail risk.

However, USDF should not be treated as identical to USDC cash.

Its risk stack includes:

- issuer/platform dependency;
- custody structure;
- backing-strategy risk;
- redemption terms/fees;
- possible depeg;
- auto-conversion behavior under stress.

Aster's published description states that USDF backing uses USDT held with Ceffu and delta-neutral strategies. That is an additional layer of counterparty/strategy dependency relative to simply holding USDC.

### 6.7 asBNB as productive collateral

asBNB is economically more complex because it is a **yield-bearing directional asset**.

It may earn rewards while contributing approximately 95% collateral value, but holding it creates BNB delta.

A market-neutral firm should therefore model:

\[
R_{\text{asBNB}}
=
y_{\text{asBNB}}
-
c_{\text{BNB hedge}}
-
c_{\text{haircut}}
-
L_{\text{basis/tail}}
\]

where the short hedge itself may:

- pay or receive BNB perp funding;
- consume gross margin;
- create liquidation risk;
- fail to track asBNB perfectly.

Advertised yields such as "up to" APY should not be used directly in an underwriting model. A more defensible approach is to run a yield sensitivity grid and subtract hedge carry.

### 6.8 ASTER staking

ASTER can be staked separately for validator/ecosystem rewards. Staking balance also contributes to certain VIP/holding calculations.

This means a required ASTER treasury position may be made more productive:

\[
R_{\text{ASTER treasury}}
=
y_{\text{stake}}
+
\text{fee/VIP benefit}
-
\text{token hedge cost}
-
\text{lock/liquidity cost}
\]

But staked ASTER should not be confused with free trading collateral. If capital is locked in staking, it reduces immediately deployable margin unless the venue specifically credits it as such.

### 6.9 Best use of Hedge Mode at Aster

Hedge Mode is most valuable when:

- one leg is persistent;
- the second leg is tactical/MM inventory;
- strategy accounting needs to remain separate;
- funding attribution matters;
- the firm wants to prevent maker fills from closing a carry leg.

It is often **not** the most capital-efficient choice when both legs are simply temporary MM inventory.

For pure two-sided market making, One-Way Mode lets natural opposite fills net exposure and lower gross inventory.

### 6.10 Aster RWA caveats

Aster lists many RWA markets, but OI and daily turnover are much smaller than trade[XYZ].

The firm should therefore treat Aster RWA as:

- selective edge;
- incentive harvesting;
- opportunistic hedge;
- secondary venue;

rather than assume it can absorb the same RWA inventory as Hyperliquid.

RWA fee schedules and margin parameters may be contract-specific. Crypto-perp MM fees should not automatically be applied to stock/commodity contracts.

### 6.11 Aster-specific risks

- lower liquidity/capacity than Hyperliquid;
- incentive program dependence;
- ASTER token volatility;
- asBNB directional and depeg/wrapper risk;
- USDF backing/custody/redemption risk;
- automatic collateral conversion risk;
- hedge-mode gross IM/MM risk;
- ADL risk;
- API/rate-limit/risk-parameter changes;
- thin RWA books and potentially larger markouts.

---

## 7. Lighter

### 7.1 Standard vs Premium is an execution-cost decision

Lighter creates an unusual trade-off.

**Standard accounts:**

- 0 maker fee;
- 0 taker fee;
- added maker latency;
- added cancel latency;
- added taker latency.

**Premium accounts:**

- no comparable added maker/cancel latency;
- explicit maker/taker fees;
- lower taker latency;
- LIT staking reduces fees.

The relevant equation is not:

\[
0\ \text{fee} < 0.4\text{ bp}
\]

It is:

\[
\text{Premium fee}
<
\text{adverse selection avoided by faster quote/cancel}
\]

If removing ~200 ms of maker/cancel delay improves average markout by 0.5 bp, paying 0.28-0.40 bp maker fee can be rational.

### 7.2 Premium fee and LIT staking

Published Premium baseline fees are approximately:

- maker: 0.40 bp;
- taker: 2.80 bp.

At high LIT staking tiers, fees can fall to approximately:

- maker: 0.28 bp;
- taker: 1.96 bp.

Therefore LIT staking has three economic components:

1. staking APR;
2. fee reduction;
3. funding-payment rebate.

Lighter currently provides a Premium-account funding-payment rebate and an additional staking-linked funding rebate, subject to caps.

### 7.3 LIT is treasury capital, not UTA margin collateral

Lighter's UTA currently centers on USDC collateral for unified Spot + Perpetual margin.

LIT staking is separate.

Therefore:

\[
C_{\text{enterprise}}
=
C_{\text{trading}}
+
S_{\text{LIT}}
\]

If a firm says "LIT staking saves 0.08 bp," it must also count the hundreds of thousands or millions of dollars committed to LIT.

### 7.4 Illustrative 100k LIT calculation

Assume, illustratively:

- 100,000 LIT staked;
- LIT price ~$4.7;
- capital committed ~$470k;
- observed staking APR ~6% (indicative, not guaranteed);
- maker fee improves from 0.40 bp to 0.32 bp;
- annual maker volume = $12B.

Staking yield:

\[
470{,}000 \times 6\%
\approx \$28{,}200
\]

Fee saving versus unstaked Premium:

\[
12B \times 0.08\text{ bp}
=
\$96{,}000
\]

Combined incremental benefit:

\[
\approx \$124{,}200 / \text{yr}
\]

which is ~26% of the illustrative $470k stake before:

- LIT price hedge;
- short-perp funding;
- basis;
- liquidity/unstaking cost;
- tax/operational effects.

But this is **not** a 26% enterprise ROE. The firm must add the staked capital to its total capital base.

Also note: compared with the Standard account, Premium still charges a positive maker fee. The relevant reason to pay it is latency/markout improvement, not staking alone.

### 7.5 Market-neutralizing the LIT stake can eliminate the apparent yield

If the business shorts LIT perp to remove LIT price delta:

\[
R_{\text{hedged LIT}}
=
y_{\text{stake}}
+
\text{fee savings}
+
\text{funding rebate}
+
\text{short funding carry}
-
\text{basis/execution}
\]

If LIT perp funding is negative, the short pays funding.

A current funding snapshot around 8 September 2026 showed extremely negative annualized LIT funding on Lighter. That rate should **not** be extrapolated as a stable annual cost, but it demonstrates that a nominal 6% staking APR can be economically irrelevant if the delta hedge is expensive.

### 7.6 UTA margin structure

Lighter's Unified Trading Account allows eligible Spot and Perpetual positions to share USDC equity.

That helps PnL transfer within the account, but published margin requirements are based on the sum of position-level gross requirements.

So:

\[
MM_{\text{portfolio}}
\approx
\sum_i |N_i|m_i
\]

rather than a covariance-netted VaR engine.

### 7.7 Same-contract long and short

Lighter exposes a signed position per market and does not currently document an Aster-style Hedge Mode.

Consequently, two independent same-contract directions generally require separate subaccounts.

That recreates the same issue as Hyperliquid:

- global delta can be zero;
- account-level margin remains fragmented.

### 7.8 RWA cross-margin restriction

Lighter's RWA markets are a credible second pool of liquidity, especially in gold, oil and major equity/index products.

However, current documentation states that **opening RWA positions under Cross Margin is restricted** while the product remains experimental/volatile.

Thus Lighter's UTA capital-efficiency advantage is currently much less relevant to a new RWA MM position than it is to ordinary crypto perps.

### 7.9 Lighter-specific risks

- latency-induced adverse selection in Standard;
- positive explicit fees in Premium;
- LIT staking opportunity cost/token risk;
- funding volatility on LIT hedge;
- sequencer/API availability;
- LLP/liquidation-backstop and ADL risk;
- gross-notional margin requirements;
- RWA cross-margin restrictions;
- oracle/market-hours divergence in RWA.

---

# Part II — Profitability

## 8. Execution P&L

For a maker fill of notional \(V\), define net execution edge:

\[
e_{\text{execution}}
=
e_{\text{spread}}
+
e_{\text{rebate}}
-
e_{\text{markout}}
-
h(f_{\text{taker}}+s_{\text{hedge}})
-
e_{\text{other}}
\]

where \(h\) is the fraction of maker flow that must be aggressively hedged rather than recycled passively.

This is the most important variable in crypto MM.

### 8.1 Reasonable underwriting targets

These are **target contribution margins**, not guaranteed historical returns:

| Market class | Sustainable net execution target before collateral/staking yield |
|---|---:|
| BTC / ETH / SOL-like majors | ~0.1-0.5 bp |
| Liquid secondary crypto | ~0.3-1.0 bp |
| Mid/long-tail crypto | ~0.5-2.0 bp |
| Thin/event-driven crypto | >2 bp possible, but capacity/tail risk deteriorate quickly |
| Gold / major RWA indices | ~0.5-2.0 bp |
| Oil / liquid single stocks | ~1-3 bp |
| Secondary equities / ETFs | ~2-5 bp |
| Thin RWA / long-tail products | >3-8 bp possible, usually with discontinuous tail risk |

A business plan should use realized **post-markout, post-hedge** edge, not quoted spread.

---

## 9. Funding and basis carry

Market neutrality does not imply carry neutrality.

If the business is:

- short perp on venue S;
- long same underlying on venue L;

then, approximately:

\[
P\&L_{\text{funding}}
=
N(f_S-f_L)
\]

with sign conventions adjusted to each venue.

A cross-venue MM should therefore solve two decisions independently:

1. Where is it optimal to quote?
2. Where is it optimal to warehouse the hedge?

The hedge venue can change dynamically with:

- funding differential;
- taker fee;
- depth;
- collateral utilization;
- liquidation buffer;
- basis/oracle divergence.

### 9.1 Holding-time vs turnover economics

Funding income scales with **average held notional and time**.

Spread/rebate P&L scales with **filled volume**.

Do not add annualized funding APR directly to per-fill basis points.

Correct decomposition:

\[
\Pi_{\text{annual}}
=
V_{\text{annual}} e
+
\bar N_{\text{annual}} c
\]

where \(e\) and \(c\) have different denominators.

---

## 10. How much is collateral yield worth in basis points?

If annual maker turnover is:

\[
T = \frac{V_{\text{annual}}}{C_y}
\]

then collateral yield \(y\) is equivalent to:

\[
e_{\text{yield,bp}}
=
\frac{10{,}000\,y}{T}
\]

### Yield-equivalent edge

| Collateral yield | 600x annual turnover | 1,200x | 2,400x |
|---:|---:|---:|---:|
| 2% | 0.333 bp | 0.167 bp | 0.083 bp |
| 5% | 0.833 bp | 0.417 bp | 0.208 bp |
| 10% | 1.667 bp | 0.833 bp | 0.417 bp |

This explains why productive collateral matters.

A 2-5% yield looks small as an APY, but 0.1-0.4 bp of effective edge is very significant in a major-coin MM strategy whose execution alpha may only be 0.2-0.5 bp.

At very high turnover the effect per filled dollar is diluted, but it still directly increases ROE.

---

## 11. $5M-capital scenario

Assume:

- enterprise trading capital: $5M;
- maker fills: $1B/month;
- annual maker volume: $12B;
- annual turnover: 2,400x.

### 11.1 Execution edge only

| Net execution edge | Annual P&L | ROE on $5M before fixed/tail costs |
|---:|---:|---:|
| 0.10 bp | $120k | 2.4% |
| 0.30 bp | $360k | 7.2% |
| 0.50 bp | $600k | 12.0% |
| 1.00 bp | $1.20M | 24.0% |
| 2.00 bp | $2.40M | 48.0% |

This demonstrates why a small change in markout, fee or rebate is economically dominant.

### 11.2 Hyperliquid current USDC supply-yield illustration

If the full $5M could continuously earn the current ~2.84% USDC supply APY:

\[
5M \times 2.84\% \approx \$142k
\]

Equivalent execution edge on $12B fills:

\[
\frac{142k}{12B}\times 10{,}000
\approx 0.118\text{ bp}
\]

If only 50% of the capital is continuously yield-eligible:

\[
\approx \$71k
\]

or about 0.059 bp equivalent.

For a BTC/ETH strategy earning only 0.3 bp execution edge, this is material.

### 11.3 Aster collateral-yield sensitivity

Because productive-collateral yields vary, use a sensitivity rather than a promotional headline.

If $5M economic value is in productive collateral:

| Gross collateral yield | Gross annual yield | Equivalent bp on $12B maker volume |
|---:|---:|---:|
| 3% | $150k | 0.125 bp |
| 5% | $250k | 0.208 bp |
| 10% | $500k | 0.417 bp |

For asBNB, subtract:

- collateral haircut;
- BNB hedge funding;
- hedge fees;
- basis;
- wrapper/liquidity tail reserve.

For USDF, subtract a stablecoin/custody/redemption risk charge.

### 11.4 Why yield can change the venue ranking

Suppose:

- Hyperliquid execution edge = 0.30 bp;
- Aster execution edge/rebate package = 0.45 bp;
- Aster productive-collateral contribution = 0.20 bp equivalent;
- Hyperliquid collateral contribution = 0.06-0.12 bp equivalent.

Then:

\[
e_{\text{HL,total}}
\approx 0.36-0.42\text{ bp}
\]

\[
e_{\text{Aster,total}}
\approx 0.65\text{ bp}
\]

Aster can win on **unit economics** even while Hyperliquid wins on **capacity**.

The correct optimization is therefore:

\[
\max \left(
\text{edge} \times \text{capacity}
\right)
\]

subject to risk and capital constraints, not simply "choose the venue with the best fee."

---

# Part III — Market-neutral architecture

## 12. Consolidated delta neutrality

For economically matched perp legs:

\[
\Delta_{\text{portfolio}}
=
\sum_i q_i M_i \Delta_i
\approx 0
\]

For identical linear crypto perps, this is approximately notional matching.

For RWA it may not be.

Examples:

- SPY perp vs S&P-500 index perp;
- QQQ vs Nasdaq-100 index;
- WTI contracts with different roll/reference conventions;
- stock feeds with different pre-market/after-hours handling;
- different dividend/corporate-action adjustments.

The hedge ratio should therefore be:

\[
h^*
=
\arg\min_h
Var(r_A - h r_B)
\]

rather than blindly 1:1 if the contracts are not identical.

---

## 13. Same-venue hedge mode versus net mode

### 13.1 Pure MM inventory

For pure MM, net mode is usually superior.

Natural buyer/seller flow reduces inventory without crossing the spread.

Example:

- initial position: 0;
- sell maker fill: -$2M;
- buy maker fill: +$1.5M;
- resulting position: -$0.5M.

No explicit hedge trade was needed for 75% of the original inventory.

### 13.2 Persistent carry + MM overlay

Hedge mode becomes more valuable when there is a persistent carry book.

Example:

- structural LONG 5M;
- market-making engine becomes SHORT 2M.

Aster Hedge Mode can preserve both.

In a net system:

\[
+5M - 2M = +3M
\]

and the market-making activity has partially closed the carry position.

On Hyperliquid/Lighter, preserving both same-contract legs generally requires two subaccounts, which fragments margin.

### 13.3 Decision rule

Use Hedge Mode / multiple subaccounts only if:

\[
\text{value of strategy separation}
>
\text{extra gross margin}
+
\text{collateral fragmentation}
+
\text{operational complexity}
\]

For most short-horizon MM inventory, this inequality will **not** hold.

For funding/carry books that should not be netted away, it often will.

---

## 14. Cross-venue market neutrality

No venue-level portfolio-margin system can offset a position held on another venue.

Example:

- Hyperliquid: SHORT $10M BTC;
- Aster: LONG $10M BTC.

Consolidated delta:

\[
\Delta_{\text{global}} \approx 0
\]

But local accounts see:

\[
\Delta_{\text{HL}}=-10M,\qquad
\Delta_{\text{Aster}}=+10M
\]

A temporary 5% cross-venue mark/basis divergence can create approximately:

- -$500k mark impact on one margin account;
- +$500k on the other.

The winning account's PnL cannot automatically rescue the losing venue.

Therefore each venue needs:

\[
E_i - MM_i
>
B_i^{\text{basis stress}}
+
B_i^{\text{oracle stress}}
+
B_i^{\text{transfer freeze}}
\]

where \(E_i\) is account equity and \(MM_i\) maintenance margin.

This is the single most important risk constraint in a multi-DEX market-neutral book.

---

## 15. Collateral transfer risk

A cross-venue MM should model capital as **pre-positioned liquidity**, not as one fungible global balance.

Potential failure sequence:

1. Venue A mark diverges.
2. Margin buffer on A falls rapidly.
3. Venue B has matching unrealized profit.
4. Withdrawal from B is delayed, throttled or operationally unavailable.
5. Bridge/chain/stablecoin route is congested.
6. Venue A liquidates before capital arrives.

The firm was "market neutral" and still lost money.

For this reason, the optimal collateral level is not minimum IM. It is a stress-based venue buffer.

---

# Part IV — Crypto vs RWA

## 16. Crypto-perp opportunity

### Hyperliquid

Best for:

- BTC/ETH/SOL and other high-capacity books;
- passive inventory recycling;
- high-turnover execution;
- primary price discovery.

Economics:

- low maker fees / rebates at scale;
- high capacity;
- PM idle-collateral yield where operationally compatible;
- HYPE staking adds separate yield + fee discounts.

Main constraint:

- serious HFT may prefer Standard Mode, reducing access to some PM economics in the execution account;
- same-market independent long/short needs separate subaccounts.

### Aster

Best for:

- qualified rebate-heavy MM;
- strategies benefiting from productive collateral;
- persistent carry + MM overlay using Hedge Mode;
- selective markets where competition is lower.

Economics:

- up to ~0.5 bp maker rebate in current qualified MM program;
- yield-bearing collateral can add 0.1-0.4+ bp equivalent depending turnover/yield;
- Hedge Mode avoids creating separate subaccounts merely for long/short separation.

Main constraint:

- lower liquidity than Hyperliquid;
- incentive dependency;
- collateral-token risk.

### Lighter

Best for:

- strategies that can tolerate Standard latency and exploit zero fees;
- strategies where Premium's lower latency materially improves markout;
- teams willing to optimize LIT stake versus fee/funding economics.

Main constraint:

- Premium maker fee can consume a large fraction of major-coin MM edge;
- LIT stake is additional treasury capital, not margin;
- no documented same-account Hedge Mode.

### Crypto-perp ranking

**Capacity-adjusted business quality:**

1. Hyperliquid
2. Aster
3. Lighter

**Potential unit economics for a qualifying MM with productive collateral:**

1. Aster
2. Hyperliquid
3. Lighter, with the caveat that Standard may beat both on selected flow if latency toxicity is low

**Same-account strategy separation:**

1. Aster
2. Hyperliquid / Lighter only via separately margined subaccounts

---

## 17. RWA-perp opportunity

RWA MM has wider apparent spreads but a worse tail-risk structure.

### Structural risks unique to RWA

- cash-market closure while perp trades 24/7;
- pre-market/after-hours feed differences;
- weekend gaps;
- earnings;
- dividends;
- stock splits;
- mergers/tenders;
- trading halts;
- ETF NAV/reference mismatch;
- index rebalances;
- commodity roll conventions;
- futures limit moves;
- oracle lags;
- stale reference markets.

A quoted 3 bp spread in an RWA perp is not necessarily more profitable than a 0.3 bp crypto spread if a single opening gap erases months of spread capture.

### Hyperliquid / trade[XYZ]

Strength:

- overwhelmingly best RWA OI and daily volume.

Weakness:

- current isolated-only market architecture means substantially worse capital pooling than the headline Portfolio Margin feature suggests.

Implication:

- best **capacity**;
- not necessarily best **margin efficiency**.

### Lighter

Strength:

- credible second venue for gold, oil, indices and equities;
- enough depth to serve as a hedge venue in selected products.

Weakness:

- opening RWA positions under Cross Margin is currently restricted;
- therefore new RWA positions cannot fully exploit UTA cross-margin efficiency.

### Aster

Strength:

- flexible account architecture;
- productive collateral;
- Hedge Mode;
- many RWA listings.

Weakness:

- much lower current RWA OI/volume;
- contract-specific margin/fee restrictions;
- potentially poor exit depth during stress.

### RWA ranking

**Liquidity/capacity:**

1. Hyperliquid trade[XYZ]
2. Lighter
3. Aster

**Account-feature potential:**

1. Aster, contract permitting
2. Hyperliquid / Lighter constrained by current RWA isolation/cross restrictions

This produces a useful paradox:

> The venue with the best RWA liquidity currently has weaker RWA margin pooling, while the venue with the richest account features has much less RWA liquidity.

That is why RWA profitability should be optimized at the **portfolio level**, not venue by venue.

---

# Part V — Risk framework

## 18. Risk register

| Risk | Hyperliquid | Aster | Lighter | Crypto severity | RWA severity |
|---|---|---|---|---:|---:|
| Cross-venue basis | High | High | High | High | Very high |
| Oracle divergence | Medium | Medium | Medium | Medium | **Very high** |
| Local liquidation despite global neutrality | High | High | High | High | High |
| Same-market long/short margin fragmentation | High if using 2 subaccounts | Lower operationally via Hedge Mode | High if using 2 subaccounts | Medium | Medium |
| Gross margin not fully risk-netted | Yes | Yes / do not assume offset | Yes | Medium | High |
| Yield-bearing collateral token risk | PM lend/USDC + HYPE treasury | **USDF/asBNB/ASTER** | LIT treasury | Medium | Medium |
| Stablecoin depeg | USDC/USDT-family exposure | USDF/USDT and others | USDC | Medium | High under stress |
| Native-token hedge funding | HYPE | ASTER/BNB | LIT | Medium-high | Medium |
| ADL/liquidation backstop | Yes | Yes | Yes | High in stress | High |
| Market-hours gap | Low for crypto | RWA | RWA | Low | **Very high** |
| Program/incentive change | Medium | **High** | Medium | Medium | Medium |
| API/latency | Medium | Medium | **High distinction Standard/Premium** | High | Medium |
| RWA isolated/cross restriction | **High** | Contract-specific | **High** | N/A | **High** |

---

## 19. Productive collateral can create wrong-way risk

Yield is not independent of market risk.

### asBNB example

Suppose the firm uses asBNB as collateral and shorts BNB perp to neutralize delta.

A stress can simultaneously produce:

- asBNB/BNB wrapper discount;
- negative BNB short funding;
- falling collateral factor;
- rising perp maintenance margin;
- lower venue liquidity.

The hedge can be directionally correct and still fail as a collateral hedge.

### USDF example

A stablecoin collateral event can create:

- depeg;
- lower collateral valuation;
- redemption friction;
- forced collateral conversion;
- simultaneous market volatility.

### HYPE / LIT / ASTER staking

If native-token staking is hedged with a short perp, the business adds:

- funding risk;
- basis risk;
- liquidation risk;
- unstaking delay;
- venue concentration.

The correct quantity is **hedged staking carry**, not advertised APR.

---

## 20. Funding-risk stress

For every staked directional token:

\[
R_{\text{hedged stake}}
=
y_s
+
b_{\text{fee}}
+
b_{\text{funding rebate}}
+
f_{\text{short}}
-
c_{\text{basis}}
-
c_{\text{execution}}
\]

Run at least these funding scenarios:

- normal funding;
- zero funding;
- short pays 10% annualized;
- short pays 25% annualized;
- short pays 100% annualized for a short acute period.

The last case may sound extreme, but perp funding on native tokens can become highly dislocated during crowded positioning.

---

## 21. RWA stress tests

At minimum, model:

### Major index / gold

- 2-5% reference move;
- 50-150 bp cross-venue oracle divergence;
- one venue stale for several minutes;
- liquidity down 70%;
- funding flips sign.

### Single stocks

- 10-20% earnings gap;
- trading halt;
- corporate-action adjustment mismatch;
- 2-5% venue-oracle basis;
- one-sided book.

### Oil

- large gap around inventory/geopolitical shock;
- front-month roll mismatch;
- reference-futures basis change;
- negative/abnormal futures-price regime as a historical tail scenario.

The margin buffer should be sized from the worst **local** venue loss, not consolidated P&L.

---

# Part VI — Recommended portfolio design

## 22. Capital buckets

The cleanest economic framework is to divide capital into four buckets.

### A. Execution margin

Capital that must remain immediately available for:

- maker inventory;
- short-term hedges;
- liquidation buffers.

Primary venues:

- Hyperliquid Standard for high-frequency crypto;
- Aster Cross/Multi-Asset where appropriate;
- Lighter Standard/Premium according to latency economics.

### B. Yielding collateral

Capital that remains margin-capable while earning:

- Hyperliquid PM supply yield;
- Aster supported productive collateral yield/rewards.

This capital should receive a risk haircut.

### C. Staked treasury capital

Examples:

- HYPE;
- ASTER;
- LIT.

Count this in enterprise ROE.

Do not hide it outside the denominator simply because it is not in the perp account.

### D. Emergency liquidity

Stable, rapidly movable capital held to recapitalize a venue during basis divergence.

This bucket may have a lower yield, but it buys survival.

Its value is analogous to an option:

\[
\text{liquidity reserve cost}
<
\text{expected liquidation/tail-loss avoided}
\]

---

## 23. Suggested functional role for each venue

### Hyperliquid

**Crypto:**
- primary price discovery;
- primary capacity venue;
- inventory recycling;
- PM/borrow-lend for slower or treasury capital where action constraints permit.

**RWA:**
- primary quoting venue because of OI/volume;
- size positions assuming isolated local liquidation risk.

### Aster

**Crypto:**
- rebate/incentive satellite;
- productive-collateral venue;
- best place of the three for same-account persistent long + short strategy separation.

**RWA:**
- selective high-edge markets;
- only where depth and contract-specific margin rules justify it.

### Lighter

**Crypto:**
- secondary quote/hedge venue;
- choose Standard vs Premium by observed markout, not headline fee;
- use LIT stake only when fee/funding benefit exceeds capital and hedge cost.

**RWA:**
- second-liquidity venue;
- especially useful for gold/oil/major equity/index hedges;
- price current cross-margin restriction into capital allocation.

---

## 24. Routing principle

For every maker fill, the hedge engine should conceptually choose among:

1. wait for natural opposite maker flow;
2. place passive hedge on same venue;
3. place passive hedge on another venue;
4. cross aggressively on another venue;
5. retain inventory because funding/basis carry is favorable.

The expected value of an immediate hedge on venue \(j\) is:

\[
EV_j =
-\text{taker fee}_j
-\text{slippage}_j
+\text{expected funding advantage}_j
+\text{basis convergence}_j
-\text{incremental liquidation cost}_j
\]

A "market-neutral" business that mechanically taker-hedges every maker fill can destroy its own spread capture.

The profitable MM minimizes **hedge urgency** while constraining inventory risk.

---

# Part VII — What should be measured in production economics

## 25. Venue-level KPIs

For each venue and symbol track:

- maker filled notional;
- maker participation share;
- quoted spread;
- realized spread at 100 ms / 1 s / 5 s / 30 s;
- adverse-selection markout;
- maker rebate;
- taker hedge fraction;
- taker fee;
- hedge slippage;
- inventory half-life;
- funding P&L;
- basis P&L;
- collateral yield;
- staking yield;
- incentive/token rewards;
- average IM;
- peak MM utilization;
- minimum local liquidation buffer;
- capital transfer time;
- API rejection/rate-limit rate;
- cancel-to-fill latency;
- venue downtime.

### Core profitability KPI

\[
\text{Net edge per maker bp}
=
\frac{
P\&L_{\text{execution}}
+
P\&L_{\text{funding}}
+
P\&L_{\text{yield allocated}}
+
P\&L_{\text{rewards allocated}}
}{
V_m
}
\times 10{,}000
\]

Also report:

\[
ROE_{\text{enterprise}}
=
\frac{\Pi}{C_{\text{trading}}+C_{\text{staked}}+C_{\text{emergency}}}
\]

This prevents apparently high-performing strategies from hiding large amounts of dormant staking or emergency capital.

---

# Part VIII — Final assessment

## 26. Hyperliquid

**Strengths**

- largest crypto capacity;
- dominant RWA capacity through trade[XYZ];
- excellent inventory recycling;
- high-volume maker economics;
- HYPE staking gives fee discount + staking return;
- PM idle stable collateral can now earn meaningful floating supply yield.

**Weaknesses**

- staked HYPE is not trading collateral;
- PM is not a blanket zero-margin risk-netting engine;
- HFT Standard Mode can be operationally preferable to PM;
- simultaneous independent same-contract long/short requires separately margined subaccounts;
- trade[XYZ] RWA isolation substantially reduces PM capital efficiency.

**Best role:** core crypto venue and core RWA liquidity venue, with treasury/PM economics treated separately from high-frequency execution when necessary.

---

## 27. Aster

**Strengths**

- explicit Hedge Mode;
- productive collateral;
- strong qualified-MM maker rebates;
- ASTER stake can contribute to broader fee/VIP economics;
- strong unit-economics potential.

**Weaknesses**

- lower capacity than Hyperliquid;
- Hedge Mode does not imply zero gross margin;
- collateral yield introduces USDF/asBNB-specific risk;
- incentives can change;
- RWA liquidity is currently small.

**Best role:** highest-potential capital-efficiency / rebate satellite for crypto; selective RWA venue; best of the three when preserving separate same-contract long/short books in one account is strategically important.

---

## 28. Lighter

**Strengths**

- zero-fee Standard option;
- strong infrastructure/settlement design;
- meaningful RWA second-venue liquidity;
- LIT staking improves Premium fee and funding economics.

**Weaknesses**

- Standard latency is an implicit fee paid through markout;
- Premium charges explicit maker fee;
- LIT is separate capital, not UTA collateral;
- native-token hedge funding can dominate staking APR;
- no documented Aster-style Hedge Mode;
- RWA cross opening is currently restricted.

**Best role:** secondary crypto quote/hedge venue and second RWA liquidity venue; Premium should be justified quantitatively by latency alpha.

---

## 29. Overall ranking after adding staking and margin architecture

### Crypto — scalable capacity

1. **Hyperliquid**
2. **Aster**
3. **Lighter**

### Crypto — potential unit economics / capital productivity

1. **Aster**, if MM-qualified and productive collateral is risk-adjusted correctly
2. **Hyperliquid**
3. **Lighter**, with strong symbol-specific exceptions

### Same-contract independent long + short

1. **Aster Hedge Mode**
2. **Hyperliquid / Lighter via separate subaccounts**, with materially worse margin fragmentation

### RWA — capacity

1. **Hyperliquid / trade[XYZ]**
2. **Lighter**
3. **Aster**

### RWA — practical capital efficiency

No venue deserves a blanket "portfolio-margin" advantage today:

- trade[XYZ]: high liquidity, but current isolated-only structure;
- Lighter: useful liquidity, but new RWA cross-margin openings restricted;
- Aster: richest account/collateral feature set, but contract-specific restrictions and much less liquidity.

The economically correct choice is therefore market-specific.

---

# 30. Bottom line

The business should optimize three layers simultaneously:

\[
\boxed{
\text{MM return}
=
\text{execution alpha}
+
\text{carry}
+
\text{capital yield}
-
\text{tail-risk cost}
}
\]

The addition of staking/collateral yield makes **Aster considerably more competitive** than a fee-only analysis suggests.

The addition of portfolio/hedge-mode mechanics also changes the conclusion:

- **Aster** has the cleanest same-account mechanism for maintaining an independent long and short in one contract.
- **Hyperliquid** has the strongest liquidity and now valuable PM/borrow-lend economics, but same-contract dual legs need separate subaccounts and most trade[XYZ] RWA markets remain isolated.
- **Lighter** shares collateral through UTA but still calculates gross margin at position level, and its RWA cross-margin restriction limits current capital efficiency.

For a market-neutral multi-DEX business, the highest-quality architecture is therefore not simply "one long and one short."

It is:

1. use **net inventory** wherever natural maker flow can recycle risk;
2. preserve distinct long/short books only when strategy separation has economic value;
3. price the **gross margin cost** of hedge mode or multiple subaccounts;
4. collect **yield on eligible collateral** without pretending the yield is risk-free;
5. include **staked native-token capital in ROE**;
6. hedge staking-token delta only when the funding/basis cost does not destroy the stake yield/fee benefit;
7. maintain a **venue-specific liquidation buffer** even when consolidated delta is zero;
8. treat RWA as a separate risk book because market-hours/oracle gaps dominate the tail.

The main competitive moat is therefore not the quoting algorithm in isolation. It is the ability to coordinate:

- execution edge;
- inventory recycling;
- cross-venue basis;
- funding;
- collateral yield;
- staking economics;
- margin mode;
- local liquidation probability;
- and capital transfer risk

as one consolidated balance-sheet problem.

---

# Sources and current-reference links

The following sources were used for the venue mechanics and current snapshots. Dynamic pages and live parameters should be checked again before deployment or capital allocation.

## Market data

- DeFiLlama, normalized perp volume: https://defillama.com/normalized-volume
- DeFiLlama, perpetual exchanges: https://defillama.com/perps
- DeFiLlama, RWA perps: https://defillama.com/rwa/perps
- DeFiLlama, trade[XYZ] RWA venue: https://defillama.com/rwa/perps/venue/trade-xyz
- DeFiLlama, Lighter RWA venue: https://defillama.com/rwa/perps/venue/lighter
- DeFiLlama, Aster RWA venue: https://defillama.com/rwa/perps/venue/aster

## Hyperliquid

- Fees and maker rebates: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
- HYPE staking: https://hyperliquid.gitbook.io/hyperliquid-docs/hypercore/staking
- Account modes / Portfolio Margin: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes
- Margining: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining
- Subaccounts: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/subaccounts
- HIP-3 builder-deployed perps: https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-3-builder-deployed-perpetuals
- Funding: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding

## Aster

- Market-maker program: https://docs.asterdex.com/product/aster-perpetual-pro/market-maker-program
- Hedge Mode / position mode: https://docs.asterdex.com/product/aster-perpetual-pro/trading/position-mode
- Multi-Asset Mode: https://docs.asterdex.com/product/aster-perpetual-pro/trading/multi-assets-mode
- Trade & Earn / yield-bearing collateral: https://docs.asterdex.com/earn/trade-and-earn
- USDF: https://docs.asterdex.com/earn/usdf
- Aster staking: https://docs.asterdex.com/aster-token/staking
- VIP program: https://docs.asterdex.com/product/aster-perpetual-pro/vip-program
- Auto-deleveraging: https://docs.asterdex.com/trading/perpetuals/fees-and-specs/auto-deleveraging-adl

## Lighter

- Trading fees / Standard vs Premium: https://docs.lighter.xyz/trading/trading-fees
- Unified Trading Account: https://docs.lighter.xyz/trading/unified-trading-account
- Margin and liquidations: https://docs.lighter.xyz/trading/margin-and-liquidations
- LIT utility/staking: https://docs.lighter.xyz/lit-token/lit-utility
- Funding rebates: https://docs.lighter.xyz/trading/funding
- RWA markets: https://docs.lighter.xyz/trading/rwa-markets
- Technical architecture: https://docs.lighter.xyz/about-lighter/technical-architecture-lighter-core

---

## Source-quality note

Some venue documentation is updated faster than search caches. In particular, portfolio-margin limits, collateral caps, RWA cross-margin availability, incentive tiers and live funding/supply APYs are dynamic. Where a live venue configuration or API response conflicts with an older documentation cache, the live risk configuration should be treated as authoritative.
