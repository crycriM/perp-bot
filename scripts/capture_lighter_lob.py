"""Capture read-only Lighter L2 book snapshots and public trades.

Clones the Hyperliquid capture schema (`events.jsonl` with `l2Book` +
`trades` records) so `perp_bot.hl_lob.load_lob_capture` and the calibration
chain run on Lighter data untouched. Market data needs no credentials.

Sources:
- WS `order_book/{market_id}`: a full snapshot on subscribe, then deltas
  (changed levels only, often one side; size 0 removes a level). `BookState`
  folds them into the book and every update emits the top-N levels.
- WS `trade/{market_id}`: public trades, deduped by `trade_id` (the subscribe
  snapshot backfills recent trades, which also covers reconnect gaps).

Each market uses its own socket so `connections.jsonl` can distinguish a
market-channel failure from a host/network-wide pause.
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
DEFAULT_SYMBOLS = {"ETH": 0, "BTC": 1, "SOL": 2, "CRCL": 121, "NATGAS": 158}


def resolve_markets(symbols: tuple[str, ...]) -> dict[str, int]:
    """Map requested symbols to Lighter market ids (REST when needed)."""
    if symbols:
        return {sym.upper(): DEFAULT_SYMBOLS[sym.upper()] for sym in symbols if sym.upper() in DEFAULT_SYMBOLS}
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
        "snapshot": str(message.get("type", "")).startswith("subscribed/"),
        "bids": [[entry["price"], entry["size"]] for entry in book.get("bids", [])],
        "asks": [[entry["price"], entry["size"]] for entry in book.get("asks", [])],
    }


SYMBOL_FOR_MARKET_ID = {idx: sym for sym, idx in DEFAULT_SYMBOLS.items()}


BOOK_DEPTH = 20  # levels per emitted snapshot (HL l2Book depth)


class BookState:
    """One market's book rebuilt from the subscribe snapshot plus deltas."""

    def __init__(self) -> None:
        self.bids: dict[str, str] = {}
        self.asks: dict[str, str] = {}

    def apply(self, parsed: dict[str, Any]) -> None:
        if parsed.get("snapshot"):
            self.bids.clear()
            self.asks.clear()
        for side, levels in ((self.bids, parsed["bids"]), (self.asks, parsed["asks"])):
            for px, sz in levels:
                if float(sz) == 0:
                    side.pop(px, None)
                else:
                    side[px] = sz

    def top(self, n: int) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        bids = sorted(self.bids.items(), key=lambda kv: float(kv[0]), reverse=True)[:n]
        asks = sorted(self.asks.items(), key=lambda kv: float(kv[0]))[:n]
        return bids, asks

    def to_hl_event(self, coin: str, ts_ms: int, depth: int = BOOK_DEPTH) -> Optional[dict[str, Any]]:
        bids, asks = self.top(depth)
        if not bids or not asks:
            return None
        return {
            "channel": "l2Book",
            "data": {
                "coin": coin,
                "time": ts_ms,
                "levels": [
                    [{"px": px, "sz": sz} for px, sz in bids],
                    [{"px": px, "sz": sz} for px, sz in asks],
                ],
            },
        }


def rest_trades_to_events(rest_trades: list[dict]) -> list[dict]:
    """Convert Lighter trade rows (REST or WS, same fields) to HL trades-channel records.

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
                "tid": row.get("trade_id"),
                "usd_amount": float(row.get("usd_amount") or 0.0),
            }
        )
    return events


def ws_trades_to_events(message: dict, seen: set) -> list[dict]:
    """Unseen trades from a `trade:{id}` WS message, HL-shaped (dedupes via `seen`)."""
    if not str(message.get("channel", "")).startswith("trade:"):
        return []
    # ponytail: liquidation_trades not captured; add if liquidation flow matters for calibration
    fresh = [t for t in message.get("trades") or [] if t.get("trade_id") not in seen]
    seen.update(t.get("trade_id") for t in fresh)
    return rest_trades_to_events(fresh)


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
    connections_path = output_dir / "connections.jsonl"
    started_ms = int(time.time() * 1000)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration
    counts = {coin: {"books": 0, "trades": 0} for coin in markets}
    connections = 0
    errors = []
    connection_stats = {coin: {"connections": 0, "closes": 0, "errors": 0} for coin in markets}
    max_event_loop_lag_s = 0.0

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        with stream_path.open("a") as stream, connections_path.open("a") as connection_log:
            last_flush = loop.time()

            def write(event: dict) -> None:
                nonlocal last_flush
                record = {"received_at_ms": int(time.time() * 1000), **event}
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
                if loop.time() - last_flush >= 1:
                    stream.flush()
                    last_flush = loop.time()
                _count_message(counts, event)

            def log_connection(coin: str, event: str, **details: Any) -> None:
                connection_log.write(json.dumps({
                    "received_at_ms": int(time.time() * 1000),
                    "coin": coin,
                    "event": event,
                    **details,
                }, separators=(",", ":")) + "\n")
                connection_log.flush()

            async def capture_market(coin: str, market_id: int) -> None:
                nonlocal connections
                book = BookState()
                seen_trades: set = set()
                backoff = 1.0
                while loop.time() < deadline:
                    connected_at = None
                    close_reason = "context_exit"
                    close_code = None
                    try:
                        async with session.ws_connect(MAINNET_WS, autoclose=True) as ws:
                            connections += 1
                            connection_stats[coin]["connections"] += 1
                            connected_at = loop.time()
                            backoff = 1.0
                            log_connection(coin, "connected")
                            for kind in ("order_book", "trade"):
                                await ws.send_str(
                                    json.dumps({"type": "subscribe", "channel": f"{kind}/{market_id}"})
                                )
                            while loop.time() < deadline:
                                remaining = deadline - loop.time()
                                try:
                                    message = await ws.receive(
                                        timeout=min(1.0, max(remaining, 0.01))
                                    )
                                except asyncio.TimeoutError:
                                    continue
                                if message.type in {
                                    aiohttp.WSMsgType.CLOSE,
                                    aiohttp.WSMsgType.CLOSED,
                                    aiohttp.WSMsgType.ERROR,
                                }:
                                    close_reason = message.type.name
                                    close_code = ws.close_code
                                    break
                                if message.type != aiohttp.WSMsgType.TEXT:
                                    continue
                                payload = json.loads(message.data)
                                if payload.get("type") == "ping":
                                    await ws.send_str(json.dumps({"type": "pong"}))
                                    continue
                                parsed = parse_order_book_message(payload)
                                if parsed is not None:
                                    book.apply(parsed)
                                    event = book.to_hl_event(coin, parsed["ts_ms"] or int(time.time() * 1000))
                                    if event is not None:
                                        write(event)
                                    continue
                                trades = ws_trades_to_events(payload, seen_trades)
                                if trades:
                                    write({"channel": "trades", "data": trades})
                            if loop.time() >= deadline:
                                close_reason = "deadline"
                            close_code = ws.close_code
                    except Exception as exc:
                        close_reason = "exception"
                        error = f"{coin}: {type(exc).__name__}: {exc}"
                        errors.append(error)
                        connection_stats[coin]["errors"] += 1
                        log_connection(coin, "error", error=error)
                    finally:
                        if connected_at is not None:
                            connection_stats[coin]["closes"] += 1
                            log_connection(
                                coin,
                                "disconnected",
                                reason=close_reason,
                                close_code=close_code,
                                connected_s=round(loop.time() - connected_at, 3),
                            )
                    if loop.time() < deadline:
                        await asyncio.sleep(min(backoff, deadline - loop.time()))
                        backoff = min(backoff * 2, 20.0)

            async def monitor_event_loop() -> None:
                nonlocal max_event_loop_lag_s
                while loop.time() < deadline:
                    expected = loop.time() + .25
                    await asyncio.sleep(min(.25, max(0, deadline - loop.time())))
                    lag = max(0, loop.time() - expected)
                    max_event_loop_lag_s = max(max_event_loop_lag_s, lag)
                    if lag > 1:
                        log_connection("ALL", "event_loop_lag", lag_s=round(lag, 3))

            await asyncio.gather(
                *(capture_market(coin, market_id) for coin, market_id in markets.items()),
                monitor_event_loop(),
            )

    summary = {
        "symbols": list(markets),
        "duration_s": duration,
        "started_at_ms": started_ms,
        "finished_at_ms": int(time.time() * 1000),
        "connections": connections,
        "connection_stats": connection_stats,
        "connection_events_path": str(connections_path.resolve()),
        "max_event_loop_lag_s": round(max_event_loop_lag_s, 3),
        "counts": counts,
        "errors": errors,
        "passed": all(v["books"] > 0 and v["trades"] > 0 for v in counts.values()),
        "events_path": str(stream_path.resolve()),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+", help="Lighter symbols (ETH, BTC, SOL)")
    parser.add_argument("--duration", type=float, default=3600.0)
    parser.add_argument("--minutes", type=float, default=None)
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
