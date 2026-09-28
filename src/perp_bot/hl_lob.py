"""Parse captured Hyperliquid L2/trade JSONL into replay objects."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from mm_core.contracts import MarketSnapshot

from perp_bot.backtest import BacktestBook, Backtrade


@dataclass(frozen=True)
class LobReplayData:
    snapshots: tuple[MarketSnapshot, ...]
    books: tuple[BacktestBook, ...]
    trades: tuple[Backtrade, ...]


def load_lob_capture(path: Path, coin: str) -> LobReplayData:
    """Load one coin, excluding subscription-backfill trades outside book time."""
    coin = coin.upper()
    books: list[BacktestBook] = []
    trades_by_id: dict[object, Backtrade] = {}

    with path.open() as stream:
        for line in stream:
            record = json.loads(line)
            channel = record.get("channel")
            data = record.get("data")
            if channel == "l2Book" and isinstance(data, dict) and data.get("coin") == coin:
                levels = data.get("levels", [])
                if len(levels) != 2 or not levels[0] or not levels[1]:
                    continue
                bids = tuple((float(level["px"]), float(level["sz"])) for level in levels[0])
                asks = tuple((float(level["px"]), float(level["sz"])) for level in levels[1])
                books.append(BacktestBook(ts=float(data["time"]) / 1000.0,
                                          bids=bids, asks=asks))
            elif channel == "trades" and isinstance(data, list):
                for row in data:
                    if row.get("coin") != coin or row.get("side") not in {"A", "B"}:
                        continue
                    trade = Backtrade(
                        ts=float(row["time"]) / 1000.0,
                        side="buy" if row["side"] == "B" else "sell",
                        price=float(row["px"]),
                        size=float(row["sz"]),
                    )
                    trades_by_id[row.get("tid", (trade.ts, trade.side, trade.price, trade.size))] = trade

    books.sort(key=lambda book: book.ts)
    if not books:
        return LobReplayData((), (), ())
    start, end = books[0].ts, books[-1].ts
    trades = tuple(sorted(
        (trade for trade in trades_by_id.values() if start <= trade.ts <= end),
        key=lambda trade: trade.ts,
    ))
    snapshots = tuple(MarketSnapshot(
        venue="hyperliquid", coin=coin, ts=book.ts, mid=book.mid,
    ) for book in books)
    return LobReplayData(snapshots, tuple(books), trades)
