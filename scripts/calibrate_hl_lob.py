"""Fee/latency-aware GLFT versus fixed-spread screening. Never places orders.

Train 60%: sigma and public penetration A/k proxy. Validation 20%: select.
Holdout 20%: evaluate selected policy once + stress. A pass permits only a
bounded micro-soak, not scaling; own-order survival fitting comes next.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import time
from pathlib import Path

from mm_core.regime import GateConfig
from mm_core.risk_policy import RiskConfig
from perp_bot.calibration import capture_quality, choose_candidate, estimate_parameters, source_fingerprint
from perp_bot.hl_lob import load_lob_capture, split_lob_replay
from perp_bot.lob_calibration import _slice_passes, replay_candidate

# Screen approval must agree with the stop the prepared live bundle enforces
# (hb-enhanced-opms prepare_calibrated_soak sets MAX_DRAWDOWN_PCT=1.0), or a
# candidate could pass screening yet be force-stopped on day one.
LIVE_DD_STOP_FRACTION = 0.01


async def calibrate_coin(args, coin):
    data = load_lob_capture(args.capture_dir / "events.jsonl", coin)
    quality = capture_quality(data)
    result = dict(coin=coin, quality=quality, approved_for_micro_soak=False, selected=None)
    if len(data.books) < 15:
        return result | {"error": "insufficient books"}
    train, remainder = split_lob_replay(data, .6)
    validation, holdout = split_lob_replay(remainder, .5)
    rules = json.loads((args.rules_dir / f"{coin}_rules.json").read_text())
    result["venue_rules"] = rules
    funding_path = args.rules_dir / f"{coin}_funding.csv"
    funding = []
    if funding_path.exists():
        with funding_path.open() as stream:
            funding = [dict(ts=float(r["ts"]), rate=float(r["rate"])) for r in csv.DictReader(stream)]
    expected_hours = set(range(math.floor(data.books[0].ts / 3600) + 1,
                               math.floor(data.books[-1].ts / 3600) + 1))
    recorded_hours = {round(f["ts"] / 3600) for f in funding}
    if not funding_path.exists() or not expected_hours.issubset(recorded_hours):
        quality["failures"].append("funding_coverage")
        quality["passed"] = False
    try:
        estimate = estimate_parameters(train)
    except ValueError as exc:
        return result | {"error": str(exc)}
    result["estimates"] = estimate
    # Conservative latency allowance, not an assertion of Gaussian tails.
    latency_edge = 1.64 * estimate["sigma_bps_sqrt_s"] * math.sqrt(
        args.decision_interval_s + args.cancel_latency_s + (quality["receipt_lag_p95_s"] or 0))
    common_policy = dict(exchange=args.venue, target_inventory=0, leverage=3,
        size_step=float(rules["size_step"]), max_market_data_age_s=2,
        min_quote_notional=float(rules["min_notional"]), maker_fee_bps=args.maker_fee_bps,
        taker_fee_bps=args.taker_fee_bps, min_edge_bps=max(args.min_edge_bps, latency_edge + 2),
        quote_reprice_bps=2, quote_post_only_buffer_bps=1,
        cancel_latency_s=args.cancel_latency_s, place_latency_s=args.place_latency_s,
        close_slippage_bps=args.close_slippage_bps)
    # Same risk path live/replay: this controlled experiment disables regime,
    # but retains toxic markout and inventory/margin safety.
    risk = RiskConfig(gate=GateConfig(regime_stop=False), toxic_markout_bps=-1)
    common = dict(coin=coin, quote_size=float(rules["quote_size"]),
        price_tick=float(rules["price_tick"]), start_equity=args.start_equity,
        tick_s=.25, decision_interval_s=args.decision_interval_s,
        quote_refresh_s=5, max_position_multiple=5, funding=funding)
    candidates = [dict(pricing_model="fixed", gamma=.02, kappa=estimate["kappa_per_bps"],
                       fixed_half_spread_bps=half) for half in (6, 10, 16, 24)]
    candidates += [dict(pricing_model="glft", gamma=gamma, kappa=estimate["kappa_per_bps"],
        arrival_rate_per_s=estimate["arrival_rate_per_s"] * factor,
        sigma_bps_sqrt_s=estimate["sigma_bps_sqrt_s"])
        for gamma in (.005, .02, .1, .5) for factor in (.1, .25, 1)]
    async def replay(slice_, params, stress=False):
        policy = common_policy | {k: v for k, v in params.items() if k not in {"gamma", "kappa"}}
        if stress:
            policy["cancel_latency_s"] *= 2
            policy["place_latency_s"] *= 2
            policy["close_slippage_bps"] *= 2
        return await replay_candidate(slice_, gamma=params["gamma"], kappa=params["kappa"],
                                      policy=policy | {"risk": risk}, **common)
    rows = []
    for params in candidates:
        rows.append(dict(parameters=params, validation=await replay(validation, params)))
    eligible = [r for r in rows
                if _slice_passes(r["validation"], args.min_fills, LIVE_DD_STOP_FRACTION)]
    # Do not inspect additional holdouts hoping to rescue a failed candidate.
    selected = choose_candidate(eligible or rows)
    params = selected["parameters"]
    selected["holdout"] = await replay(holdout, params)
    selected["stress_holdout"] = await replay(holdout, params, stress=True)
    stress = selected["stress_holdout"]
    approved = (quality["passed"] and bool(eligible)
                and _slice_passes(selected["holdout"], args.min_fills, LIVE_DD_STOP_FRACTION)
                and stress["net_pnl"] > 0
                and stress["max_drawdown"] < LIVE_DD_STOP_FRACTION
                and stress["max_initial_margin_fraction"] < .5)
    result.update(selected=selected, candidates=rows, policy=common_policy,
        approved_for_micro_soak=approved, regime_stop=False, toxic_markout_bps=-1,
        update_interval=args.decision_interval_s, quote_refresh_interval=5,
        max_market_data_age_s=2, start_equity=args.start_equity, max_position_multiple=5,
        split_sizes={"train": len(train.books), "validation": len(validation.books), "holdout": len(holdout.books)})
    return result


async def run(args):
    results = [await calibrate_coin(args, coin) for coin in args.coins]
    with (args.capture_dir / "events.jsonl").open("rb") as stream:
        capture_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    return dict(schema_version=2, created_at=time.time(), source_fingerprint=source_fingerprint(), capture_sha256=capture_hash,
        capture_path=str((args.capture_dir / "events.jsonl").resolve()),
        passed=all(r["approved_for_micro_soak"] for r in results), results=results,
        units="seconds; bps; inventory in nominal quote lots; A per side per second",
        limitations=["Public A is a penetration proxy, not measured own-order fill intensity.",
                     "Visible-queue replay omits hidden queue and full market impact.",
                     "No liquidation engine model; initial-margin usage screens capital risk.",
                     "L2 quality, measured latency, fees and holdout gates precede micro-live."])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_dir", type=Path)
    parser.add_argument("coins", nargs="+", type=str.upper)
    parser.add_argument("--venue", choices=("hyperliquid", "lighter"), default="hyperliquid")
    parser.add_argument("--rules-dir", type=Path, required=True)
    parser.add_argument("--maker-fee-bps", type=float, required=True)
    parser.add_argument("--taker-fee-bps", type=float, required=True)
    parser.add_argument("--cancel-latency-s", type=float, required=True, help="measured p95 including HB cancel confirmation")
    parser.add_argument("--place-latency-s", type=float, required=True, help="measured p95 submit to live order")
    parser.add_argument("--decision-interval-s", type=float, default=1)
    parser.add_argument("--close-slippage-bps", type=float, default=2)
    parser.add_argument("--min-edge-bps", type=float, default=3)
    parser.add_argument("--min-fills", type=int, default=100)
    parser.add_argument("--start-equity", type=float, default=300)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    for key in ("cancel_latency_s", "place_latency_s", "decision_interval_s", "min_fills", "start_equity"):
        if not math.isfinite(getattr(args, key)) or getattr(args, key) <= 0:
            parser.error(f"{key} must be finite and positive")
    report = asyncio.run(run(args))
    path = args.output or args.capture_dir / "calibration_v2.json"
    def finite(value):
        if isinstance(value, float) and not math.isfinite(value): return None
        if isinstance(value, dict): return {k: finite(v) for k, v in value.items()}
        if isinstance(value, list): return [finite(v) for v in value]
        return value
    path.write_text(json.dumps(finite(report), indent=2, allow_nan=False) + "\n")
    for r in report["results"]:
        print(r["coin"], "PASS (micro only)" if r["approved_for_micro_soak"] else "BLOCKED",
              r["quality"]["failures"], r.get("error", ""))
    print(path)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
