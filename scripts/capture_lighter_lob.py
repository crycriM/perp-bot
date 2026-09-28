"""Capture read-only Lighter L2 book snapshots and public trades.

Clones the Hyperliquid capture schema (`events.jsonl` with `l2Book` +
`trades` records) so `perp_bot.hl_lob.load_lob_capture` and the calibration
chain run on Lighter data untouched. Market data needs no credentials.

Sources:
- WS `order_book/{market_id}` for book updates (full top-of-book per msg is
  a delta of the venue's internal book; we forward the message's current
  side arrays as the snapshot — sufficient for top-of-book AS calibration).
- REST `/api/v1/recentTrades` polled each loop for public trades.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp

MAINNET_WS = "wss://mainnet.zklighter.elliot.ai/stream"
MAINNET_REST = "https://mainnet.zklighter.elliot.ai"
DEFAULT_SYMBOLS = {"ETH": 0, "BTC": 1, "SOL": 2}


def resolve_markets(symbols: tuple[str, ...]) -> dict[str, int]:
    """Map requested symbols to Lighter market ids (REST when needed)."""
    if symbols:
        return {sym: DEFAULT_SYMBOLS[sym.upper()] for sym in symbols if sym.upper() in DEFAULT_SYMBOLS}
    return DEFAULT_SYMBOLS


def parse_order_book_message(message: dict) -> Optional[dict[str, Any]]:
    channel = message.get("channel", "")
    if not channel.startswith("order_book:"):
        return None
    book = message.get("order_book") or {}
    market_id = int(channel.split(":", 1)[1])
    # Lighter stamps books in microseconds; the HL loader expects milliseconds.
    return {
        "coin": SYMBOL_FOR_MARKET_ID.get(market_id, f"M{market_id}"),
        "ts_ms": int(int(message.get("last_updated_at") or 0) // 1000),
        "offset": message.get("offset"),
        "bids": [[entry["price"], entry["size"]] for entry in book.get("bids", [])],
        "asks": [[entry["price"], entry["size"]] for entry in book.get("asks", [])],
    }


SYMBOL_FOR_MARKET_ID = {idx: sym for sym, idx in DEFAULT_SYMBOLS.items()}


def to_hl_book_event(parsed: Optional[dict], now_ms: int) -> Optional[dict[str, Any]]:
    """Render one parsed venue message as an HL-loader-compatible l2Book event."""
    if not parsed or not parsed["bids"] or not parsed["asks"]:
        return None
    return {
        "channel": "l2Book",
        "data": {
            "coin": parsed["coin"],
            "time": parsed["ts_ms"] or now_ms,
            "levels": [
                [{"px": px, "sz": sz} for px, sz in parsed["bids"]],
                [{"px": px, "sz": sz} for px, sz in parsed["asks"]],
            ],
        },
    }


def rest_trades_to_events(rest_trades: list[dict]) -> list[dict]:
    """Convert Lighter recentTrades rows to HL trades-channel-shaped records.

    Aggressor side: is_maker_ask=True means liquidity came off the ask, so the
    taker was the buyer -> 'B'; otherwise the taker was the seller -> 'A'.
    """
    events = []
    for row in rest_trades:
        market_id = int(row.get("market_id") or 0)
        is_maker_ask = bool(row.get("is_maker_ask"))
        events.append(
            {
                "coin": SYMBOL_FOR_MARKET_ID.get(market_id, f"M{market_id}"),
                "side": "B" if is_maker_ask else "A",
                "px": float(row["price"]) if row.get("price") else 0.0,
                "sz": float(row["size"]) if row.get("size") else 0.0,
                "time": int(row.get("timestamp") or 0),
                "trade_id": row.get("trade_id"),
                "usd_amount": float(row.get("usd_amount") or 0.0),
            }
        )
    return events


def _count_message(counts: dict, event: dict) -> None:
    data = event.get("data")
    if event["channel"] == "l2Book" and isinstance(data, dict):
        coin = data.get("coin")
        if coin in counts:
            counts[coin]["books"] += 1
    elif event["channel"] == "trades" and isinstance(data, list):
        for trade in data:
            if trade["coin"] in counts:
                counts[trade["coin"]]["trades"] += 1


async def capture(markets: dict[str, int], duration: float, output_dir: Path) -> dict:
    global SYMBOL_FOR_MARKET_ID
    SYMBOL_FOR_MARKET_ID = {idx: sym for sym, idx in markets.items()}
    output_dir.mkdir(parents=True, exist_ok=False)
    stream_path = output_dir / "events.jsonl"
    started_ms = int(time.time() * 1000)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration
    counts = {coin: {"books": 0, "trades": 0} for coin in markets}
    connections = 0
    errors = []
    backoff = 1.0

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        with stream_path.open("a") as stream:

            def write(event: dict) -> None:
                record = {"received_at_ms": int(time.time() * 1000), **event}
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
                stream.flush()
                _count_message(counts, event)

            while loop.time() < deadline:
                try:
                    async with session.ws_connect(MAINNET_WS, autoclose=True) as ws:
                        connections += 1
                        backoff = 1.0
                        for market_id in markets.values():
                            await ws.send_str(
                                json.dumps({"type": "subscribe", "channel": f"order_book/{market_id}"})
                            )
                        next_trade_poll = loop.time()
                        while loop.time() < deadline:
                            remaining = deadline - loop.time()
                            try:
                                message = await ws.receive(
                                    timeout=min(1.0, max(remaining, 0.01))
                                )
                            except asyncio.TimeoutError:
                                message = None
                            if message and message.type == aiohttp.WSMsgType.TEXT:
                                payload = json.loads(message.data)
                                parsed = parse_order_book_message(payload)
                                event = to_hl_book_event(parsed, int(time.time() * 1000))
                                if event is not None:
                                    write(event)
                            elif message and message.type in {
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            }:
                                break
                            if loop.time() >= next_trade_poll:
                                next_trade_poll = loop.time() + 5.0
                                await poll_trades(session, markets, deadline, write)

                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
                if loop.time() < deadline:
                    await asyncio.sleep(min(backoff, deadline - loop.time()))
                    backoff = min(backoff * 2, 20.0)

    summary = {
        "symbols": list(markets),
        "duration_s": duration,
        "started_at_ms": started_ms,
        "finished_at_ms": int(time.time() * 1000),
        "connections": connections,
        "counts": counts,
        "errors": errors,
        "passed": all(v["books"] > 0 and v["trades"] > 0 for v in counts.values()),
        "events_path": str(stream_path.resolve()),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


async def poll_trades(session, markets, deadline, write):
    """Poll /api/v1/recentTrades for each market and emit HL-shaped events."""
    for market_id in markets.values():
        async with session.get(
            f"{MAINNET_REST}/api/v1/recentTrades",
            params={"market_id": market_id, "limit": 100},
        ) as response:
            payload = await response.json(content_type=None)
        trades = payload.get("trades") or []
        events = rest_trades_to_events(trades)
        if events:
            write({"channel": "trades", "data": events})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+", help="Lighter symbols (ETH, BTC, SOL)")
    parser.add_argument("--duration", type=float, default=3600.0)
    parser.add_argument("--minutes", type=float, default=None)
    parser.add_argument("--trade-poll-s", type=float, default=5.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    duration = args.minutes * 60.0 if args.minutes else args.duration
    if not 0 < duration <= 86400:
        parser.error("duration must be in (0, 86400]")
    unknown = [s for s in args.symbols if s.upper() not in DEFAULT_SYMBOLS]
    if unknown:
        parser.error(f"unknown symbols (add to DEFAULT_SYMBOLS): {unknown}")
    markets = resolve_markets(tuple(args.symbols))
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    output = args.output or Path("data") / f"lighter_lob_{stamp}"
    summary = asyncio.run(capture(markets, duration, output))
    print(json.dumps(summary, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
