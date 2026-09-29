"""Parse captured Hyperliquid L2/trade JSONL into replay objects."""

from __future__ import annotations

import json
import math
from collections import deque
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


MISMATCH_LIMIT = 3  # consecutive l2Book snapshots whose touch disagrees with the bbo stream


class BookOverlay:
    """HL's public l2Book is throttled to ~5.5 s while `bbo` runs at block cadence.

    Keep the latest l2 depth and put the latest bbo touch on top of it. Shared by
    the replay loader and the live Hummingbot connector patch so both see the same
    book. Depth behind the touch is still up to ~5.5 s old.
    """

    def __init__(self):
        self._bids = self._asks = None
        self._bbo = None
        self._history = deque(maxlen=128)  # (time_ms, bid_px, ask_px)
        self._mismatch = 0

    @property
    def healthy(self) -> bool:
        """False once the bbo stream has silently stopped tracking the venue."""
        return self._mismatch < MISMATCH_LIMIT

    def on_l2(self, time_ms, bids, asks):
        # The last bbo at/before the snapshot must show the snapshot's own touch.
        seen = next((h for h in reversed(self._history) if h[0] <= time_ms), None)
        self._mismatch = 0 if seen and seen[1:] == (bids[0][0], asks[0][0]) else self._mismatch + 1
        self._bids, self._asks = tuple(bids), tuple(asks)
        if self._bbo and self._bbo[0] > time_ms:
            return self._apply()
        return self._bids, self._asks

    def on_bbo(self, time_ms, bid, ask):
        if bid is None or ask is None:
            return None
        if bid[0] >= ask[0]:
            raise ValueError("crossed bbo")
        self._bbo = (time_ms, bid, ask)
        self._history.append((time_ms, bid[0], ask[0]))
        return None if self._bids is None else self._apply()

    def _apply(self):
        _, bid, ask = self._bbo
        return ((bid,) + tuple(x for x in self._bids if x[0] < bid[0]),
                (ask,) + tuple(x for x in self._asks if x[0] > ask[0]))


def load_lob_capture(path: Path, coin: str) -> LobReplayData:
    """Load one coin, excluding subscription-backfill trades outside book time."""
    coin = coin.upper()
    books: list[BacktestBook] = []
    trades_by_id: dict[object, Backtrade] = {}
    overlay = BookOverlay()

    def add_book(time_ms, received, bids, asks):
        exchange_ts = float(time_ms) / 1000.0
        books.append(BacktestBook(ts=max(exchange_ts, float(received) / 1000) if received else exchange_ts,
                                  bids=bids, asks=asks, exchange_ts=exchange_ts if received else None))

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
                if (any(not math.isfinite(p) or not math.isfinite(s) or p <= 0 or s <= 0 for p, s in bids + asks)
                        or bids[0][0] >= asks[0][0]
                        or list(bids) != sorted(bids, reverse=True) or list(asks) != sorted(asks)):
                    raise ValueError(f"invalid/crossed/unsorted book for {coin}")
                add_book(data["time"], record.get("received_at_ms"), *overlay.on_l2(data["time"], bids, asks))
            elif channel == "bbo" and isinstance(data, dict) and data.get("coin") == coin:
                bid, ask = (None if not lvl else (float(lvl["px"]), float(lvl["sz"])) for lvl in data["bbo"])
                merged = overlay.on_bbo(data["time"], bid, ask)
                if merged:
                    add_book(data["time"], record.get("received_at_ms"), *merged)
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
                    key = row.get("tid", row.get("trade_id"))  # HL tid / Lighter trade_id
                    trades_by_id[key if key is not None else (trade.ts, trade.side, trade.price, trade.size)] = trade

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
