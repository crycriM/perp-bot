# Perp Bot ↔ Hummingbot functional-test status

**Status date:** 2026-09-04
**Scope:** Hyperliquid perpetual market making: the existing `perp-bot` /
`dex_executor` path, and its intended replacement, `PerpMMController` in
`hb-enhanced-opms`.

This is an evidence ledger, not a claim that every test currently passes. It
was compiled from the checked-in plans, source, commit history, and the current
working trees. No project virtual environment or Hummingbot conda environment
is present in this checkout, so the historical test counts below have not been
re-run for this report.

## Bottom line

| Area | Status | What is actually proven | What is still needed |
|---|---|---|---|
| Standalone Perp Bot → native OPMS → HL testnet | **Passed historically** | The real keeper sent `ExecIntent`s through the native OPMS, and dedupe plus reconnect/resnapshot were exercised on 2026-07-10. | A fresh shadow run and the basket/rebalancer's live integration test. Standalone mainnet rollout remains deliberately deferred. |
| Backtest and basket preparation | **Implemented / calibrated** | BTC gates passed; the four-leg ETH/SOL basket and its rebalancer were added and calibrated on 2026-08-31. | The newer ETH/SOL replay is candle-derived, not tick replay; the rebalancer has no real position/price-feed test yet. |
| `PerpMMController` code path | **Implemented; unit-level evidence only** | The controller reuses `Keeper` via `InProcessClient`, maps quote and execution intents to HB executor actions, and includes a `FillObserver` and topology helper. | Run it inside a real pinned Hummingbot runtime against the HL testnet connector. |
| Hummingbot ↔ HL connection | **Partially de-risked, not graduated** | A legacy-vs-HB-native TWAP test completed on HL testnet on 2026-07-10. | There is no recorded `PerpMMController` connector startup, quote lifecycle, fill/analytics, parity, or Phase-2 live-gate result. The latest explicit deployment plan still says HB connector setup is not deployed. |
| New `hb-enhanced-opms/tests_live/` worktree | **In progress; not evidence of HB integration** | It is a double-opt-in raw Hyperliquid-SDK mainnet smoke suite. | It neither imports Hummingbot nor starts `PerpMMController`; a pass would validate raw HL access, not the HB connection. It is currently untracked and has no recorded run result. |

The migration rule is unchanged: native OPMS remains the oracle until HB passes
both parity and live gates. Do not retire the native HL path yet.

## Dated achievements

| Date | Achievement | Evidence / interpretation |
|---|---|---|
| 2026-07-08 | Native OPMS B5 HL-testnet gate passed. | Two-sided AS quoting, refresh, real fills, fill websocket, positions, and stop cleanup were exercised. The gate exposed and fixed registration, async subscription, stop-cleanup, and fill-publication defects. This proves the *native* execution body, not Hummingbot. |
| 2026-07-09 | HB migration implementation began. | Commit `a4fc3b4` introduced the generic `PerpMMController`, topology validation, and FillObserver integration. The migration plan establishes decision-log parity and a live gate as prerequisites for cutover. |
| 2026-07-10 | Perp Bot C4.3 native testnet gate passed. | `run_live_testnet_gate.py` exercised `Keeper` → `OpmsClient` → `POST /api/v1/intents` → HL testnet. It found/fixed fixed-quote loss in the intent router and reconnect/resnapshot failure. The gate's own test orders did not fill, although the shared fill mechanism had been proven in B5. |
| 2026-07-10 | Direct legacy PA-V2 vs HB-native TWAP comparison completed on HL testnet. | A real Hummingbot TWAP controller completed a buy/sell round trip and positions were independently polled. This is useful connector/executor evidence, but it is not a `PerpMMController` test and does not prove quote, risk, topology, or analytics behavior. |
| 2026-07-13 | HB leaf/controller test suite was recorded as 127/127 green. | The recorded result uses `tests/conftest.py` Hummingbot stubs; it is valuable unit coverage but is not a real-HB-runtime result. |
| 2026-08-31 | Four-leg ETH/SOL basket and `BasketRebalancer` added. | 30-day candle replay calibration was recorded for ETH and SOL; the deployment document records seven rebalancer unit tests. This advances deployment preparation, not functional connectivity. |
| 2026-09-02 | Account/subaccount documentation and deployment preparation updated. | `mm1` and `mm2` are recorded as created and funded; the same deployment plan explicitly says the Hummingbot connector still needs configuring for both. |
| 2026-09-03 | Latest committed HB change was DLMM audit logging. | It does not advance any PerpMMController parity or HL live gate. |
| 2026-09-04 | A new, untracked mainnet raw-SDK test suite is present. | It adds opt-in markers and read/account/order tests, but has no recorded execution outcome and does not exercise the Hummingbot connector. |

## Gate ledger

### Existing Perp Bot rollout

| Gate | State | Remaining evidence |
|---|---|---|
| C4.1 backtest | **Passed historically** for BTC (`gamma=0.25`, `kappa=0.5`): 5.50 bps edge, 0.057 markout ratio, 3.94% max drawdown, zero reported liquidations. | Preserve the replay artifacts; treat the liquidation check cautiously because the current backtester hard-codes it to zero. |
| C4.2 shadow | **Harness exists, run not recorded.** | Run `scripts/run_shadow.py` for a bounded live-testnet window and review its JSONL against a replay log with `scripts/diff_decision_logs.py`. |
| C4.3 native testnet | **Passed 2026-07-10.** | It is the baseline/oracle gate; rerun when the native seam changes. |
| C4.4/C4.5 standalone mainnet | **Deferred by the 2026-07-10 migration decision.** | Do not spend this risk budget before the HB controller reaches parity and testnet-live readiness. |
| Basket rebalancer | **Unit-tested only.** | Shadow it against real account positions and prices, then demonstrate a bounded correction through the selected execution body. |

### Hummingbot replacement gates

| Gate | State today | Concrete closure condition |
|---|---|---|
| Phase 1 quote-decision parity | **Partial.** `test_perp_mm_bridge.py` proves the bare keeper and `InProcessClient` produce matching records from synthetic snapshots. | Feed recorded HL data to the standalone shadow keeper and a real HB-hosted controller; use `diff_decision_logs.py` and account for every delta. |
| Phase 2 executor routing | **Implemented, indirectly tested.** | `PerpMMController._execution_actions()` exists, but its routing test recreates the routing logic instead of instantiating a real controller. Exercise the actual controller in an HB runtime. |
| FillObserver lifecycle | **Source wired; unproven.** | Verify real `on_start()` registration, fill-event delivery, mid updates, and non-empty `explain()`/markout/slippage output. The current test only checks method presence. |
| Deploy-time topology | **Helper tested; not invoked.** | Add the HB script-strategy/config-loader call to `validate_controller_topology()` before controllers start, then prove separate HL subaccount routing. |
| PA-V2 execution parity | **Not started.** | Implement the planned `research/run_shadow_pa.py` and produce a report meeting mean VWAP delta <0.5 bps and fill-rate delta <5%. |
| Phase 2 HL-testnet live gate | **Not started.** | Start a real `PerpMMController` with testnet credentials; prove passive de-risk order placement/refresh/completion, FillObserver output, and emergency completion within the short cycle. |
| One-week oracle shadow | **Not started.** | Keep native Perp Bot as the oracle while HB runs, then show no unexplained decision-log divergence for at least one week. |
| Native HL-path retirement | **Blocked.** | All preceding parity and live gates must pass first; no native files should be deleted beforehand. |

## The functional-test plan from here

1. **Make the HB environment reproducible on testnet.** Pin the exact HB
   checkout/image, install the three editable packages in its conda env, import
   a least-privilege HL testnet agent key into HB's encrypted store, and retain
   redacted startup/version logs. Verify the installed version includes the
   subaccount/vault-address fixes cited in the account-naming document.

2. **Run a read-only connector/controller smoke test.** Start one real
   `PerpMMController` without creating orders. Assert connector readiness,
   BBO/mid, funding, trading rules, account equity, signed position, and the
   correct target subaccount. This must call the real `on_start()` and prove
   FillObserver registration rather than using the HB stubs.

3. **Add and run the dedicated Phase-2 testnet gate.** Use an isolated testnet
   account and a micro position. Exercise quote create/cancel/refresh,
   passive de-risk, emergency de-risk, a real fill (or documented timeout
   fallback), position reconciliation, FillObserver output, and clean stop.
   Capture controller, connector, and decision logs as the gate artifact.

4. **Close parity before promotion.** Build the missing PA replay harness and
   run both decision-log and execution-quality comparisons on the same
   recorded tape. Resolve, rather than merely count, every decision-log delta.

5. **Test deployment topology and the basket.** Invoke topology validation in
   the real HB startup path; prove each controller sends to its intended
   subaccount. Then shadow the four-leg basket and rebalancer against live
   position/price feeds before permitting an automated correction.

6. **Soak, then consider micro-mainnet.** Keep the native path shadowing HB
   for at least the migration plan's one-week criterion. Only after the
   testnet, parity, topology, analytics, and soak artifacts are reviewed
   should the separate micro-capital approval be considered.

## Current `tests_live` safety note

`hb-enhanced-opms/tests_live/test_hyperliquid_mainnet.py` is useful as a raw
HL preflight, but should not be described as an HB functional test. It imports
`hyperliquid.info.Info` and `hyperliquid.exchange.Exchange`, rather than the
Hummingbot connector or `PerpMMController`.

It is protected by `OPMS_LIVE_MAINNET=confirm` and, for order tests,
`OPMS_LIVE_PLACE_ORDERS=confirm`. Before enabling it on an account that has
real strategies, scope `test_cancel_all_orders_for_account` to order IDs
created by the test: its present behavior can cancel every open order in the
account. Keep the HB controller gate on **testnet** until its exit criteria
are met.

## Authoritative references

- [Migration plan](../clmm-animation/docs/perp-hummingbot-migration-plan.md) — Phase 1 parity and HL testnet gate.
- [Phase 2 detailed plan](../clmm-animation/docs/perp-phase2-implementation-plan.md) — routing, analytics, topology, PA parity, and live-gate acceptance criteria.
- [Remaining-work ledger](../clmm-animation/docs/common-mm-remaining-tasks-BCDE.md) — dated native testnet results and the migration decision.
- [Perp strategy](../clmm-animation/docs/perp-mm-strategy.md) — rollout gates and backtest criteria.
- [Market-neutral deployment plan](docs/hl-market-neutral-mm-deployment-plan.md) — basket calibration, funding, and unresolved live-integration work.
- [Account and subaccount naming](docs/account-naming.md) — credential routing and the HB subaccount constraints.
- [HB OPMS README](../hb-enhanced-opms/README.md) — conda deployment and credential-import procedure.
- [`PerpMMController`](../hb-enhanced-opms/src/opms/controllers/generic/perp_mm_controller.py), [`tests_live`](../hb-enhanced-opms/tests_live/), and [`run_live_testnet_gate.py`](scripts/run_live_testnet_gate.py) — current implementation and harnesses.
