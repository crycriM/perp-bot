"""Fetch HL history for backtest.py (Stream C, C3).

ponytail: HL's public API has no historical L2 book or tick-level trade feed
(`l2_snapshot` is live-only, and there's no REST "recent trades" history) —
only OHLCV candles and funding are backfillable. So `mid` comes from candle
close and each candle's high/low touch becomes one synthetic Backtrade (half
the bar's volume on each side, ordered by which extreme the bar reached
first given open->close direction). This is an approximation, not real tick
data; if backtest fidelity vs. live turns out to matter, replace it with a
WS trade-stream logger run live for the fetch window.
"""

import argparse
import csv
import time
from pathlib import Path

from hyperliquid.info import Info
from hyperliquid.utils import constants


INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
# ponytail: both candles_snapshot (5000 bars/call) and funding_history (500
# records/call, hourly -> ~20.8d) are capped; page through in fixed-size
# windows and dedupe by timestamp instead of guessing one call covers the
# whole range.
MAX_BARS_PER_CALL = 5000
FUNDING_WINDOW_MS = 400 * 3_600_000  # 400 hourly records, under the 500 cap


def fetch(coin: str, days: int, interval: str, testnet: bool):
    url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
    info = Info(url, skip_ws=True)
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000

    bar_ms = INTERVAL_MS.get(interval, 60_000)
    window_ms = bar_ms * MAX_BARS_PER_CALL
    candles_by_t = {}
    t = start
    while t < end:
        window_end = min(t + window_ms, end)
        for c in info.candles_snapshot(coin, interval, t, window_end):
            candles_by_t[c["t"]] = c
        t = window_end
    candles = [candles_by_t[k] for k in sorted(candles_by_t)]

    funding_by_t = {}
    t = start
    while t < end:
        window_end = min(t + FUNDING_WINDOW_MS, end)
        for f in info.funding_history(coin, t, window_end):
            funding_by_t[f["time"]] = f
        t = window_end
    funding = [funding_by_t[k] for k in sorted(funding_by_t)]

    return candles, funding


def candle_snapshots(coin: str, candles: list[dict]):
    for c in candles:
        yield {"ts": c["t"] / 1000, "venue": "hyperliquid", "coin": coin, "mid": float(c["c"])}


def candle_trades(candles: list[dict]):
    for c in candles:
        ts = c["t"] / 1000
        half = float(c["v"]) / 2
        o, h, l, cl = float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"])
        first, second = (l, h) if cl >= o else (h, l)
        first_side, second_side = ("sell", "buy") if cl >= o else ("buy", "sell")
        yield {"ts": ts, "side": first_side, "price": first, "size": half}
        yield {"ts": ts + 0.001, "side": second_side, "price": second, "size": half}


def funding_events(funding: list[dict]):
    for f in funding:
        yield {"ts": f["time"] / 1000, "rate": float(f["fundingRate"])}


def write_csv(path: Path, rows, fieldnames: list[str]):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("coin", help="HL coin symbol, e.g. BTC, ETH, SOL")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--interval", default="15m",
                     help="HL candle interval; HL's retention window shrinks with finer "
                          "granularity (~3.5d at 1m, ~15d at 5m, 30+ at 15m/1h) — pick one "
                          "that covers --days or older bars silently come back empty")
    ap.add_argument("--out", default="data", help="output directory")
    ap.add_argument("--testnet", action="store_true")
    args = ap.parse_args()

    candles, funding = fetch(args.coin, args.days, args.interval, args.testnet)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    write_csv(outdir / f"{args.coin}_snapshots.csv", candle_snapshots(args.coin, candles),
              ["ts", "venue", "coin", "mid"])
    write_csv(outdir / f"{args.coin}_trades.csv", candle_trades(candles),
              ["ts", "side", "price", "size"])
    write_csv(outdir / f"{args.coin}_funding.csv", funding_events(funding),
              ["ts", "rate"])

    print(f"{args.coin}: {len(candles)} candles, {len(funding)} funding events -> {outdir}/")


if __name__ == "__main__":
    main()
