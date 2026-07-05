import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from mm_core.as_core import gueant_half_spread, gueant_reservation_price
from mm_core.contracts import ExecIntent, MarketSnapshot, QuoteSpec
from mm_core.inventory import Caps, PerpInventory
from mm_core.vol import VOLATILITY_MODELS

logger = logging.getLogger(__name__)

@dataclass
class BacktestFill:
    side: str
    price: float
    size: float

@dataclass
class Backtrade:
    ts: float
    side: str
    price: float
    size: float

class Backtest:
    """Event replay backtester for HL L2 + trades + funding."""

    def __init__(self, config, start_equity: float = 1.0):
        self.config = config
        self.start_equity = start_equity
        self._trades: list[Backtrade] = []
        self._funding_events: list = []
        self._snapshots: list[MarketSnapshot] = []
        self._fills: list[BacktestFill] = []
        self._history: list = []
        self._strategies: dict[str, Callable] = {}
        self._equity = start_equity
        self._inventory = PerpInventory(position=0.0, _caps=config.caps)
        self._mid_history: list[tuple] = []
        self._resting_orders: list[dict] = []
        self._strategy = None
        self._baseline_spread: float = 50.0

    def load_trades(self, trades: list[Backtrade]):
        self._trades = trades

    def load_funding(self, events: list):
        self._funding_events = events

    def set_strategy(self, strategy: Callable):
        self._strategy = strategy

    def set_baseline(self, baseline: Callable):
        self._strategies["baseline"] = baseline

    def add_snapshot(self, snapshot: MarketSnapshot):
        self._snapshots.append(snapshot)
        self._mid_history.append((snapshot.ts, snapshot.mid))

    def _fill_rule(self, ts: float, trade: Backtrade) -> BacktestFill | None:
        filled = []
        remaining = []
        for order in self._resting_orders:
            if trade.side == "buy" and order["side"] == "ask" and trade.price >= order["price"]:
                fill_size = min(trade.size, order["size"])
                filled.append(BacktestFill(side="ask", price=order["price"], size=fill_size))
                order["size"] -= fill_size
                trade.size -= fill_size
                if order["size"] <= 0:
                    continue
                remaining.append(order)
            elif trade.side == "sell" and order["side"] == "bid" and trade.price <= order["price"]:
                fill_size = min(trade.size, order["size"])
                filled.append(BacktestFill(side="bid", price=order["price"], size=fill_size))
                order["size"] -= fill_size
                trade.size -= fill_size
                if order["size"] <= 0:
                    continue
                remaining.append(order)
            else:
                remaining.append(order)
        self._resting_orders = remaining
        return filled[0] if filled else None

    async def run(self, duration_s: float = 3600.0):
        coin = self.config.coin
        gamma = self.config.gamma
        kappa = self.config.kappa

        trades_iter = iter(self._trades)
        funding_iter = iter(self._funding_events)
        next_trade = next(trades_iter, None)
        next_funding = next(funding_iter, None)

        t0 = self._snapshots[0].ts if self._snapshots else time.time()
        t_end = t0 + duration_s
        last_quote_ts = 0.0
        tick_s = 1.0

        t = t0
        while t < t_end:
            # Process snapshots up to t
            while self._snapshots and self._snapshots[0].ts <= t:
                snap = self._snapshots.pop(0)
                self._mid_history.append((snap.ts, snap.mid))

            mid = self._mid_history[-1][1] if self._mid_history else 0.0

            # Process trades
            while next_trade and next_trade.ts <= t:
                fill = self._fill_rule(t, next_trade)
                if fill:
                    self._fills.append(fill)
                    delta = -fill.size if fill.side == "ask" else fill.size
                    self._inventory.position += delta
                    pnl = (0 if fill.side == "ask" else 1) * (mid - fill.price) * fill.size
                    self._equity += pnl

                next_trade = next(trades_iter, None)

            # Process funding
            while next_funding and next_funding.get("ts", 0) <= t:
                fr = next_funding.get("rate", 0.0)
                cost = self._inventory.position * mid * fr
                self._equity += cost
                next_funding = next(funding_iter, None)

            # Strategy decision every tick
            if t - last_quote_ts >= tick_s:
                last_quote_ts = t
                if self._strategy and len(self._mid_history) > 2:
                    intent = self._strategy(self.config, self._inventory, self._equity,
                                           self._mid_history, t, mid)
                    if intent and intent.quote:
                        self._resting_orders.append({
                            "side": "bid", "price": intent.quote.bid_price,
                            "size": intent.quote.bid_size,
                        })
                        self._resting_orders.append({
                            "side": "ask", "price": intent.quote.ask_price,
                            "size": intent.quote.ask_size,
                        })

            self._history.append({
                "ts": t, "mid": mid, "equity": self._equity,
                "position": self._inventory.position,
            })
            t += tick_s

        return self._history

@dataclass
class Strategy:
    config: Any = None

    def __call__(self, config, inventory, equity, mid_history, ts, mid):
        if not mid_history or len(mid_history) < 2:
            return None
        sigma = VOLATILITY_MODELS["close_to_close"](mid_history)
        r = gueant_reservation_price(mid, inventory.position, config.gamma, sigma, config.kappa)
        hs = gueant_half_spread(config.gamma, sigma, config.kappa)
        return ExecIntent(
            venue="hl", coin=config.coin,
            target_inventory=inventory.position,
            quote=QuoteSpec(
                bid_price=r - hs, ask_price=r + hs,
                bid_size=config.caps.max_position * 0.1,
                ask_size=config.caps.max_position * 0.1,
            ),
        )
