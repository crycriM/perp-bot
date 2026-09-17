# perp-bot

Perp CLOB market-making bot: the perp-side **keeper and backtester** in the
`amm-solution` architecture. It decides *what state to reach* — quote prices
and sizes, target inventory, de-risk urgency — and emits `ExecIntent`s to an
OPMS execution layer. It never emits exchange-native order verbs and holds no
exchange credentials.

The strategy it implements is a bidirectional Avellaneda-Stoikov market-making
design for perp orderbooks. All shared math and policy (AS quotes, regime,
risk, PnL, markout) lives in the `mm-core` library, and the detailed strategy
and architecture documentation lives in sibling repos — see
[References](#references) for what is public and what is not. This repo is
the perp keeper loop and the event-replay backtester built on `mm-core`.

## Role in the system

```text
mm-core (brain: AS, regime, risk, PnL, markout)
     ▲
perp-bot (keeper + backtester) ──ExecIntent──▶  OPMS (body)  ──▶  venue adapter
                                                 │
                                                 ├── dex_executor     (legacy: standalone FastAPI service — REST + WS)
                                                 └── hb-enhanced-opms (migration: HB-backed Python package — in-process)
```

The keeper loop, one `PerpPairConfig` at a time:

1. ingests normalized **market-data** and **fills** — OPMS websockets on the
   service path, in-process pushes from `PerpMMController` on the HB path
   (fills are subaccount-scoped)
2. reconciles inventory against authoritative OPMS position snapshots every
   tick — the keeper itself holds no durable state; OPMS (and the exchange)
   is the truth
3. evaluates regime and markout via `mm_core`
4. runs the shared risk policy: quote / widen / stop quoting / de-risk / emergency exit
5. emits `ExecIntent`s (quote specs via `QuoteSpec`, target-inventory
   corrections, emergency flattens) — OPMS owns how they execute

The execution body is deliberately swappable, with two implementations of
the same role:

- **Legacy — `dex_executor`, service + API (status quo).** A standalone
  FastAPI service (port 8000) that owns the venue adapters and a REST/WS
  surface (`/api/v1/intents`, `/ws/md/...`, `/ws/fills/...`). The keeper
  talks to it over HTTP/WS through `perp_bot.opms_client.OpmsClient`. This
  is what the live keeper loop is pointed at today, and the only path wired
  end-to-end so far.
- **Migration target — `hb-enhanced-opms`, HB-backed Python package.** Not a
  service: a regular Python package (conda env, since Hummingbot is not on
  PyPI) whose `PerpMMController` hosts an *unmodified* `Keeper` in-process
  each Hummingbot control cycle. `InProcessClient`
  (`opms.controllers.generic.perp_mm_bridge`) duck-types `OpmsClient`'s
  callback/query surface and is fed directly from Hummingbot's candles and
  positions; the controller translates the keeper's `ExecIntent`s into
  Hummingbot executor actions (active-cancel / passive-aggressive). No REST
  hop, no service process.

Both execute the same `Keeper` and share the intent/fill/position contract,
so perp-bot is agnostic about which one handles a pair. Decision-log parity
(`replay_decision_log.py` + `diff_decision_logs.py`) is the gate that keeps
the two paths byte-identical: an `InProcessClient`-driven keeper must produce
the same decision records as a bare keeper given the same inputs. Until the
HB path is wired end-to-end, live runs use `dex_executor`.

## The strategy, in brief

Bidirectional perp MM posts resting bids and asks around fair value. A bid
fill opens a **long perp**, an ask fill opens a **short perp** — net
inventory is a single signed delta, so there is no token custody risk in the
way there is for spot LP: risk is mark-to-market on a perpetual position.

The bot earns on three edges:

1. **Spread capture** — the primary edge: buy at bid, sell at ask.
2. **Maker rebate** — small (≤ 0.5 bps at top tiers); a tiebreaker, never a
   reason to keep a quote up.
3. **Funding rate** — secondary; inventory oscillating around zero keeps
   funding drag minimal, and a structural tilt (`target_inventory`) can
   harvest favorable funding regimes.

Inventory control is Avellaneda-Stoikov. `mm_core.as_core` implements the
simplified **stationary (T→∞)** form — constant spread plus linear inventory
tilt around a configured target:

```text
reservation_price_shift = (q − q_target) · γσ² / (2κ)
optimal_spread          = γσ² / (2κ) + (2/γ) · ln(1 + γ/κ)
```

`q` is signed net inventory, `γ` risk aversion, `σ` realized volatility, `κ`
fill-intensity decay. **`γ`/`κ` are scale-dependent on the pair's price and
volatility — calibrate them on the pair's own data with `Backtest` before a
live run; never copy fixture or default values.** The full GLFT asymptotics
(drift term `q* ≈ μ/(γσ²)`) and fair-value `Ŝ` beyond the raw mid are the
documented extensions of this model; the stationary form above is what the
code implements today.

Risk management: regime gating (quiet vs widened vs no-quote), hard
inventory caps that flip quoting to reduce-only, **fail-closed margin
health** (a missing, malformed, or non-finite `margin_available` is treated
as zero available margin and triggers `EMERGENCY_EXIT` rather than disabling
the stop), markout tracking for toxic flow, and — for multi-account
deployments — the basket rebalancer below.

The backtester enforces these rollout gates before a pair goes live: **net edge > 2.0 bps,
markout ratio < 0.5, max drawdown < 5%, zero liquidations**. Rollout
sequence: backtest → shadow → testnet live → micro mainnet.

## Features

- **Live keeper** (`Keeper`) driven by OPMS REST + WS, with reconnect and
  position resnapshot on drift or disconnect
- **Shadow mode** (`shadow_mode=True`): full decision cycle runs, intents are
  written to the decision log but never posted — the paper-trading step before going live
- **Event-replay backtester** (`Backtest`) for HL-style snapshots, trade
  prints, and discrete funding events, with pluggable strategies
  (`Strategy` = Guéant-AS, `BaselineStrategy` = fixed symmetric spread)
- **Single PnL accounting path**: live keeper and backtester both use
  `mm_core.pnl.PnLLedger` — realized/unrealized, spread capture, markout,
  funding, fees; never two competing calculations
- **Decision logging** (JSONL, one record per tick with the validated margin
  reading and emitted intent) plus **replay** and **diff** scripts for
  comparing backtest vs shadow artifacts
- **Account topology validation** (`validate_account_topology`): netted
  venues (Hyperliquid, Lighter) allow exactly one keeper per
  `(exchange, coin, account_id)`; hedge-capable venues (Aster) may share
  accounts
- **Venue capability registry** (`venue_capabilities`): position mode and
  same-account hedge support per venue, enforced at config time
- **Basket rebalancer** (`BasketRebalancer`): a slower portfolio-level netting
  loop that corrects per-subaccount imbalance and flags portfolio-level per-coin
  net exposure, with a gross-notional capacity guard; shadow-capable
- **Hyperliquid historical data fetcher** for the backtester

## Package layout

```text
src/perp_bot/
  backtest.py            # event-replay backtester, strategies, rollout gates
  config.py              # PerpPairConfig
  keeper.py              # live keeper loop, shadow mode, decision log;
                         # also hosted unmodified by hb-enhanced-opms' PerpMMController
  margin_health.py       # fail-closed margin_available handling
  opms_client.py         # OpmsClient: REST + WS client for the legacy service OPMS
                         # (reconnect, resnapshot); duck-typed by hb-enhanced-opms'
                         # InProcessClient on the in-process path
  rebalancer.py          # portfolio netting / rebalancing controller
  rebalancer_feeds.py    # OPMS-backed position/price/intent providers
  topology.py            # netted/hedged deployment validation
  venue_capabilities.py  # per-venue position mode, hedge support
scripts/
  fetch_hl_data.py       # pull HL candles/trades/funding into CSVs (direct REST)
  run_backtest.py        # backtest off the fetched CSVs, print the gate report
  run_shadow.py          # bounded shadow run of the live keeper
  run_rebalancer_shadow.py
  replay_decision_log.py # replay a decision log against a controller config
  diff_decision_logs.py  # compare two decision logs (e.g. backtest vs shadow)
tests/
```

## Dependencies

- Python ≥ 3.11
- `mm-core` (sibling project — installed editable)
- `aiohttp` ≥ 3.9 (client for the legacy service OPMS only — not needed on
  the HB-backed in-process path)
- dev: `pytest`, `pytest-asyncio`
- the historical fetch script uses `requests` directly (no HL SDK required)

## Installation

Per repo convention, one venv per project — never the system Python:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ../mm-core
pip install -e .
```

## Quick start

The steps below follow the single path that is wired end-to-end today:
the legacy service `dex_executor` + `OpmsClient`. Step 1 backtests
standalone; only steps 2 and 3 need the service. See step 3 for the
HB-backed in-process variant.

**0 — Start the legacy OPMS service** (needed for steps 2 and 3): run
`dex_executor` with the target account configured in the execution service
environment, with testnet mode enabled before starting:

```bash
uvicorn opms.service.app:app --host localhost --port 8000 --app-dir src
```

**1 — Backtest first** (runs standalone — no OPMS needed; calibrate `γ`/`κ`
and check the rollout gates):

```bash
python scripts/fetch_hl_data.py ETH --days 30 --interval 15m
python scripts/run_backtest.py ETH --gamma 1.0 --kappa 0.5 --max-position 0.05
```

**2 — Shadow run** (needs the step-0 service; live OPMS data, intents
computed and logged, never sent):

```bash
python scripts/run_shadow.py ETH --exchange hyperliquid --account-id test \
  --gamma 0.25 --kappa 0.5 --max-position 0.05 --duration-s 900
```

**3 — Live keeper.** No CLI — a small async script drives the library:

```python
import asyncio, logging
from mm_core.inventory import Caps
from perp_bot.config import PerpPairConfig
from perp_bot.keeper import Keeper
from perp_bot.opms_client import OpmsClient
from perp_bot.topology import validate_account_topology

logging.basicConfig(level=logging.INFO)  # perp_bot uses stdlib logging and
                                         # configures no handlers itself

config = PerpPairConfig(
    coin="ETH", exchange="hyperliquid", account_id="test",
    gamma=0.25, kappa=0.5,             # calibrated in step 1
    caps=Caps(max_position=0.05, critical_position=0.1),
)
validate_account_topology([config])

async def main():
    client = OpmsClient(
        base_url="http://localhost:8000", ws_base_url="ws://localhost:8000",
        exchange=config.exchange, coin=config.coin,
        pair_config=config, account_id=config.account_id,
    )
    keeper = Keeper(client, config, tick_s=5.0, decision_log_path="decisions.jsonl")
    try:
        await keeper.start()
    except KeyboardInterrupt:
        pass
    finally:
        await keeper.stop()

asyncio.run(main())
```

**HB-backed variant (migration).** The same `Keeper` runs unmodified on
the `hb-enhanced-opms` path: `PerpMMController` constructs it with an
`InProcessClient` instead of `OpmsClient` — no `base_url`/`ws_base_url`, no
REST, no `aiohttp`. The controller steps the keeper each Hummingbot control
cycle and translates its intents into Hummingbot executor actions. Wiring
that controller into a strategy script (conda env, deploy helpers) is
covered in `hb-enhanced-opms/README.md`.

Notes:

- **One keeper per `(exchange, coin, account_id)` on netted venues.** If you
  deploy several keepers at once, validate the topology with
  `validate_account_topology(configs)` first.
- `tick_s` ≥ 1.0; each tick does a REST position read (+ one intent POST while
  quoting) on top of OPMS's own ~1s venue polling, so start slower (e.g. 5s)
  on a real venue. (REST/WS only on the legacy service path — in-process
  pushes on the HB path.)
- `keeper.start()` runs until stopped — there is no built-in session limit.
- `decision_log_path` is the persisted trail (flat JSONL, caller-owned; rotate
  yourself) and the shadow-mode artifact.
- Position resyncs from OPMS on every tick and after restarts; the **PnL
  ledger does not** — a restarted keeper starts its PnL from zero, so
  persist it yourself via the decision log or `keeper.pnl_explain()` if PnL
  must survive restarts.

## Testing

```bash
pytest
```

Covers keeper behavior and emitted intents, inventory reconciliation against
OPMS truth, funding and PnL accounting, OPMS client REST/WS behavior,
margin-health fail-closed paths, topology validation, the rebalancer, and
backtest fills/metrics/gates.
