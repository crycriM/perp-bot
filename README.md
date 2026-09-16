# perp-bot

Perp CLOB market-making bot built on `mm_core` and executed through OPMS.

This project is the **perp-side keeper and backtester** from the shared architecture in `clmm-animation/docs/perp-mm-strategy.md`, `common-mm-bot-shared-architecture.md`, and `common-plan-overview.md`. It does not place orders directly on exchanges. Instead, it consumes OPMS market-data and fill streams, runs shared AS/regime/risk logic, and emits `ExecIntent`s back to OPMS.

## What this bot does

- subscribes to normalized **market-data** and **fills** from OPMS
- maintains authoritative perp inventory from OPMS position snapshots
- computes **Guéant stationary Avellaneda-Stoikov** quotes using `mm_core`
- applies shared **risk policy**: quote, widen, stop quoting, de-risk, emergency exit
- emits **`ExecIntent`** messages instead of exchange-native order verbs
- tracks **PnL, spread capture, markout, and funding**
- provides an **event-replay backtester** for HL-style L2/trade/funding data

## Role in the system

The architecture split is deliberate:

- **`mm_core`** owns shared math and policy
- **`perp-bot`** owns the perp keeper loop and backtest harness
- **`dex_executor` / OPMS** owns actual execution, adapters, positions, and venue connectivity

So this bot decides **what state to reach**:

- target inventory
- passive vs urgent de-risking
- quote prices and sizes

OPMS decides **how to execute it** using its own execution strategies.

## Package layout

```text
src/perp_bot/
  backtest.py      # event-replay backtester and rollout gates
  config.py        # per-pair config
  keeper.py        # live keeper loop and decision logging
  opms_client.py   # REST + WS client for OPMS
```

## Core concepts

### Keeper loop

`Keeper` ingests OPMS state and runs the live decision cycle:

1. receive market snapshots and fills from OPMS websockets
2. reconcile local inventory with authoritative OPMS positions
3. evaluate regime and markout using `mm_core`
4. run shared risk policy
5. emit an `ExecIntent` back to OPMS

It also writes optional JSON-lines decision logs for shadow-mode analysis.

### Inventory model

Unlike the DLMM bot, the perp bot uses a single signed inventory:

- positive = long perp
- negative = short perp

This maps directly onto the AS formulation and makes inventory management simpler than two-token LP inventory.

### Intent-based execution

This bot never emits `place_order` or `cancel_order` calls. It emits:

- quote intents with `QuoteSpec`
- target-inventory intents for de-risking
- emergency intents for urgent flattening

That is the seam required by the shared architecture so OPMS remains the single order-authority.

### PnL and markout

The keeper uses the shared `mm_core` PnL ledger and markout tracker to measure:

- realized and unrealized PnL
- spread capture
- markout / toxic flow
- funding accrual
- fee impact

### Backtesting

`Backtest` replays:

- market snapshots
- trade prints
- funding events

It uses the same shared PnL accounting as the live keeper, so backtest and live numbers come from one accounting path.

It also exposes rollout gates inspired by the perp strategy doc:

- minimum net edge
- maximum markout ratio
- maximum drawdown
- zero liquidations in the simplified model

## OPMS integration

`OpmsClient` talks to the real OPMS surfaces:

- `POST /api/v1/intents`
- `GET /api/v1/positions/{exchange}/{symbol}`
- `GET /api/v1/accounts/{exchange}/{account_id}/equity`
- `WS /ws/md/{exchange}/{symbol}`
- `WS /ws/fills/{exchange}?account_id=...`

It reconnects websocket streams and can resnapshot positions when drift or disconnects occur.

## Configuration

`PerpPairConfig` controls:

- `coin`
- `gamma`
- `kappa`
- `widen_factor`
- `exchange`
- `account_id`
- `position_mode`
- funding cadence
- position caps
- regime gate and risk-policy settings

Default routing uses `exchange="hyperliquid"` because OPMS expects real adapter ids, not shorthand labels.

`position_mode` is inferred from the venue when omitted:

- `hyperliquid` / `lighter` -> `net`
- `aster` -> `hedge`

For netted venues, deploy at most **one keeper per `(exchange, coin, account_id)`**.
If you launch several keepers at once, validate that topology explicitly with
`perp_bot.validate_account_topology(...)` before starting them.

## Install

```bash
pip install -e .
```

Requires Python 3.11+, `mm-core`, and `aiohttp`.

## Running on one pair

There's no CLI yet — the keeper is a library you drive with a small async
script. Three pieces: an OPMS account, a `PerpPairConfig`, and a `Keeper`
wired to an `OpmsClient`.

### 1. Have OPMS running with the account configured

The bot talks to a running `dex_executor` (OPMS) instance — it does not hold
exchange credentials itself. Configure the account there via env vars
(`{EXCHANGE}_{ACCOUNT_ID}_{CREDENTIAL_TYPE}`, see `dex_executor/README.md`),
e.g. for Hyperliquid testnet:

```bash
HYPERLIQUID_TEST_PRIVATE_KEY=0x...
HYPERLIQUID_TEST_ACCOUNT_ADDRESS=0x...
HYPERLIQUID_TEST_IS_TESTNET=true
```

then start OPMS (from `dex_executor/`):

```bash
uvicorn opms.service.app:app --host 127.0.0.1 --port 8000 --app-dir src
```

### 2. Configure the pair

```python
from mm_core.inventory import Caps
from perp_bot.config import PerpPairConfig
from perp_bot.topology import validate_account_topology

config = PerpPairConfig(
    coin="ETH",
    exchange="hyperliquid",   # OPMS adapter id — must match AccountRegistry
    account_id="test",        # -> HYPERLIQUID_TEST_* in OPMS's .env
    # omitted -> inferred from venue capabilities: Hyperliquid/Lighter = net,
    # Aster = hedge
    gamma=1.0,                # risk aversion — see calibration note below
    kappa=0.5,                # fill-intensity decay — see calibration note below
    # caps are in the coin's own base units (ETH here), not USD. Quote size
    # per side is 10% of max_position (keeper.py:_actuate), so keep this
    # small for a first run: 0.05 ETH cap -> 0.005 ETH (~$9) per quote.
    caps=Caps(max_position=0.05, critical_position=0.1),
)

validate_account_topology([config])
```

`gate` and `risk` (`mm_core.regime.GateConfig`, `mm_core.risk_policy.RiskConfig`)
default to stock values in `__post_init__` if left unset.

**`gamma`/`kappa` calibration:** these are the Guéant-Avellaneda-Stoikov
risk-aversion and fill-intensity-decay parameters (`mm_core/as_core.py`).
They're scale-dependent on the pair's price and volatility — there's no
universal "right" value, and the numbers above are only the test suite's
placeholder (calibrated for a synthetic BTC fixture, not real ETH). Don't
copy them into a live config; fit them against `Backtest` on the pair's own
recorded data first (see step 4).

### 3. Run the keeper

```python
import asyncio
import logging
from perp_bot.opms_client import OpmsClient
from perp_bot.keeper import Keeper

logging.basicConfig(level=logging.INFO)  # perp_bot uses stdlib logging and
                                          # configures no handlers itself —
                                          # nothing prints without this

async def main():
    client = OpmsClient(
        base_url="http://127.0.0.1:8000",
        ws_base_url="ws://127.0.0.1:8000",
        exchange=config.exchange,
        coin=config.coin,
        api_key="",  # OPMS has no auth yet; leave empty
        pair_config=config,
        account_id=config.account_id,
    )
    # tick_s: each tick does one REST GET (positions) and, while quoting,
    # one REST POST (intent) to OPMS — which itself polls the exchange for
    # market data on its own ~1s cadence (md_publisher.py). 1.0 is roughly
    # the floor; start slower (e.g. 5.0) against a real venue to stay well
    # under its rate limits, the same reasoning as OPMS's own MM algorithm
    # (dex_executor's avellaneda_stoikov_mm defaults to a 5s refresh).
    keeper = Keeper(client, config, tick_s=5.0, decision_log_path="decisions.jsonl")
    try:
        await keeper.start()
    except KeyboardInterrupt:
        pass
    finally:
        await keeper.stop()

asyncio.run(main())
```

`keeper.start()` connects the OPMS market-data and fills websockets, then
runs the tick loop until `keeper.stop()` is called (or the process is
killed) — it does not exit on its own, and there's no built-in max-iteration
or session limit for live running.

Each emitted `ExecIntent` now carries `account_id`, and the fills websocket is
subaccount-scoped (`/ws/fills/{exchange}?account_id=...`) so same-symbol
strategies on different Hyperliquid subaccounts do not ingest each other's
fills.

For **C4.2 shadow**, use the same keeper path with `shadow_mode=True`: intents
are fully computed and written into the decision log, but they are **not**
posted to OPMS. The helper script runs this mode for a bounded window:

Margin health is fail-closed on every live and replayed position snapshot.
Missing, malformed, or non-finite `margin_available` values are logged at
critical severity and treated as zero available margin, producing an
`EMERGENCY_EXIT` instead of silently disabling the stop. Decision logs persist
the validated reading so `replay_decision_log.py` can reproduce the configured
soft/hard margin thresholds exactly.

```bash
python scripts/run_shadow.py BTC \
  --exchange hyperliquid \
  --account-id test \
  --gamma 0.25 --kappa 0.5 \
  --max-position 0.1 --critical-position 0.2 \
  --duration-s 900 \
  --decision-log-path shadow-decisions.jsonl
```

**Logs:** `perp_bot.keeper`/`.opms_client`/`.backtest` log through stdlib
`logging` (`logger.info(...)` per tick: mid, regime, decision, urgency,
inventory, equity) — configure a handler yourself (`logging.basicConfig`
above, or point it at a file) or you'll see nothing. Separately,
`decision_log_path` (optional) appends one JSON line per tick — `ts, mid,
regime, decision, urgency, inventory, equity, total_pnl, intent`, plus
`source` and `intent_sent` — the only built-in persisted trail, and it's a
flat file the caller owns (rotate it yourself); pass `None` (the default) to
skip it.

**Position:** perp-bot holds no durable state of its own. `Keeper` keeps
inventory in memory (`self._inventory.position`) and re-syncs it from OPMS's
authoritative `GET /api/v1/positions/{exchange}/{symbol}` every tick, and
again via `resnapshot_positions()` on websocket reconnect/error — so OPMS
(and ultimately the exchange) is the source of truth, and a keeper restart
just re-adopts whatever OPMS reports. Nothing is written to disk by
perp-bot itself outside the optional decision log above.

**PnL:** also in-memory only, via `mm_core`'s `PnLLedger`
(`self._pnl` in `Keeper`) — call `keeper.pnl_explain()` for a full breakdown
(realized/unrealized/spread-capture/funding/fees) at any time, and note
`total_pnl` is written into each decision-log row if you configured one.
Unlike position, realized PnL/fill history is **not** reconciled from OPMS
on restart — a restarted keeper starts its PnL ledger from zero (inventory
resyncs; historical realized PnL doesn't). If you need PnL to survive
restarts, consume the decision log (or `pnl_explain()`) into your own store.

### 4. Backtest before going live

Calibrate `gamma`/`kappa` and check the §8.3 rollout gates with `Backtest`
before sending anything live. It's a pure in-memory event-replay engine —
no network, no OPMS — driven by the same `mm_core.pnl.PnLLedger` the live
keeper uses, so backtest and live PnL are never two different pieces of
math.

**There is no data fetcher yet** (Stream C's `common-mm-remaining-tasks-BCDE.md`
task "C3 data fetch" is still open) — you feed it `MarketSnapshot`/
`Backtrade`/funding lists yourself. Today the only exercised path is
synthetic data (see `tests/test_backtest.py`); pulling real HL history
(`info.l2_snapshot`, `candles/trades`, `funding_history`) into that shape is
the still-open prerequisite before this produces a real result for a real
pair.

```python
import asyncio
import time

from mm_core.contracts import MarketSnapshot
from mm_core.inventory import Caps

from perp_bot.config import PerpPairConfig
from perp_bot.backtest import Backtest, Backtrade, Strategy

config = PerpPairConfig(
    coin="ETH", exchange="hyperliquid",
    gamma=1.0, kappa=0.5,  # the values under test — sweep these, don't guess
    caps=Caps(max_position=0.05, critical_position=0.1),
)

bt = Backtest(config, start_equity=10_000.0)
bt.set_strategy(Strategy(config))  # the Guéant-AS strategy under test —
                                    # BaselineStrategy (fixed symmetric
                                    # spread) is the §8.3 comparison point

# Feed the replay data — ts must be in one consistent clock across all three:
t0 = time.time()
for i in range(3600):                                    # one L1 snapshot/s
    bt.add_snapshot(MarketSnapshot(venue="hyperliquid", coin="ETH", ts=t0 + i, mid=1750.0 + i * 0.01))
bt.load_trades([
    Backtrade(ts=t0 + 12, side="buy", price=1750.5, size=0.01),
    # ... one Backtrade per print; a resting quote fills when a trade
    # crosses its price (cancel/replace, never stacked — see _fill_rule)
])
bt.load_funding([
    {"ts": t0 + 1800, "rate": 1e-5},  # discrete scheduled payments, not
])                                    # continuous — HL pays hourly/8h

history = asyncio.run(bt.run(duration_s=3600.0, tick_s=1.0))
```

`history` is a list of `{ts, mid, equity, position}` dicts, one per replay
step. `tick_s` defaults to `1.0` in `Backtest.run(...)`, but the
CSV-driven `scripts/run_backtest.py` infers a larger step from sparse candle
data so 30-day fetches do not spend millions of iterations on empty seconds.
For the numbers that matter:

```python
m = bt.metrics()
# {final_equity, net_pnl, net_edge_bps, max_drawdown, n_fills,
#  spread_capture, markout_pnl, markout_ratio, funding_pnl, liquidations}

report = bt.gate_report()
# {"passed": bool, "checks": {name: {"passed", "value", "threshold"}}}
# gates: net_edge_bps > 2.0, markout_ratio < 0.5, max_drawdown < 0.05,
# liquidations == 0 (perp-mm-strategy.md §8.3)
```

`bt.pnl_explain()` gives the full `PnLBreakdown` behind `metrics()` —
realized/unrealized PnL, spread capture, markout, funding, fees, and every
individual fill — for auditing *why* a number came out the way it did, not
just what it is.

If you pass `--decision-log-path` to `scripts/run_backtest.py`, the backtest
also writes one JSONL intention record per replay step (`source="backtest"`).
Compare a backtest artifact to a shadow run with:

```bash
python scripts/diff_decision_logs.py backtest-decisions.jsonl shadow-decisions.jsonl
```

Two things that look wired up but aren't yet: `set_baseline()` stores a
`BaselineStrategy` on the instance but nothing in `run()`/`metrics()` reads
it back — to compare AS against baseline, run two separate `Backtest`
instances (one `set_strategy(Strategy(config))`, one
`set_strategy(BaselineStrategy())`) over the same data and diff their
`metrics()` yourself. And `liquidations` in `metrics()` is hardcoded to
`0` — there's no margin/leverage model in this backtester, so that gate
always passes regardless of how much leverage the live config would use.

See `common-mm-remaining-tasks-BCDE.md`'s Stream C section for the full rollout
sequence this feeds into: backtest → shadow (log intents, don't send) →
testnet live → micro mainnet.

## Tests

```bash
pytest
```

The test suite covers:

- keeper behavior and emitted intents
- inventory reconciliation from OPMS truth
- funding and PnL accounting
- OPMS client REST/WS integration behavior
- backtest fills, metrics, and rollout gates
