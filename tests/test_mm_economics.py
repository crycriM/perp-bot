import asyncio

import pytest

from mm_core.contracts import MarketSnapshot
from mm_core.risk_policy import Decision
from perp_bot.backtest import Backtest, Backtrade
from perp_bot.config import PerpPairConfig
from perp_bot.keeper import bounded_quote_sizes, build_intent


def test_glft_requires_estimates_not_old_scale_defaults():
    with pytest.raises(ValueError, match="GLFT"):
        PerpPairConfig(coin="SOL", pricing_model="glft")


def test_glft_quotes_in_lots_with_economic_floor_and_constant_lot_on_widen():
    cfg = PerpPairConfig(coin="SOL", pricing_model="glft", gamma=.02,
                        kappa=.4, arrival_rate_per_s=2, sigma_bps_sqrt_s=3,
                        quote_size=1, maker_fee_bps=1.5, min_edge_bps=2)
    quote = build_intent(cfg, Decision.QUOTE, "normal", 100, 0, [], 10).quote
    wide = build_intent(cfg, Decision.WIDEN, "normal", 100, 0, [], 10).quote
    assert quote.bid_price <= 100 * (1 - 3.5 / 1e4)
    assert quote.ask_price >= 100 * (1 + 3.5 / 1e4)
    assert wide.bid_size == wide.ask_size == 1


def test_small_target_gap_does_not_remove_executable_reducing_side():
    bid, ask = bounded_quote_sizes(64, 50, 250, 50, min_size=44)
    assert ask == 44  # may cross structural target, never flat or hard cap
    assert bid == 50
    assert bounded_quote_sizes(4, 0, 250, 50, min_size=44)[1] == 0  # explicit dust


def test_single_account_stop_flattens_while_structural_leg_holds():
    for target, expected in [(0, 0), (5, 3)]:
        cfg = PerpPairConfig(coin="SOL", target_inventory=target)
        assert build_intent(cfg, Decision.STOP_QUOTING, "normal", 100, 3, [], 10).target_inventory == expected


def test_replay_books_fill_at_event_time_without_future_mid_and_with_fees():
    cfg = PerpPairConfig(coin="SOL", maker_fee_bps=2, taker_fee_bps=5)
    bt = Backtest(cfg, start_equity=1000)
    bt.add_snapshot(MarketSnapshot(venue="hyperliquid", coin="SOL", ts=0, mid=100))
    bt.add_snapshot(MarketSnapshot(venue="hyperliquid", coin="SOL", ts=.9, mid=200))
    bt._resting_orders = [dict(side="bid", price=99, size=1)]
    bt.load_trades([Backtrade(.1, "sell", 98, 1)])
    asyncio.run(bt.run(duration_s=2, tick_s=1))
    result = bt.pnl_explain()
    assert result.spread_capture == pytest.approx(1)
    assert result.fee_pnl == pytest.approx(-.0198)


def test_cancel_latency_keeps_order_exposed_until_ack_and_no_input_mutation():
    bt = Backtest(PerpPairConfig(coin="SOL"))
    bt._resting_orders = [dict(side="bid", price=99, size=1, active_at=1, cancel_at=2)]
    assert not bt._fill_rule(.5, Backtrade(.5, "sell", 98, 1))
    assert bt._fill_rule(1.5, Backtrade(1.5, "sell", 98, 1))
    bt._resting_orders = [dict(side="bid", price=99, size=1, active_at=1, cancel_at=2)]
    assert not bt._fill_rule(2.1, Backtrade(2.1, "sell", 98, 1))
