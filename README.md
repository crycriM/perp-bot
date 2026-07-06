# perp_bot

Perp CLOB market-making bot built on `mm_core` and executed through OPMS.

This project is the **perp-side keeper and backtester** from the shared architecture in `clmm-animation/docs/perp-mm-strategy.md`, `mm-bot-shared-architecture.md`, and `plan-overview.md`. It does not place orders directly on exchanges. Instead, it consumes OPMS market-data and fill streams, runs shared AS/regime/risk logic, and emits `ExecIntent`s back to OPMS.

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
- **`perp_bot`** owns the perp keeper loop and backtest harness
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
- `WS /ws/fills/{exchange}`

It reconnects websocket streams and can resnapshot positions when drift or disconnects occur.

## Configuration

`PerpPairConfig` controls:

- `coin`
- `gamma`
- `kappa`
- `widen_factor`
- `exchange`
- `account_id`
- funding cadence
- position caps
- regime gate and risk-policy settings

Default routing uses `exchange="hyperliquid"` because OPMS expects real adapter ids, not shorthand labels.

## Install

```bash
pip install -e .
```

Requires Python 3.11+, `mm_core`, and `aiohttp`.

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

