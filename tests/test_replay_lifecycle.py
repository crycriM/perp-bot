"""Full-lifecycle replay tests: the cancel/replace/activate/flatten behavior of
Backtest.run, not _fill_rule in isolation.

Disclosed model constraints pinned here: queue position is sampled at decision
time (not at activation), and the post-only buffer is computed against the
decision-time book; a quote that a trade prints through between cancel and
cancel-effect is a real fill, after cancel-effect it is not.
"""

import asyncio

import pytest

from mm_core.contracts import ExecIntent, MarketSnapshot, QuoteSpec

from perp_bot.backtest import Backtest, BacktestBook, Backtrade
from perp_bot.config import PerpPairConfig


def _quote_intent(inventory, *, bid=None, ask=None, bid_size=0.0, ask_size=0.0,
                  target=0.0):
    return ExecIntent(
        venue="hyperliquid", coin="SOL", target_inventory=target,
        current_inventory=inventory.position,
        quote=QuoteSpec(bid_price=bid, ask_price=ask,
                        bid_size=bid_size, ask_size=ask_size),
    )


def _bt(cfg, strategy, *, books=False, mids_to=None, flatten=False):
    bt = Backtest(cfg, start_equity=1000.0, decision_interval_s=1.0,
                  quote_refresh_s=1000.0, flatten_at_end=flatten)
    bt.set_strategy(strategy)
    last = mids_to if mids_to is not None else 11
    for i in range(last):
        if books:
            bt.add_book(BacktestBook(ts=float(i), bids=((99.5, 5.0),),
                                     asks=((100.5, 5.0),)))
        else:
            bt.add_snapshot(MarketSnapshot(venue="hyperliquid", coin="SOL",
                                           ts=float(i), mid=100.0))
    return bt


def test_cancel_latency_fills_before_ack_never_after_and_replace_waits_activation():
    cfg = PerpPairConfig(coin="SOL", cancel_latency_s=1.0, place_latency_s=1.0)

    def strategy(config, inventory, equity, mid_history, ts, mid):
        if ts < 5:
            return _quote_intent(inventory, bid=99.5, ask=100.5, bid_size=1.0, ask_size=1.0)
        return _quote_intent(inventory, bid=99.2, ask=100.8, bid_size=1.0, ask_size=1.0)

    bt = _bt(cfg, strategy)
    bt.load_trades([
        Backtrade(5.5, "sell", 99.0, 1.0),    # before cancel_at=6: genuine exposure
        Backtrade(6.5, "sell", 98.0, 1.0),    # after ack; new bid not active until 7
        Backtrade(6.9, "buy", 100.9, 1.0),    # through new ask but pre-activation
        Backtrade(7.1, "buy", 101.0, 1.0),    # active replace takes it
    ])
    asyncio.run(bt.run(duration_s=11.0, tick_s=1.0))
    assert [(f.side, f.price, f.size) for f in bt._fills] == [
        ("bid", 99.5, 1.0), ("ask", 100.8, 1.0)]
    assert len(bt._resting_orders) <= 2


def test_reduce_only_fill_clips_at_flat_and_cannot_flip_through_run():
    cfg = PerpPairConfig(coin="SOL", cancel_latency_s=1.0, place_latency_s=1.0)

    def strategy(config, inventory, equity, mid_history, ts, mid):
        if ts < 5:
            return _quote_intent(inventory, bid=99.5, bid_size=2.0)
        return _quote_intent(inventory, ask=100.6, ask_size=5.0)

    bt = _bt(cfg, strategy)
    bt.load_trades([
        Backtrade(3.5, "sell", 99.4, 2.0),    # maker bid fill -> +2
        Backtrade(7.5, "buy", 101.0, 10.0),   # after replace activation (6.5 is dead-zone)
    ])
    history = asyncio.run(bt.run(duration_s=11.0, tick_s=1.0))
    assert [(f.side, f.size) for f in bt._fills] == [("bid", 2.0), ("ask", 2.0)]
    assert bt._pnl.position == pytest.approx(0.0)
    assert min(h["position"] for h in history) == pytest.approx(0.0)


def test_terminal_flatten_pays_touch_slippage_and_taker_fee():
    cfg = PerpPairConfig(coin="SOL", place_latency_s=1.0, close_slippage_bps=10.0,
                         taker_fee_bps=4.5)

    def strategy(config, inventory, equity, mid_history, ts, mid):
        return _quote_intent(inventory, bid=99.5, bid_size=1.0)

    bt = _bt(cfg, strategy, books=True, mids_to=10, flatten=True)
    bt.load_trades([Backtrade(4.5, "sell", 99.4, 1.0)])
    history = asyncio.run(bt.run(duration_s=10.0, tick_s=1.0))
    fills = bt._pnl.fills
    assert fills[0].fee == pytest.approx(99.5 * 1.5 / 1e4)      # maker fee on entry
    assert fills[-1].label == "terminal"
    assert fills[-1].price == pytest.approx(99.5 * (1 - 10.0 / 1e4))
    assert fills[-1].fee == pytest.approx(99.5 * (1 - 10.0 / 1e4) * 4.5 / 1e4)
    assert bt._pnl.position == pytest.approx(0.0)
    assert history[-1]["position"] == pytest.approx(0.0)


def test_stale_market_data_suppresses_new_quotes_and_further_fills():
    cfg = PerpPairConfig(coin="SOL", max_market_data_age_s=2.0)

    def strategy(config, inventory, equity, mid_history, ts, mid):
        return _quote_intent(inventory, bid=99.5, ask=100.5, bid_size=1.0, ask_size=1.0)

    bt = _bt(cfg, strategy, mids_to=4)   # market data stops at ts=3
    bt.load_trades([Backtrade(7.0, "sell", 50.0, 1.0)])  # would print through a live bid
    asyncio.run(bt.run(duration_s=10.0, tick_s=1.0))
    assert bt._fills == []
    assert bt._resting_orders == []


def test_min_quote_notional_filter_blocks_small_legs_then_admits_after_resize():
    cfg = PerpPairConfig(coin="SOL", place_latency_s=1.0, min_quote_notional=200.0)

    def strategy(config, inventory, equity, mid_history, ts, mid):
        size = 1.0 if ts < 5 else 3.0
        return _quote_intent(inventory, bid=99.5, ask=100.5, bid_size=size, ask_size=size)

    bt = _bt(cfg, strategy)
    bt.load_trades([
        Backtrade(3.5, "sell", 99.4, 1.0),   # only sub-minimal quotes existed: no fill
        Backtrade(6.5, "sell", 99.3, 5.0),   # admitted 3-unit bid takes the full order
    ])
    asyncio.run(bt.run(duration_s=11.0, tick_s=1.0))
    assert [(f.side, f.price, f.size) for f in bt._fills] == [("bid", 99.5, 3.0)]
