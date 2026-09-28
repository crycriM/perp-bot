"""Fetch HL history for backtest.py. Calls HL's public REST endpoints
directly (no hyperliquid SDK dependency)."""

import argparse
import csv
import json
import time
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

import requests

MAINNET_API = "https://api.hyperliquid.xyz"
TESTNET_API = "https://api.hyperliquid-testnet.xyz"

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
MAX_BARS_PER_CALL = 5000
FUNDING_WINDOW_MS = 400 * 3_600_000


def derive_venue_rules(
    asset: dict, context: dict, *, quote_notional: float = 25.0,
    min_notional: float = 10.0,
) -> dict:
    """Derive HL's executable size grid and five-significant-figure tick."""
    mark = Decimal(context["markPx"])
    if mark <= 0 or quote_notional < min_notional:
        raise ValueError("mark must be positive and quote notional must meet the venue minimum")
    size_step = Decimal(1).scaleb(-int(asset["szDecimals"]))
    max_price_decimals = 6 - int(asset["szDecimals"])
    tick_exponent = max(mark.adjusted() - 4, -max_price_decimals)
    price_tick = Decimal(1).scaleb(tick_exponent)

    def size_for(notional: float) -> Decimal:
        units = (Decimal(str(notional)) / mark / size_step).to_integral_value(
            rounding=ROUND_CEILING,
        )
        return units * size_step

    return {
        "coin": asset["name"],
        "mark_price": float(mark),
        "size_step": float(size_step),
        "price_tick": float(price_tick),
        "min_notional": float(min_notional),
        "min_order_size": float(size_for(min_notional)),
        "quote_notional": float(quote_notional),
        "quote_size": float(size_for(quote_notional)),
    }


def fetch_venue_rules(base_url: str, coin: str, quote_notional: float) -> dict:
    resp = requests.post(f"{base_url}/info", json={"type": "metaAndAssetCtxs"})
    resp.raise_for_status()
    meta, contexts = resp.json()
    for asset, context in zip(meta["universe"], contexts):
        if asset["name"] == coin:
            return derive_venue_rules(asset, context, quote_notional=quote_notional)
    raise ValueError(f"Hyperliquid market not found: {coin}")


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
        yield {"ts": ts, "side": second_side, "price": second, "size": half}


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
    ap.add_argument("--quote-notional", type=float, default=25.0,
                    help="calibration quote notional; rounded up to the live size grid")
    ap.add_argument("--testnet", action="store_true")
    args = ap.parse_args()

    candles, funding = fetch(args.coin, args.days, args.interval, args.testnet)
    base_url = TESTNET_API if args.testnet else MAINNET_API
    rules = fetch_venue_rules(base_url, args.coin, args.quote_notional)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    write_csv(outdir / f"{args.coin}_snapshots.csv", candle_snapshots(args.coin, candles),
              ["ts", "venue", "coin", "mid"])
    write_csv(outdir / f"{args.coin}_trades.csv", candle_trades(candles),
              ["ts", "side", "price", "size"])
    write_csv(outdir / f"{args.coin}_funding.csv", funding_events(funding),
              ["ts", "rate"])
    (outdir / f"{args.coin}_rules.json").write_text(
        json.dumps(rules, indent=2, sort_keys=True) + "\n"
    )

    print(f"{args.coin}: {len(candles)} candles, {len(funding)} funding events, "
          f"quote_size={rules['quote_size']:g}, tick={rules['price_tick']:g} -> {outdir}/")


if __name__ == "__main__":
    main()
