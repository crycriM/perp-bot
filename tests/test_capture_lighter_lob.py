"""Tests for the Lighter L2/trades capture harness (synthetic fixtures)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from capture_lighter_lob import (
    parse_order_book_message,
    rest_trades_to_events,
    to_hl_book_event,
)


BOOK_MSG = {
    "channel": "order_book:0",
    "last_updated_at": 1790608177458063,
    "offset": 13394388,
    "order_book": {
        "code": 0,
        "asks": [{"price": "2671.37", "size": "4.4516"}],
        "bids": [{"price": "2671.20", "size": "1.2"}],
    },
}


class TestOrderBookParser:
    def test_parses_symbol_and_levels(self):
        parsed = parse_order_book_message(BOOK_MSG)
        assert parsed["coin"] == "ETH"
        assert parsed["ts_ms"] == 1790608177458  # venue µs -> loader-ms
        assert parsed["bids"] == [["2671.20", "1.2"]]
        assert parsed["asks"] == [["2671.37", "4.4516"]]

    def test_ignores_unknown_channel(self):
        assert parse_order_book_message({"channel": "connected"}) is None

    def test_hl_snapshot_shape_matches_loader(self):
        parsed = parse_order_book_message(BOOK_MSG)
        event = to_hl_book_event(parsed, now_ms=1500)
        assert event["channel"] == "l2Book"
        data = event["data"]
        assert data["coin"] == "ETH"
        assert data["time"] == 1790608177458
        bids, asks = data["levels"]
        assert bids == [{"px": "2671.20", "sz": "1.2"}]
        assert asks == [{"px": "2671.37", "sz": "4.4516"}]
        # loader-safe: exactly two non-empty sides
        assert bids and asks


class TestRestTrades:
    def test_trades_csv_row_shape(self):
        rest = [
            {
                "trade_id": 32381439439,
                "market_id": 0,
                "size": "0.0900",
                "price": "2671.14",
                "timestamp": 1790608177123,
                "is_maker_ask": True,
            }
        ]
        events = rest_trades_to_events(rest)
        row = events[0]
        assert row["coin"] == "ETH"
        assert row["side"] == "B"  # maker on ask -> buyer is the aggressor
        assert row["px"] == 2671.14
        assert row["sz"] == 0.09
        assert row["time"] == 1790608177123

    def test_taker_sell_side(self):
        row = {"trade_id": 1, "market_id": 1, "size": "0.02", "price": "80000.1",
               "timestamp": 5, "is_maker_ask": False}
        events = rest_trades_to_events([row])
        assert events[0]["coin"] == "BTC"
        assert events[0]["side"] == "A"
