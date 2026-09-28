"""Capture read-only Hyperliquid L2 book snapshots and public trades."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import aiohttp


MAINNET_WS = "wss://api.hyperliquid.xyz/ws"


def subscription_messages(coins: list[str]) -> list[dict]:
    return [
        {"method": "subscribe", "subscription": {"type": channel, "coin": coin}}
        for coin in coins
        for channel in ("l2Book", "trades")
    ]


def _count_message(counts: dict, message: dict) -> None:
    channel = message.get("channel")
    data = message.get("data")
    if channel == "l2Book" and isinstance(data, dict):
        coin = data.get("coin")
        if coin in counts:
            counts[coin]["books"] += 1
    elif channel == "trades" and isinstance(data, list):
        for trade in data:
            coin = trade.get("coin")
            if coin in counts:
                counts[coin]["trades"] += 1


async def capture(coins: list[str], duration: float, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=False)
    stream_path = output_dir / "events.jsonl"
    started_ms = int(time.time() * 1000)
    deadline = asyncio.get_running_loop().time() + duration
    counts = {coin: {"books": 0, "trades": 0} for coin in coins}
    connections = 0
    errors = []
    backoff = 1.0

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        with stream_path.open("a") as stream:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    async with session.ws_connect(MAINNET_WS, autoclose=True) as ws:
                        connections += 1
                        backoff = 1.0
                        for subscription in subscription_messages(coins):
                            await ws.send_json(subscription)
                        while asyncio.get_running_loop().time() < deadline:
                            remaining = deadline - asyncio.get_running_loop().time()
                            try:
                                message = await ws.receive(timeout=min(45.0, remaining))
                            except asyncio.TimeoutError:
                                await ws.send_json({"method": "ping"})
                                continue
                            if message.type == aiohttp.WSMsgType.TEXT:
                                payload = json.loads(message.data)
                                record = {"received_at_ms": int(time.time() * 1000), **payload}
                                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
                                stream.flush()
                                _count_message(counts, payload)
                            elif message.type in {
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            }:
                                break
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
                if asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(min(backoff, deadline - asyncio.get_running_loop().time()))
                    backoff = min(backoff * 2, 20.0)

    summary = {
        "coins": coins,
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coins", nargs="+", help="Hyperliquid symbols")
    parser.add_argument("--duration", type=float, default=3600.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0 < args.duration <= 86400:
        parser.error("--duration must be in (0, 86400]")
    coins = list(dict.fromkeys(coin.upper() for coin in args.coins))
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    output = args.output or Path("data") / f"hl_lob_{stamp}"
    summary = asyncio.run(capture(coins, args.duration, output))
    print(json.dumps(summary, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
