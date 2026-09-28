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


def _slice_lob_replay(
    data: LobReplayData, start: float, end: float, *, include_end: bool,
) -> LobReplayData:
    contains = (lambda ts: start <= ts <= end) if include_end else (lambda ts: start <= ts < end)
    books = tuple(book for book in data.books if contains(book.ts))
    snapshots = tuple(snapshot for snapshot in data.snapshots if contains(snapshot.ts))
    trades = tuple(trade for trade in data.trades if contains(trade.ts))
    return LobReplayData(snapshots, books, trades)


def split_lob_replay(
    data: LobReplayData, train_fraction: float = 2.0 / 3.0,
) -> tuple[LobReplayData, LobReplayData]:
    """Chronologically split a capture without leaking boundary events."""
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be in (0, 1)")
    if len(data.books) < 2:
        raise ValueError("at least two books are required")
    start, end = data.books[0].ts, data.books[-1].ts
    cutoff = start + (end - start) * train_fraction
    return (
        _slice_lob_replay(data, start, cutoff, include_end=False),
        _slice_lob_replay(data, cutoff, end, include_end=True),
    )


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
