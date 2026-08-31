"""Fetch HL history for backtest.py — direct REST API version.

Bypasses the hyperliquid SDK's Info class initialization issues by calling
HL's public REST endpoints directly.
"""

import argparse
import csv
import time
from pathlib import Path

import requests

MAINNET_API = "https://api.hyperliquid.xyz"
TESTNET_API = "https://api.hyperliquid-testnet.xyz"

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
MAX_BARS_PER_CALL = 5000
FUNDING_WINDOW_MS = 400 * 3_600_000


def fetch(coin: str, days: int, interval: str, testnet: bool):
    base_url = TESTNET_API if testnet else MAINNET_API
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000

    bar_ms = INTERVAL_MS.get(interval, 60_000)
    window_ms = bar_ms * MAX_BARS_PER_CALL
    
    candles_by_t = {}
    t = start
    while t < end:
        window_end = min(t + window_ms, end)
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": t,
                "endTime": window_end,
            }
        }
        resp = requests.post(f"{base_url}/info", json=payload)
        resp.raise_for_status()
        for c in resp.json():
            candles_by_t[c["t"]] = c
        t = window_end
    candles = [candles_by_t[k] for k in sorted(candles_by_t)]

    funding_by_t = {}
    t = start
    while t < end:
        window_end = min(t + FUNDING_WINDOW_MS, end)
        payload = {
            "type": "fundingHistory",
            "coin": coin,
            "startTime": t,
            "endTime": window_end,
        }
        resp = requests.post(f"{base_url}/info", json=payload)
        resp.raise_for_status()
        for f in resp.json():
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
