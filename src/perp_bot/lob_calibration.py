"""Strict train/OOS calibration helpers for captured L2 order books."""

from __future__ import annotations

from mm_core.inventory import Caps

from perp_bot.backtest import (
    GATE_MAX_DRAWDOWN,
    GATE_MAX_MARKOUT_RATIO,
    GATE_MIN_NET_EDGE_BPS,
    Backtest,
    Backtrade,
    Strategy,
)
from perp_bot.config import PerpPairConfig
from perp_bot.hl_lob import LobReplayData


def _slice_passes(metrics: dict, min_fills: int) -> bool:
    return (
        metrics["net_edge_bps"] > GATE_MIN_NET_EDGE_BPS
        and metrics["markout_ratio"] < GATE_MAX_MARKOUT_RATIO
        and metrics["max_drawdown"] < GATE_MAX_DRAWDOWN
        and metrics["liquidations"] == 0
        and metrics["n_fills"] >= min_fills
    )


def candidate_passes(train: dict, oos: dict, min_fills: int) -> bool:
    return _slice_passes(train, min_fills) and _slice_passes(oos, min_fills)


def rank_candidate(candidate: dict) -> tuple[float, float, float, float]:
    """Prefer robust edge, then lower toxicity/drawdown, then total edge."""
    train, oos = candidate["train"], candidate["oos"]
    return (
        min(train["net_edge_bps"], oos["net_edge_bps"]),
        -max(train["markout_ratio"], oos["markout_ratio"]),
        -max(train["max_drawdown"], oos["max_drawdown"]),
        train["net_edge_bps"] + oos["net_edge_bps"],
    )


async def replay_candidate(
    data: LobReplayData,
    *,
    coin: str,
    gamma: float,
    kappa: float,
    quote_size: float,
    price_tick: float,
    start_equity: float,
    tick_s: float,
    decision_interval_s: float,
    quote_refresh_s: float,
    max_position_multiple: float,
) -> dict:
    if not data.books:
        raise ValueError(f"no L2 books available for {coin}")
    max_position = quote_size * max_position_multiple
    config = PerpPairConfig(
        coin=coin,
        gamma=gamma,
        kappa=kappa,
        quote_size=quote_size,
        price_tick=price_tick,
        caps=Caps(max_position=max_position, critical_position=max_position * 2.0),
    )
    backtest = Backtest(
        config,
        start_equity=start_equity,
        decision_interval_s=decision_interval_s,
        quote_refresh_s=quote_refresh_s,
    )
    backtest.set_strategy(Strategy(config))
    for book in data.books:
        backtest.add_book(book)
    # _fill_rule consumes trade size, so every candidate receives fresh rows.
    backtest.load_trades([
        Backtrade(trade.ts, trade.side, trade.price, trade.size)
        for trade in data.trades
    ])
    backtest.load_funding([])
    duration_s = data.books[-1].ts - data.books[0].ts + tick_s
    await backtest.run(duration_s=duration_s, tick_s=tick_s)
    return backtest.metrics()
