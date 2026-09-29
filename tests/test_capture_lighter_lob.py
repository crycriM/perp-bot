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


# --- 2026-09-29: WS deltas must be folded into a book; trades from WS, deduped ---

from capture_lighter_lob import BookState, ws_trades_to_events  # noqa: E402


def _msg(kind, bids=(), asks=(), ts_us=1790608177458063):
    return {
        "type": f"{kind}/order_book",
        "channel": "order_book:0",
        "last_updated_at": ts_us,
        "order_book": {
            "bids": [{"price": p, "size": s} for p, s in bids],
            "asks": [{"price": p, "size": s} for p, s in asks],
        },
    }


class TestBookState:
    def test_snapshot_then_one_sided_delta_keeps_other_side(self):
        book = BookState()
        book.apply(parse_order_book_message(_msg("subscribed", bids=[("99", "1"), ("100", "2")],
                                                 asks=[("101", "3"), ("102", "4")])))
        book.apply(parse_order_book_message(_msg("update", asks=[("101", "0"), ("100.5", "5")])))
        bids, asks = book.top(20)
        assert bids == [("100", "2"), ("99", "1")]          # best first, untouched by an asks-only delta
        assert asks == [("100.5", "5"), ("102", "4")]       # size 0 removed the 101 level

    def test_snapshot_resets_state(self):
        book = BookState()
        book.apply(parse_order_book_message(_msg("subscribed", bids=[("99", "1")], asks=[("101", "1")])))
        book.apply(parse_order_book_message(_msg("subscribed", bids=[("98", "1")], asks=[("103", "1")])))
        assert book.top(20) == ([("98", "1")], [("103", "1")])

    def test_top_n_limit(self):
        book = BookState()
        book.apply(parse_order_book_message(_msg(
            "subscribed", bids=[(str(90 + i), "1") for i in range(10)], asks=[(str(200 + i), "1") for i in range(10)])))
        bids, asks = book.top(3)
        assert [p for p, _ in bids] == ["99", "98", "97"]
        assert [p for p, _ in asks] == ["200", "201", "202"]

    def test_full_book_event_from_state(self):
        book = BookState()
        parsed = parse_order_book_message(_msg("subscribed", bids=[("99", "1")], asks=[("101", "2")]))
        book.apply(parsed)
        event = book.to_hl_event("ETH", parsed["ts_ms"])
        assert event["data"]["levels"] == [[{"px": "99", "sz": "1"}], [{"px": "101", "sz": "2"}]]
        assert event["data"]["time"] == 1790608177458


class TestWsTrades:
    MSG = {"type": "update/trade", "channel": "trade:1", "trades": [
        {"trade_id": 7, "market_id": 1, "size": "0.02", "price": "80000.1", "timestamp": 5, "is_maker_ask": False},
        {"trade_id": 8, "market_id": 1, "size": "0.01", "price": "80000.2", "timestamp": 6, "is_maker_ask": True},
    ]}

    def test_dedupes_by_trade_id_and_emits_tid(self):
        seen: set = set()
        first = ws_trades_to_events(self.MSG, seen)
        again = ws_trades_to_events(self.MSG, seen)
        assert [t["tid"] for t in first] == [7, 8]
        assert again == []
        assert first[0]["coin"] == "BTC" and first[0]["side"] == "A"

    def test_ignores_non_trade_channels(self):
        assert ws_trades_to_events({"channel": "order_book:0"}, set()) == []


def test_loader_dedupes_on_lighter_trade_id(tmp_path):
    """Old tapes carry `trade_id`, not `tid`: distinct same-ms equal-size fills must survive."""
    import json
    from perp_bot.hl_lob import load_lob_capture

    book = {"channel": "l2Book", "data": {"coin": "ETH", "time": 1000,
            "levels": [[{"px": "99", "sz": "1"}], [{"px": "101", "sz": "1"}]]}}
    book2 = {**book, "data": {**book["data"], "time": 3000}}
    row = {"coin": "ETH", "side": "B", "px": 100.0, "sz": 0.1, "time": 2000}
    trades = {"channel": "trades", "data": [{**row, "trade_id": 1}, {**row, "trade_id": 2}, {**row, "trade_id": 1}]}
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in (book, trades, book2)) + "\n")
    assert len(load_lob_capture(path, "ETH").trades) == 2
