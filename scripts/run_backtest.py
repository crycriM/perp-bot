"""C4.1: run Backtest off fetch_hl_data.py's CSVs and print the §8.3 gate report.

Usage: python run_backtest.py BTC --data-dir data --gamma 1.0 --kappa 0.5
"""

import argparse
import asyncio
import csv
from pathlib import Path

from mm_core.contracts import MarketSnapshot
from mm_core.inventory import Caps

from perp_bot.backtest import Backtest, Backtrade, Strategy
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
    args = ap.parse_args()

    snapshots, trades, funding = load(args.coin, args.data_dir)
    config = PerpPairConfig(coin=args.coin, gamma=args.gamma, kappa=args.kappa,
                             caps=Caps(max_position=10.0, critical_position=20.0))

    bt = Backtest(config, start_equity=args.start_equity)
    bt.set_strategy(Strategy(config))
    for s in snapshots:
        bt.add_snapshot(s)
    bt.load_trades(trades)
    bt.load_funding(funding)

    duration_s = snapshots[-1].ts - snapshots[0].ts if len(snapshots) > 1 else 0.0
    asyncio.run(bt.run(duration_s=duration_s))

    report = bt.gate_report()
    for name, check in report["checks"].items():
        status = "PASS" if check["passed"] else "FAIL"
        print(f"{status:5s} {name:15s} value={check['value']:.4f} threshold={check['threshold']}")
    print("GATE:", "PASSED" if report["passed"] else "FAILED")


if __name__ == "__main__":
    main()
