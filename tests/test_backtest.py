import time

import pytest

from mm_core.contracts import ExecIntent, MarketSnapshot, QuoteSpec
from mm_core.inventory import Caps

from perp_bot.config import PerpPairConfig
from perp_bot.backtest import BaselineStrategy, Backtest, Backtrade, Strategy


def make_config():
    config = PerpPairConfig(coin="BTC", gamma=1.0, kappa=0.5)
    config.caps = Caps(max_position=10.0, critical_position=20.0)
    return config


def fixed_quote_strategy(bid, ask, size=1.0):
    """Strategy stub quoting fixed prices every tick."""
    def strategy(config, inventory, equity, mid_history, ts, mid):
        return ExecIntent(
            venue="hl", coin="BTC", target_inventory=inventory.position,
            quote=QuoteSpec(bid_price=bid, ask_price=ask, bid_size=size, ask_size=size),
        )
    return strategy


def run_bt(strategy, trades, mid=50000.0, n=30, duration=40.0):
    import asyncio
    bt = Backtest(make_config(), start_equity=10000.0)
    bt.set_strategy(strategy)
    t0 = time.time()
    for i in range(n):
        bt.add_snapshot(MarketSnapshot(venue="hl", coin="BTC", ts=t0 + i, mid=mid))
    bt.load_trades([Backtrade(ts=t0 + t.ts, side=t.side, price=t.price, size=t.size)
                    for t in trades])
    bt.load_funding([])
    asyncio.run(bt.run(duration_s=duration))
    return bt


@pytest.mark.asyncio
async def test_backtest_basic():
    import asyncio
    config = make_config()
    bt = Backtest(config, start_equity=10000.0)
    bt.set_strategy(Strategy(config))

    t0 = time.time()
    for i in range(50):
        bt.add_snapshot(
            MarketSnapshot(venue="hl", coin="BTC", ts=t0 + i, mid=50000.0 + i * 10)
        )
    bt.load_trades([
        Backtrade(ts=t0 + 5, side="buy", price=50000.0, size=1.0),
        Backtrade(ts=t0 + 10, side="sell", price=50100.0, size=1.0),
    ])
    bt.load_funding([])

    history = await bt.run(duration_s=60.0)
    assert len(history) > 0
    assert history[0]["equity"] > 0


def test_ask_fill_earns_the_spread():
    """Selling above mid must ADD pnl (this was zeroed by the old rule)."""
    # Ask resting at 50010; an aggressive buy prints through it
    trades = [Backtrade(ts=5, side="buy", price=50020.0, size=1.0)]
    bt = run_bt(fixed_quote_strategy(bid=49000.0, ask=50010.0), trades)

    m = bt.metrics()
    assert m["n_fills"] == 1
    assert bt._fills[0].side == "ask"
    # short 1 @ 50010 vs mid 50000 → +10 marked to mid
    assert m["net_pnl"] == pytest.approx(10.0)


def test_bid_fill_earns_the_spread():
    trades = [Backtrade(ts=5, side="sell", price=49980.0, size=1.0)]
    bt = run_bt(fixed_quote_strategy(bid=49990.0, ask=51000.0), trades)

    m = bt.metrics()
    assert m["n_fills"] == 1
    assert bt._fills[0].side == "bid"
    assert m["net_pnl"] == pytest.approx(10.0)


def test_quotes_are_replaced_not_stacked():
    """Resting book holds at most one bid + one ask (cancel/replace)."""
    bt = run_bt(fixed_quote_strategy(bid=49000.0, ask=51000.0), trades=[], n=30)
    assert len(bt._resting_orders) <= 2


def test_one_trade_can_fill_multiple_orders():
    """A large print sweeping both quotes fills both (old code dropped one)."""
    def two_asks(config, inventory, equity, mid_history, ts, mid):
        return ExecIntent(
            venue="hl", coin="BTC", target_inventory=0.0,
            quote=QuoteSpec(bid_price=50005.0, ask_price=50010.0,
                            bid_size=1.0, ask_size=1.0),
        )
    # A sell at 50001 prints through the bid (50005); a buy at 50020 through the ask
    trades = [
        Backtrade(ts=5, side="sell", price=50001.0, size=1.0),
        Backtrade(ts=6, side="buy", price=50020.0, size=2.0),
    ]
    bt = run_bt(two_asks, trades)
    sides = [f.side for f in bt._fills]
    assert "bid" in sides and "ask" in sides


def test_inventory_carries_price_risk():
    """Held inventory is marked to mid: a long loses when mid drops."""
    import asyncio
    bt = Backtest(make_config(), start_equity=10000.0)
    bt.set_strategy(fixed_quote_strategy(bid=49995.0, ask=51000.0))
    t0 = time.time()
    # mid 50000 for 10 ticks, then drops to 49000
    for i in range(10):
        bt.add_snapshot(MarketSnapshot(venue="hl", coin="BTC", ts=t0 + i, mid=50000.0))
    for i in range(10, 30):
        bt.add_snapshot(MarketSnapshot(venue="hl", coin="BTC", ts=t0 + i, mid=49000.0))
    # get long 1 early via a sell printing through the bid
    bt.load_trades([Backtrade(ts=t0 + 5, side="sell", price=49990.0, size=1.0)])
    bt.load_funding([])
    asyncio.run(bt.run(duration_s=35.0))

    m = bt.metrics()
    assert m["net_pnl"] < -500, "long through a 1000-point drop must lose"
    assert m["max_drawdown"] > 0


def test_funding_costs_longs_when_positive():
    import asyncio
    bt = Backtest(make_config(), start_equity=10000.0)
    bt.set_strategy(fixed_quote_strategy(bid=49995.0, ask=51000.0))
    t0 = time.time()
    for i in range(30):
        bt.add_snapshot(MarketSnapshot(venue="hl", coin="BTC", ts=t0 + i, mid=50000.0))
    bt.load_trades([Backtrade(ts=t0 + 5, side="sell", price=49990.0, size=1.0)])
    bt.load_funding([{"ts": t0 + 10, "rate": 1e-4}])
    asyncio.run(bt.run(duration_s=35.0))

    # long 1 @ 49995 vs mid 50000 → +5 edge, minus funding 50000*1e-4 = 5
    assert bt.metrics()["net_pnl"] == pytest.approx(0.0, abs=1e-6)


def test_metrics_shape():
    bt = run_bt(fixed_quote_strategy(bid=49000.0, ask=51000.0), trades=[])
    m = bt.metrics()
    assert {"final_equity", "net_pnl", "net_edge_bps", "max_drawdown", "n_fills",
            "spread_capture", "markout_pnl", "funding_pnl"} <= m.keys()


def test_pnl_explain_matches_metrics_breakdown():
    """metrics() and pnl_explain() must agree — one ledger, one truth."""
    trades = [Backtrade(ts=5, side="buy", price=50020.0, size=1.0)]
    bt = run_bt(fixed_quote_strategy(bid=49000.0, ask=50010.0), trades)

    m = bt.metrics()
    b = bt.pnl_explain()
    assert m["spread_capture"] == pytest.approx(b.spread_capture)
    assert m["markout_pnl"] == pytest.approx(b.markout_pnl)
    assert m["funding_pnl"] == pytest.approx(b.funding_pnl)
    assert b.n_fills == m["n_fills"]


def test_pnl_explain_exposes_fill_history():
    trades = [Backtrade(ts=5, side="buy", price=50020.0, size=1.0)]
    bt = run_bt(fixed_quote_strategy(bid=49000.0, ask=50010.0), trades)

    b = bt.pnl_explain()
    assert len(b.fills) == 1
    assert b.fills[0].side == "sell"  # our resting ask was hit
    assert b.fills[0].price == pytest.approx(50010.0)
    assert b.fills[0].mid_at_fill is not None


def test_strategy_intents_use_configured_exchange_as_venue():
    """Both the AS Strategy and BaselineStrategy must tag intents with the
    real routing exchange id, not a display label — this is what OPMS uses
    to resolve the adapter/account."""
    config = make_config()
    assert config.exchange == "hyperliquid"

    from mm_core.inventory import PerpInventory
    inv = PerpInventory(position=0.0, _caps=config.caps)
    mid_history = [(0.0, 50000.0), (1.0, 50010.0), (2.0, 50020.0)]

    for strat_cls in (Strategy(config), BaselineStrategy()):
        intent = strat_cls(config, inv, 10000.0, mid_history, 2.0, 50020.0)
        assert intent.venue == "hyperliquid"


def test_baseline_strategy_quotes_symmetric_fixed_spread():
    bt = run_bt(BaselineStrategy(spread_bps=100.0), trades=[], mid=50000.0, n=5)
    assert len(bt._resting_orders) == 2
    bid = next(o for o in bt._resting_orders if o["side"] == "bid")
    ask = next(o for o in bt._resting_orders if o["side"] == "ask")
    # 100bps total spread on 50000 mid -> +/- 250 either side
    assert bid["price"] == pytest.approx(49750.0)
    assert ask["price"] == pytest.approx(50250.0)


def test_gate_report_fails_on_thin_edge_and_reports_thresholds():
    """A quote so wide it never fills earns zero edge -> net_edge_bps gate fails."""
    bt = run_bt(fixed_quote_strategy(bid=1.0, ask=1000000.0), trades=[], n=5)
    report = bt.gate_report()
    assert report["passed"] is False
    assert report["checks"]["net_edge_bps"]["passed"] is False
    assert report["checks"]["net_edge_bps"]["threshold"] == 2.0
    assert report["checks"]["liquidations"]["passed"] is True


def test_gate_report_passes_when_all_metrics_within_bounds():
    trades = [Backtrade(ts=5, side="buy", price=50020.0, size=1.0)]
    bt = run_bt(fixed_quote_strategy(bid=49000.0, ask=50010.0), trades)
    report = bt.gate_report()
    assert report["checks"]["max_drawdown"]["passed"] is True
    assert report["checks"]["liquidations"] == {"passed": True, "value": 0, "threshold": 0}
