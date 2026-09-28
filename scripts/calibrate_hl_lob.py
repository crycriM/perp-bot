"""Calibrate Guéant parameters on a captured HL L2/trade stream.

Each cell must independently pass the rollout gates on chronological train
and untouched out-of-sample slices. No passing cell means no live soak.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from perp_bot.hl_lob import load_lob_capture, split_lob_replay
from perp_bot.lob_calibration import candidate_passes, rank_candidate, replay_candidate


def _floats(value: str) -> list[float]:
    values = [float(item) for item in value.split(",")]
    if not values or any(item <= 0 for item in values):
        raise argparse.ArgumentTypeError("values must be a comma-separated positive list")
    return values


async def calibrate_coin(args, coin: str) -> dict:
    capture = load_lob_capture(args.capture_dir / "events.jsonl", coin)
    train, oos = split_lob_replay(capture, args.train_fraction)
    rules = json.loads((args.rules_dir / f"{coin}_rules.json").read_text())
    rows = []
    for gamma in args.gammas:
        for kappa in args.kappas:
            common = {
                "coin": coin,
                "gamma": gamma,
                "kappa": kappa,
                "quote_size": float(rules["quote_size"]),
                "price_tick": float(rules["price_tick"]),
                "start_equity": args.start_equity,
                "tick_s": args.tick_s,
                "max_position_multiple": args.max_position_multiple,
            }
            train_metrics = await replay_candidate(train, **common)
            oos_metrics = await replay_candidate(oos, **common)
            row = {
                "gamma": gamma,
                "kappa": kappa,
                "train": train_metrics,
                "oos": oos_metrics,
            }
            row["passed"] = candidate_passes(train_metrics, oos_metrics, args.min_fills)
            rows.append(row)
    passing = sorted((row for row in rows if row["passed"]), key=rank_candidate, reverse=True)
    return {
        "coin": coin,
        "capture": {
            "books": len(capture.books),
            "trades": len(capture.trades),
            "duration_s": capture.books[-1].ts - capture.books[0].ts,
            "train_books": len(train.books),
            "train_trades": len(train.trades),
            "oos_books": len(oos.books),
            "oos_trades": len(oos.trades),
        },
        "venue_rules": rules,
        "min_fills_per_slice": args.min_fills,
        "passing_cells": len(passing),
        "selected": passing[0] if passing else None,
        "candidates": rows,
    }


async def run(args) -> dict:
    results = [await calibrate_coin(args, coin) for coin in args.coins]
    return {
        "passed": all(result["selected"] is not None for result in results),
        "train_fraction": args.train_fraction,
        "tick_s": args.tick_s,
        "start_equity": args.start_equity,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_dir", type=Path)
    parser.add_argument("coins", nargs="+", type=str.upper)
    parser.add_argument("--rules-dir", type=Path, default=Path("data/5m"))
    parser.add_argument("--gammas", type=_floats,
                        default=_floats("0.25,0.5,1,2,3,5,8"))
    parser.add_argument("--kappas", type=_floats,
                        default=_floats("1000,2000,3000,5000,8000,12000,20000"))
    parser.add_argument("--train-fraction", type=float, default=2.0 / 3.0)
    parser.add_argument("--tick-s", type=float, default=0.5)
    parser.add_argument("--min-fills", type=int, default=10)
    parser.add_argument("--start-equity", type=float, default=300.0)
    parser.add_argument("--max-position-multiple", type=float, default=5.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.tick_s <= 0 or args.min_fills <= 0 or args.start_equity <= 0:
        parser.error("tick, min fills, and start equity must be positive")
    report = asyncio.run(run(args))
    output = args.output or args.capture_dir / "calibration_report.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    for result in report["results"]:
        selected = result["selected"]
        if selected:
            print(f"{result['coin']}: PASS gamma={selected['gamma']:g} "
                  f"kappa={selected['kappa']:g} cells={result['passing_cells']}")
        else:
            print(f"{result['coin']}: FAIL cells=0")
    print(f"CALIBRATION: {'PASSED' if report['passed'] else 'FAILED'} -> {output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
