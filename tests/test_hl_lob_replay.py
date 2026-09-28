import asyncio
import json

import pytest

from mm_core.contracts import ExecIntent, QuoteSpec
from mm_core.inventory import Caps

from perp_bot.backtest import Backtest, BacktestBook, Backtrade
from perp_bot.config import PerpPairConfig
from perp_bot.hl_lob import load_lob_capture


def _write_events(path, events):
    path.write_text("".join(json.dumps(event) + "\n" for event in events))


def test_load_lob_capture_maps_sides_deduplicates_and_bounds_trades(tmp_path):
    path = tmp_path / "events.jsonl"
    book = lambda ts, bid, ask: {
        "channel": "l2Book",
        "data": {
            "coin": "ENA",
            "time": ts,
            "levels": [
                [{"px": str(bid), "sz": "100", "n": 1}],
                [{"px": str(ask), "sz": "200", "n": 1}],
            ],
        },
    }
    trades = lambda rows: {"channel": "trades", "data": rows}
    _write_events(path, [
        trades([{"coin": "ENA", "side": "B", "px": "1.02", "sz": "3", "time": 900, "tid": 1}]),
        book(1000, 1.00, 1.02),
        trades([
            {"coin": "ENA", "side": "A", "px": "1.00", "sz": "4", "time": 1100, "tid": 2},
            {"coin": "ENA", "side": "B", "px": "1.02", "sz": "5", "time": 1200, "tid": 3},
        ]),
        trades([{"coin": "ENA", "side": "B", "px": "1.02", "sz": "5", "time": 1200, "tid": 3}]),
        book(2000, 1.01, 1.03),
        trades([{"coin": "ENA", "side": "A", "px": "1.01", "sz": "6", "time": 2100, "tid": 4}]),
    ])

    replay = load_lob_capture(path, "ENA")

    assert [snapshot.mid for snapshot in replay.snapshots] == pytest.approx([1.01, 1.02])
    assert [(trade.ts, trade.side, trade.size) for trade in replay.trades] == [
        (1.1, "sell", 4.0),
        (1.2, "buy", 5.0),
    ]
    assert replay.books[0].bids == ((1.0, 100.0),)
    assert replay.books[0].asks == ((1.02, 200.0),)


def _fixed_strategy(bid, ask, size):
    def strategy(config, inventory, equity, mid_history, ts, mid):
        return ExecIntent(
            venue="hyperliquid",
            coin=config.coin,
            target_inventory=inventory.position,
            quote=QuoteSpec(
                bid_price=bid,
                ask_price=ask,
                bid_size=size,
                ask_size=size,
            ),
        )
    return strategy


def _queue_backtest(trades):
    config = PerpPairConfig(
        coin="ENA",
        caps=Caps(max_position=1000.0, critical_position=2000.0),
    )
    bt = Backtest(config, start_equity=1000.0)
    bt.set_strategy(_fixed_strategy(1.00, 1.02, 10.0))
    for ts in (0.0, 0.5, 1.0, 1.5, 2.0):
        bt.add_book(BacktestBook(ts=ts, bids=((1.00, 100.0),), asks=((1.02, 20.0),)))
    bt.load_trades(trades)
    bt.load_funding([])
    asyncio.run(bt.run(duration_s=2.5, tick_s=0.5))
    return bt


def test_lob_queue_blocks_equal_price_trade_that_does_not_clear_ahead_size():
    bt = _queue_backtest([Backtrade(ts=1.1, side="buy", price=1.02, size=15.0)])

    assert bt.metrics()["n_fills"] == 0


def test_lob_queue_fills_residual_after_ahead_size_is_cleared():
    bt = _queue_backtest([Backtrade(ts=1.1, side="buy", price=1.02, size=25.0)])

    assert bt.metrics()["n_fills"] == 1
    assert bt._fills[0].side == "ask"
    assert bt._fills[0].size == pytest.approx(5.0)


def test_trade_through_quote_fills_order_despite_displayed_queue():
    bt = _queue_backtest([Backtrade(ts=1.1, side="buy", price=1.03, size=1.0)])

    assert bt.metrics()["n_fills"] == 1
    assert bt._fills[0].size == pytest.approx(10.0)
