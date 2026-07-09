"""C4.1: run Backtest off fetch_hl_data.py's CSVs and print the §8.3 gate report.

Usage: python run_backtest.py BTC --data-dir data --gamma 1.0 --kappa 0.5
"""

import argparse
import asyncio
import csv
from pathlib import Path

from mm_core.contracts import MarketSnapshot
from mm_core.inventory import Caps

from perp_bot.backtest import Backtest, Backtrade, Strategy, infer_tick_s_from_snapshots
from perp_bot.config import PerpPairConfig


def load(coin: str, data_dir: Path):
    with open(data_dir / f"{coin}_snapshots.csv") as fh:
        snapshots = [MarketSnapshot(venue=r["venue"], coin=r["coin"], ts=float(r["ts"]), mid=float(r["mid"]))
                     for r in csv.DictReader(fh)]
    with open(data_dir / f"{coin}_trades.csv") as fh:
        trades = [Backtrade(ts=float(r["ts"]), side=r["side"], price=float(r["price"]), size=float(r["size"]))
                  for r in csv.DictReader(fh)]
    with open(data_dir / f"{coin}_funding.csv") as fh:
        funding = [{"ts": float(r["ts"]), "rate": float(r["rate"])} for r in csv.DictReader(fh)]
    return snapshots, trades, funding


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("coin")
    ap.add_argument("--data-dir", default="data", type=Path)
    ap.add_argument("--gamma", type=float, default=1.0)
    ap.add_argument("--kappa", type=float, default=0.5)
    ap.add_argument("--start-equity", type=float, default=10_000.0)
    ap.add_argument("--max-position", type=float, default=10.0,
                    help="Inventory cap in base units; also sets quote size at 10%% "
                         "of this cap inside Strategy.")
    ap.add_argument("--critical-position", type=float, default=None,
                    help="Critical inventory threshold in base units. Defaults "
                         "to 2x --max-position.")
    ap.add_argument("--tick-s", type=float, default=None,
                    help="Replay step in seconds. Defaults to the dataset's smallest "
                         "positive snapshot gap, which makes candle-based HL fetches "
                         "complete in finite time.")
    ap.add_argument("--decision-log-path", type=Path, default=None,
                    help="Optional JSONL path to write one backtest decision/intention "
                         "record per replay step for shadow-vs-backtest diffing.")
    args = ap.parse_args()

    critical_position = (
        args.critical_position
        if args.critical_position is not None else args.max_position * 2
    )
    snapshots, trades, funding = load(args.coin, args.data_dir)
    config = PerpPairConfig(coin=args.coin, gamma=args.gamma, kappa=args.kappa,
                             caps=Caps(max_position=args.max_position,
                                       critical_position=critical_position))

    bt = Backtest(
        config,
        start_equity=args.start_equity,
        decision_log_path=str(args.decision_log_path) if args.decision_log_path else None,
    )
    bt.set_strategy(Strategy(config))
    for s in snapshots:
        bt.add_snapshot(s)
    bt.load_trades(trades)
    bt.load_funding(funding)

    tick_s = args.tick_s or infer_tick_s_from_snapshots(snapshots)
    duration_s = (
        snapshots[-1].ts - snapshots[0].ts + tick_s
        if len(snapshots) > 1 else tick_s
    )
    asyncio.run(bt.run(duration_s=duration_s, tick_s=tick_s))

    report = bt.gate_report()
    print(
        f"DATA coin={args.coin} snapshots={len(snapshots)} trades={len(trades)} "
        f"funding={len(funding)} tick_s={tick_s} "
        f"max_position={args.max_position} critical_position={critical_position}"
    )
    for name, check in report["checks"].items():
        status = "PASS" if check["passed"] else "FAIL"
        print(f"{status:5s} {name:15s} value={check['value']:.4f} threshold={check['threshold']}")
    print("GATE:", "PASSED" if report["passed"] else "FAILED")


if __name__ == "__main__":
    main()
