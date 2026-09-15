# Perp Bot ↔ Hummingbot functional-test status

**Status date:** 2026-09-15
**Scope:** Hyperliquid perpetual market making: the existing `perp-bot` /
`dex_executor` path, and its intended replacement, `PerpMMController` in
`hb-enhanced-opms`.

This is an evidence ledger, not a claim that every test currently passes. It
was compiled from the checked-in plans, source, commit history, and the current
working trees.

Environment now present on this machine (updated 2026-09-15):

- The `hummingbot` conda env exists and has `mm_core`, `perp_bot`, and `opms`
  installed editable (Python 3.13.15), plus the `hyperliquid` SDK.
- **Hummingbot itself is now compiled and importable.** The pinned checkout
  `/home/christian/sources/hummingbot` (v2.16.0, version `20260729`) was built
  in place with `pip install -e . --no-deps --no-build-isolation`; its 60
  Cython `.pyx` sources now have compiled `.so` files and
  `import hummingbot.connector.connector_base` succeeds.
- ⚠ **The pinned Hummingbot checkout carries one local (uncommitted) patch:**
  `hyperliquid_perpetual_api_order_book_data_source.py` `_parse_funding_info_message`
  used `ctx.get("openInterest", ctx.get("funding", "0"))` as the funding rate.
  HL's `activeAssetCtx` payload contains both fields, so the connector reported
  open interest (~1e6) as funding. Fixed to `ctx.get("funding", "0")`. Re-apply
  this patch after any Hummingbot update; the live smoke guards it (funding
  must be `< 0.01`).
- Unit suites green on 2026-09-15: **mm-core 112**, **perp-bot 52**,
  **hb-enhanced-opms 142** stub tests, plus **19 real-Hummingbot tests**
  (`tests_real/`, no stubs; 2 more skip without `OPMS_HB_MAINNET`). `scripts/run_hb_mainnet_smoke.py` passes on
  mainnet read-only for both `e2_mm1` (vault) and `e2_main` (master).

## Bottom line

| Area | Status | What is actually proven | What is still needed |
|---|---|---|---|
| Standalone Perp Bot → native OPMS → HL testnet | **Passed historically** | The real keeper sent `ExecIntent`s through the native OPMS, and dedupe plus reconnect/resnapshot were exercised on 2026-07-10. | A fresh shadow run and the basket/rebalancer's live integration test. Standalone mainnet rollout remains deliberately deferred. |
| Backtest and basket preparation | **Implemented / calibrated** | BTC gates passed; the four-leg ETH/SOL basket and its rebalancer were added and calibrated on 2026-08-31. | The newer ETH/SOL replay is candle-derived, not tick replay; the rebalancer has no real position/price-feed test yet. |
| `PerpMMController` code path | **Runs against real Hummingbot; quote, fill, de-risk and emergency paths all passed live on mainnet** | The controller reuses `Keeper` via `InProcessClient`, maps quote/execution intents to HB executor actions, wires a `FillObserver`, and routes every intent to an executor config the real `ExecutorOrchestrator` can instantiate (13 real-HB tests). Three real bugs fixed 2026-09-14/15: the non-existent `TwapExecutorConfig` import (real name/fields: `TWAPExecutorConfig`, `total_amount_quote`/`total_duration`/`order_interval`), the unregistered custom `passive_aggressive_executor`, and `_current_equity()` looking up `"USDC"` where HB's HL connector reports `"USD"` (silent zero equity). `scripts/run_hb_mainnet_smoke.py` runs the real `on_start()` + `update_processed_data()` on mainnet read-only and asserts FillObserver registration, equity, and analytics shape. Five more real defects were fixed 2026-09-15 on the way to the de-risk gate (see the achievement row). | Parity vs the native oracle. |
| Hummingbot ↔ HL connection | **Read-only smoke + live quote/cancel gate passed on mainnet** | The real `HyperliquidPerpetualDerivative` builds with `use_vault=True` for `e2_mm1` (reads its 300 USD unified balance) and `use_vault=False` for `e2_main` (131.98 USD); the real `PerpMMController.on_start()` registers the `FillObserver` on that connector, `update_processed_data()` reads a live mid + funding (`1.25e-05`, after the connector patch) and returns positive equity. A mainnet credential importer for master/subaccount shapes exists and dry-runs correctly. `scripts/run_hb_mainnet_quote_gate.py` then drove the same controller through real `OrderExecutor`s to **resting mainnet orders** on `e2_mm1` and `e2_mm2` (see the 2026-09-15 achievement row). `scripts/run_hb_mainnet_derisk_gate.py` then produced real fills, reconciled the position, and flattened it through the de-risk and emergency executors. | Import into HB's encrypted store (the gates inject credentials directly) and run under a real HB strategy/Clock; parity. |
| `hb-enhanced-opms/tests_live/` mainnet raw-SDK gate | **Passed 2026-09-14 (30/30)** | Real HL-mainnet orders were placed and cancelled on `e2_main`, `e2_mm1`, and `e2_mm2`; subaccount `vaultAddress` routing was proven (an order signed for one subaccount does not appear on any other); post-only (`Alo`) was proven maker-only (passive rests, crossing is rejected without filling); unified-account spot collateral backs perps; cleanup left zero orders/positions. | Still raw-SDK, not Hummingbot. It validates HL access, post-only, and the subaccount routing seam, not the HB connector or `PerpMMController`. |

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
| 2026-09-14 | Mainnet raw-SDK tiny-order gate run and passed; harness hardened. | `tests_live/` now routes subaccount calls through `vault_address`, prices test orders far-passive (below bid / above ask) so they cannot cross or fill, and cancels by `oid` only. 30/30 passed on mainnet across `e2_main`/`e2_mm1`/`e2_mm2`, including a post-only (`Alo`) maker-only proof; cleanup verified clean (0 orders / 0 positions). Also fixed the previous `test_cancel_all_orders_for_account` blanket cancel (would drain every open order on a live account) by replacing it with scoped cancellation. |
| 2026-09-14 | Hummingbot compiled; `PerpMMController` fixed and verified against real HB; mainnet connector path added. | Built the pinned HB checkout in place (`.so` for all 60 `.pyx`). Fixed `PerpMMController`'s `TwapExecutorConfig` → `TWAPExecutorConfig` (correct field names), registered the custom `PassiveAggressiveExecutor` in `ExecutorOrchestrator._executor_mapping`, and fixed `_current_equity()` to resolve the connector's actual balance label (`USD`, not `USDC`). Added `tests_real/` (13 tests, no stubs) which caught all three bugs the stub suite masked; added `scripts/import_hl_mainnet_credentials.py` (master vs subaccount `use_vault` auto-detection) and proved the real mainnet connector reads the subaccount unified balance. |
| 2026-09-15 | Live read-only `PerpMMController` smoke passed on HL mainnet; fixed a HB funding-rate connector bug. | `scripts/run_hb_mainnet_smoke.py` brought up the real connector + controller on mainnet for `e2_mm1` (vault) and `e2_main` (master), calling real `on_start()`/`update_processed_data()` with no orders: mid, funding, positive equity, and `{fill_pnl, markout, slippage}` analytics all verified. This exposed a Hummingbot bug: the HL WS funding parser used `openInterest` as the funding rate (reported ~1.04e6 instead of `1.25e-05`). Patched the pinned checkout to use `funding`; the smoke now guards the range. |
| 2026-09-15 | **Live quote/cancel gate passed on HL mainnet through the real HB executor path**, on both basket subaccounts. | `scripts/run_hb_mainnet_quote_gate.py` drives the real `PerpMMController` on a live connector: the keeper reached its own `QUOTE` decision (regime gate open, 10–13 warm-up ticks), `determine_executor_actions()` produced two `OrderExecutorConfig`s, and real `OrderExecutor`s placed two `LIMIT_MAKER` (HL `Alo`) orders of 0.006 ETH (~$14.9/side). Verified independently through the raw HL SDK: both orders rested **only** on the target subaccount and were invisible to the other two accounts; the refresh cycle emitted `StopExecutorAction`s matching the live executor ids, cancelled both resting orders and replaced them; teardown left **0 orders / 0 positions** and USDC balances unchanged (300.0 on both). Artifacts: `hb-enhanced-opms/logs/quote_gate_20260915T114934` (`e2_mm1`) and `…T115127` (`e2_mm2`), each with `gate.json` and `decisions.jsonl` (the intended `gate.log` was silently not written — HB's root-logger setup made `logging.basicConfig` a no-op; fixed afterwards). Orders were priced 8 bps through the touch on purpose, so **no fill occurred** — markout/slippage stay empty and a real-fill gate is still owed. The first attempt (`…T114705`) failed and left four live orders that the gate's own raw-SDK force-cancel cleaned up: `early_stop()` sets `SHUTTING_DOWN`, which is already *not* `is_active`, so waiting on `is_active` and then calling `stop()` kills the control loop before `control_shutdown_process` cancels. Anything that stops executors outside `ExecutorOrchestrator` must wait for `is_closed`. |
| 2026-09-15 | **Real Hummingbot launch path built (step A of the single-account plan); not yet run.** | `hb-enhanced-opms/deploy/`: HB V2 script `opms_perp_mm.py` (refuses to start on topology violations or credentials routed to another account — this is the first call site of `validate_controller_topology()`), controller shim, shadow configs for `e2_mm1`, installer, and `run_shadow_session.sh`. Verified offline with HB's own loader logic: script/config classes resolve, `PerpMMController` loads from the YAML, markets derive to `hyperliquid_perpetual: ETH-USD`, the routing guard accepts `e2_mm1`'s address and rejects master credentials. Fixed on the way: HB's `add_controller()` never passes `update_interval`, so controllers would have cycled at 1 s (now a config field, default 5 s); `opms/connectors/topology.py` kept its own capability table that still called Lighter hedge-mode (now delegates to `perp_bot.venue_capabilities`). Added controller `shadow_mode`. Parity tool `perp-bot/scripts/replay_decision_log.py` replays an HB-hosted log through a bare `Keeper`: exact parity on the `e2_mm2` quote-gate log (14/14 rows), and on the de-risk log it flags exactly the 3 rows after the injected drawdown. **Blocked on** an operator-chosen `HB_PASSWORD` (none exists; the credential store is empty). |
| 2026-09-15 | **Live fill / de-risk / emergency gate passed on HL mainnet**, after fixing six defects on the HB execution path — two of them found only by running live. | `scripts/run_hb_mainnet_derisk_gate.py` on `e2_mm1` (`logs/derisk_gate_20260915T122339`): MARKET open of 0.012 ETH filled and the controller position reconciled in 12.8 s; the keeper's own `DE_RISK` decision ran **one** reduce-only `PassiveAggressiveExecutor` (children 0.0045 + 0.0075 ETH, resting, refreshed every ~21 s, filled passively) to flat in 63.6 s, `COMPLETED`; after re-open and an injected drawdown, `EMERGENCY_EXIT` ran **one** 10 s-cycle PA whose aggressive market order fired 12.2 s after its first order, flat 24.8 s after escalation; 0 rejected orders, 0 leftovers, `e2_mm1` USDC 299.85 after all of the day's gates. **Defects fixed (all locked by `tests_real/` or the stub suite):** (1) the controller read position from HB's `positions_held`, executor bookkeeping that never sees PA fills — it now reads the connector's venue position; (2) `determine_executor_actions()` stopped and recreated every executor on every control cycle, resetting the PA child clock so its aggressive fallback (and thus any emergency exit) could never fire — an in-flight execution executor with the same type/side/cycle is now kept; (3) PA orders were always `OPEN`, so a de-risk could flip the book — PA now takes `position_action` and de-risk/emergency send reduce-only `CLOSE`; (4) PA children were `amount/5` with a separate remainder child, both routinely under HL's $10 minimum — children are now sized to min notional and the remainder folds into the last child; (5) **found live:** HB's `ExecutorInfo.config` is a discriminated union of built-in configs only, so a running PA raised on `executor_info`, breaking controller reports and the DB recorder — the controller module now extends the union; (6) **found live:** the connector position cache lags fills by 5–12 s, so right after a de-risk completed the keeper re-issued a second reduce-only PA that HL rejected 17× across two phases (reduce-only is what stopped it flipping short) — the controller now refreshes venue positions once when an executor that traded finishes. Also: PA early-stop no longer reports `COMPLETED`, and gate logs are now actually captured. |

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
| Phase 1 quote-decision parity | **Partial; tooling ready.** `test_perp_mm_bridge.py` proves the bare keeper and `InProcessClient` produce matching records from synthetic snapshots; `replay_decision_log.py` shows exact parity on a real HB-controller mainnet log (14/14). | Feed recorded HL data to the standalone shadow keeper and a real HB-hosted controller; use `diff_decision_logs.py` and account for every delta. |
| Phase 2 executor routing | **Implemented; verified against real HB and live on mainnet.** | `tests_real/test_perp_mm_controller_real_hb.py` instantiates the real controller path and asserts every urgency (quote / passive / normal / immediate / emergency) produces a config whose `.type` is in HB's real `ExecutorOrchestrator._executor_mapping`. Quote (`run_hb_mainnet_quote_gate.py`), de-risk and emergency (`run_hb_mainnet_derisk_gate.py`) routes are proven live to fills. | The `immediate` → `TWAPExecutor` route is **unreachable from the keeper** (`STOP_QUOTING` is its only `immediate` decision and carries a zero inventory gap), so it is untested live by construction. |
| FillObserver lifecycle | **Proven live with real fills 2026-09-15.** | The de-risk gate delivered 5 real `OrderFilledEvent`s (market open, passive PA fills, aggressive emergency fill) to the FillObserver: non-empty `explain()`, markout resolved at 10/30/60 s, slippage n=5 (mean 0.12 bps). | — |
| Deploy-time topology | **Helper tested; routing proven through the HB connector; call site now exists** (`deploy/hummingbot/scripts/opms_perp_mm.py`, not yet run in a live HB process). The quote gate showed a `PerpMMController` on `e2_mm1` and one on `e2_mm2` each landing orders **only** on their own subaccount (raw-SDK cross-check of all three accounts). | Add the HB script-strategy/config-loader call to `validate_controller_topology()` before controllers start, and run the two legs **concurrently** — both gate runs were sequential, and the three accounts still share one agent signer key. |
| Raw HL subaccount routing | **Proven 2026-09-14 (raw SDK).** | `vault_address`-scoped orders land only on the intended subaccount and are invisible to the other subaccounts/master. This de-risks the seam HB will drive. |
| Hummingbot runtime availability | **Resolved 2026-09-14.** | Pinned HB checkout (`/home/christian/sources/hummingbot`, v2.16.0) compiled in place; `import hummingbot.connector.connector_base` succeeds. |
| PA-V2 execution parity | **Not started.** | Implement the planned `research/run_shadow_pa.py` and produce a report meeting mean VWAP delta <0.5 bps and fill-rate delta <5%. |
| Phase 2 live gate | **Passed on mainnet 2026-09-15** (no HL testnet credentials exist in `.env`, so both gates ran on mainnet at micro size, matching the 2026-09-14 raw-SDK precedent). Quote create → rest → refresh → cancel → clean stop on `e2_mm1` and `e2_mm2`; real fill, position reconciliation, passive de-risk to `COMPLETED`, emergency exit to flat, FillObserver analytics, and clean stop on `e2_mm1` (`logs/derisk_gate_20260915T122339`). All §7.1 criteria met. The emergency market-order latency first measured 12.2 s against the 10 s criterion (10 s child limit + executor tick + cancel acknowledgement); the emergency `child_order_time_limit` was set to **5 s** by decision on 2026-09-15 and re-measured at **8.7 s** from the controller's decision (flat in 21.7 s, `logs/derisk_gate_20260915T144500`). The gate now fails if it exceeds 10 s. |
| One-week oracle shadow | **Not started.** | Keep native Perp Bot as the oracle while HB runs, then show no unexplained decision-log divergence for at least one week. |
| Native HL-path retirement | **Blocked.** | All preceding parity and live gates must pass first; no native files should be deleted beforehand. |

## The functional-test plan from here

1. **Make the HB environment reproducible.** Pin the exact HB
   checkout/image, install the three editable packages in its conda env, import
   a least-privilege HL agent key into HB's encrypted store, and retain
   redacted startup/version logs. Verify the installed version includes the
   subaccount/vault-address fixes cited in the account-naming document.
   **Mostly done (2026-09-14):** the conda env, the three editable packages,
   and Hummingbot itself (all 60 Cython extensions compiled) are in place, and
   `scripts/import_hl_mainnet_credentials.py` now imports master/subaccount
   credentials with the right `use_vault` flag (dry-run verified). Remaining:
   actually run the import with `HB_PASSWORD` and retain the redacted
   startup/version logs.

2. **Run a read-only connector/controller smoke test.** Start one real
   `PerpMMController` without creating orders. Assert connector readiness,
   BBO/mid, funding, trading rules, account equity, signed position, and the
   correct target subaccount. This must call the real `on_start()` and prove
   FillObserver registration rather than using the HB stubs.
   **Done 2026-09-15 (read-only, mainnet):** `scripts/run_hb_mainnet_smoke.py`
   starts the real connector + controller for `e2_mm1` (vault) and `e2_main`
   (master), calls real `on_start()`, asserts FillObserver registration, and
   reads mid, funding, positive equity, and the analytics shape — no orders.
   `tests_real/` adds the structural + balance coverage. Trading-rules
   assertion is covered indirectly (order configs quantize through the real
   connector); an explicit trading-rules read is still worth adding.

3. **Add and run the dedicated Phase-2 testnet gate.** Use an isolated testnet
   account and a micro position. Exercise quote create/cancel/refresh,
   passive de-risk, emergency de-risk, a real fill (or documented timeout
   fallback), position reconciliation, FillObserver output, and clean stop.
   Capture controller, connector, and decision logs as the gate artifact.
   **Quote half done 2026-09-15 (mainnet, micro size — no testnet
   credentials exist):** `hb-enhanced-opms/scripts/run_hb_mainnet_quote_gate.py`
   proves quote create/rest/refresh/cancel and clean stop on `e2_mm1` and
   `e2_mm2`, writing `gate.json` + `decisions.jsonl` per run (`gate.log` from later runs on).
   **Fill / de-risk / emergency half done 2026-09-15:**
   `hb-enhanced-opms/scripts/run_hb_mainnet_derisk_gate.py` opens a 0.012 ETH
   position with a real MARKET fill, lets the keeper's own risk policy drive a
   reduce-only passive de-risk to flat at real control-cycle cadence, re-opens,
   injects a drawdown so the keeper escalates to `EMERGENCY_EXIT`, and requires
   flat within 30 s; it fails on a second executor or any rejected order.
   Passed on `e2_mm1` (artifacts `gate.json`, `decisions.jsonl`, `gate.log`).
   **Step 3 is closed** (emergency latency re-measured at 8.7 s after the
   5 s emergency child limit; see the gate ledger).

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
HL preflight and as the subaccount-routing gate, but should not be described
as an HB functional test. It imports `hyperliquid.info.Info` and
`hyperliquid.exchange.Exchange`, rather than the Hummingbot connector or
`PerpMMController`.

It is protected by `OPMS_LIVE_MAINNET=confirm` and, for order tests,
`OPMS_LIVE_PLACE_ORDERS=confirm`. As of 2026-09-14 the previously dangerous
blanket `test_cancel_all_orders_for_account` has been removed; cancellation is
by `oid` on orders the test created, and every placement is cancelled in a
`finally`. The `hl_exchange` fixture now signs subaccount calls with
`vaultAddress` set to that subaccount, so `e2_mm1`/`e2_mm2` tests no longer
route to the master. Order sizing is deliberately tiny and priced far from the
touch (non-marketable), so a pass should leave no position and no resting
order — verify with the cleanup check described in the test plan.

Two mainnet facts to remember: the accounts are in **unified-account mode**, so
spot USDC collateralizes perps (perp `accountValue` stays 0 until a position
opens — read `spot_user_state` for the real balance), and all three
`HYPERLIQUID_E2_*` entries share **one agent signer key**
(`0x8a9e…Fd709`). The shared key is fine for sequential tests but will collide
on nonces if `e2_mm1` and `e2_mm2` ever trade concurrently — split them onto
separate agent wallets before a concurrent basket deployment.

Keep the HB controller gate on **testnet** until its exit criteria are met.

## Open findings from the 2026-09-15 live gates (not fixed)

- ~~**Drawdown stop is blind to open-position losses on HL unified
  accounts.**~~ **Withdrawn — the finding was wrong.** It compared HB's
  equity with the perp `accountValue`, which on a unified account is only the
  margin allocated to the position (+ its PnL). Per-cycle snapshots with
  0.012 ETH open (`logs/derisk_gate_20260915T144500`) show HL marks the spot
  USDC total to market: `total − unrealizedPnl` stayed at 299.8353 across
  five samples while PnL moved. So the `USD` balance HB feeds the keeper
  already includes unrealized PnL and the drawdown stop works (it lags by one
  connector balance poll, 5–12 s). The derisk gate now fails if that identity
  breaks. HL also publishes a venue-computed margin indicator:
  `spotClearinghouseState.tokenToAvailableAfterMaintenance` = spot total −
  `crossMaintenanceMarginUsed` (exact to 1e-4 in the same samples) — a
  liquidation-distance measure usable for a margin-health stop, which
  `RiskPolicy` does not have today.
- ~~**Emergency latency 12.2 s vs the plan's 10 s.**~~ Resolved: 5 s child
  limit, 8.7 s measured.
- **A PA whose child is skipped still reports `COMPLETED`** — task opened in
  `clmm-animation/docs/common-mm-remaining-tasks-BCDE.md` (Stream D, not yet
  done). (seen when HL
  rejected every order of a child: `cycle expired in IDLE — skipping
  (filled 0/0.012)` → `COMPLETED`). Consumers must not read `COMPLETED` as
  "filled"; check executed size or venue position.
- **Quotes are cancel/replaced every control cycle.** HB's controller
  `update_interval` defaults to 1 s; deploy with ≥ 5 s (the keeper's
  `tick_s >= 5` guidance) to stay well inside HL rate limits.
- ~~**`/home/christian/sources/hummingbot/conf/conf_client.yml` was owned by
  root.**~~ Resolved 2026-09-15 (chowned to `christian`).
- **Harness-only noise:** the standalone gates don't start HB's `RateOracle`,
  so `InFlightOrder.cumulative_fee_paid` logs "Could not find the exchange
  rate for USD-ETH". Fills and FillObserver fees are unaffected.

## Authoritative references

- [Migration plan](../clmm-animation/docs/perp-hummingbot-migration-plan.md) — Phase 1 parity and HL testnet gate.
- [Phase 2 detailed plan](../clmm-animation/docs/perp-phase2-implementation-plan.md) — routing, analytics, topology, PA parity, and live-gate acceptance criteria.
- [Remaining-work ledger](../clmm-animation/docs/common-mm-remaining-tasks-BCDE.md) — dated native testnet results and the migration decision.
- [Perp strategy](../clmm-animation/docs/perp-mm-strategy.md) — rollout gates and backtest criteria.
- [Market-neutral deployment plan](docs/hl-market-neutral-mm-deployment-plan.md) — basket calibration, funding, and unresolved live-integration work.
- [Account and subaccount naming](docs/account-naming.md) — credential routing and the HB subaccount constraints.
- [HB OPMS README](../hb-enhanced-opms/README.md) — conda deployment and credential-import procedure.
- [`PerpMMController`](../hb-enhanced-opms/src/opms/controllers/generic/perp_mm_controller.py), [`tests_live`](../hb-enhanced-opms/tests_live/), and [`run_live_testnet_gate.py`](scripts/run_live_testnet_gate.py) — current implementation and harnesses.
