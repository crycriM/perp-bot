import asyncio
import dataclasses
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from mm_core.contracts import ExecIntent, MarketSnapshot, QuoteSpec
from mm_core.inventory import Caps, PerpInventory
from mm_core.markout import MarkoutTracker
from mm_core.pnl import Fill, PnLLedger
from mm_core.regime import evaluate_regime
from mm_core.risk_policy import Decision, RiskPolicy
from mm_core.quote_refresh import quote_refresh_reason

from perp_bot.keeper import MID_HISTORY_LEN, build_intent
from perp_bot.margin_health import initial_margin_available

logger = logging.getLogger(__name__)

# rollout gates (restated in the README "The strategy, in brief")
GATE_MIN_NET_EDGE_BPS = 2.0
GATE_MAX_MARKOUT_RATIO = 0.5
GATE_MAX_DRAWDOWN = 0.05

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


@dataclass(frozen=True)
class BacktestBook:
    """One full L2 snapshot used to model queue ahead of maker quotes."""

    ts: float
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    exchange_ts: float | None = None  # ts is local availability when captured

    @property
    def mid(self) -> float:
        return (self.bids[0][0] + self.asks[0][0]) / 2.0


@dataclass
class BacktestDecisionRecord:
    source: str
    ts: float
    mid: float
    inventory: float
    equity: float
    intent: ExecIntent | None
    decision: str | None = None
    urgency: str | None = None
    regime: str | None = None
    regime_half_life: float | None = None
    regime_hurst: float | None = None
    regime_trending: bool | None = None
    regime_state: str | None = None
    regime_transition: str | None = None
    regime_raw_open: bool | None = None
    regime_provisional: bool = False
    regime_sample_count: int | None = None
    regime_sample_interval_s: float | None = None
    regime_history_span_s: float | None = None


def infer_tick_s_from_snapshots(snapshots: list[MarketSnapshot], default: float = 1.0) -> float:
    """Infer a replay step from sparse market snapshots.

    Real fetched HL datasets are candle-based, not one-snapshot-per-second.
    Replaying them with a fixed 1s loop turns a 30-day dataset into millions of
    empty iterations, so the CSV runner uses the smallest positive snapshot gap
    as the backtest step.
    """
    positive_gaps = [
        right.ts - left.ts
        for left, right in zip(snapshots, snapshots[1:])
        if right.ts > left.ts
    ]
    return min(positive_gaps) if positive_gaps else default

class Backtest:
    """Event replay backtester for HL L2 + trades + funding.

    Uses the same mm_core.pnl.PnLLedger the live keeper uses, so a
    strategy's backtest numbers and its live numbers are never computed by
    two different pieces of math that can silently drift apart.
    """

    def __init__(
        self,
        config,
        start_equity: float = 1.0,
        decision_log_path: str | None = None,
        decision_interval_s: float | None = None,
        quote_refresh_s: float | None = None,
        flatten_at_end: bool = False,
    ):
        if decision_interval_s is not None and decision_interval_s <= 0:
            raise ValueError("decision_interval_s must be positive")
        if quote_refresh_s is not None and quote_refresh_s <= 0:
            raise ValueError("quote_refresh_s must be positive")
        self.config = config
        self.start_equity = start_equity
        self._decision_interval_s = decision_interval_s
        self._quote_refresh_s = quote_refresh_s
        self._flatten_at_end = flatten_at_end
        self._trades: list[Backtrade] = []
        self._funding_events: list = []
        self._snapshots: list[MarketSnapshot] = []
        self._books: list[BacktestBook] = []
        self._current_book: BacktestBook | None = None
        self._fills: list[BacktestFill] = []
        self._history: list = []
        self._strategies: dict[str, Callable] = {}
        self._equity = start_equity
        self._pnl = PnLLedger(venue=getattr(config, "exchange", "hyperliquid"), symbol=config.coin)
        self._inventory = PerpInventory(position=0.0, _caps=config.caps)
        self._mid_history: list[tuple] = []
        self._resting_orders: list[dict] = []
        self._strategy = None
        self._baseline_spread: float = 50.0
        self._decision_log = open(decision_log_path, "a") if decision_log_path else None

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

    def add_book(self, book: BacktestBook):
        self._books.append(book)
        self.add_snapshot(MarketSnapshot(
            venue="hyperliquid", coin=self.config.coin, ts=book.ts, mid=book.mid,
        ))

    def _visible_queue(self, side: str, price: float) -> float | None:
        if self._current_book is None:
            return None
        levels = self._current_book.bids if side == "bid" else self._current_book.asks
        tolerance = max(abs(price) * 1e-12, 1e-12)
        return sum(size for level_price, size in levels
                   if abs(level_price - price) <= tolerance)

    def _fill_rule(self, ts: float, trade: Backtrade) -> list[BacktestFill]:
        """A resting order fills when a trade prints through its price."""
        trade = dataclasses.replace(trade)  # never consume the input capture
        filled: list[BacktestFill] = []
        remaining = []
        for order in self._resting_orders:
            if ts >= order.get("cancel_at", float("inf")):
                continue
            if ts < order.get("active_at", -float("inf")):
                remaining.append(order)
                continue
            if trade.size <= 0:
                remaining.append(order)
                continue
            is_ask_hit = trade.side == "buy" and order["side"] == "ask"
            is_bid_hit = trade.side == "sell" and order["side"] == "bid"
            tolerance = max(abs(order["price"]) * 1e-12, 1e-12)
            through = (
                (is_ask_hit and trade.price > order["price"] + tolerance)
                or (is_bid_hit and trade.price < order["price"] - tolerance)
            )
            at_price = (
                (is_ask_hit or is_bid_hit)
                and abs(trade.price - order["price"]) <= tolerance
            )
            if through:
                # A later print beyond our price proves our level was swept,
                # even if the exchange reports that worse-price match in a
                # separate trade record with a smaller size.
                fill_size = order["size"]
            elif at_price:
                queue_ahead = order.get("queue_ahead")
                if queue_ahead is not None and queue_ahead > 0:
                    consumed = min(queue_ahead, trade.size)
                    order["queue_ahead"] -= consumed
                    trade.size -= consumed
                if trade.size <= 0:
                    remaining.append(order)
                    continue
                fill_size = min(trade.size, order["size"])
            else:
                remaining.append(order)
                continue

            if order.get("reduce_only"):
                closeable = max(0, self._pnl.position if is_ask_hit else -self._pnl.position)
                fill_size = min(fill_size, closeable)
                if fill_size <= 0:
                    continue

            if is_ask_hit:
                filled.append(BacktestFill(side="ask", price=order["price"], size=fill_size))
                order["size"] -= fill_size
                trade.size -= fill_size
                if order["size"] <= 0:
                    continue
                remaining.append(order)
            elif is_bid_hit:
                filled.append(BacktestFill(side="bid", price=order["price"], size=fill_size))
                order["size"] -= fill_size
                trade.size -= fill_size
                if order["size"] <= 0:
                    continue
                remaining.append(order)
            else:
                remaining.append(order)
        self._resting_orders = remaining
        return filled

    async def run(self, duration_s: float = 3600.0, tick_s: float = 1.0):
        coin = self.config.coin
        gamma = self.config.gamma
        kappa = self.config.kappa

        # One chronological stream: a later snapshot in the same control
        # interval must not become a fill's reference mid (look-ahead).
        events = sorted(
            [(b.ts, 0, b) for b in self._books]
            + [(s.ts, 1, s) for s in self._snapshots]
            + [(r.ts, 2, r) for r in self._trades]
            + [(r["ts"], 3, r) for r in self._funding_events],
            key=lambda item: (item[0], item[1]),
        )
        event_index = 0

        t0 = self._snapshots[0].ts if self._snapshots else time.time()
        t_end = t0 + duration_s
        if tick_s <= 0:
            raise ValueError("tick_s must be > 0")
        decision_interval_s = self._decision_interval_s or tick_s
        quote_refresh_s = self._quote_refresh_s or decision_interval_s
        last_decision_ts = t0 - decision_interval_s
        last_quote_ts = -float("inf")

        t = t0
        while t < t_end:
            # Process snapshots up to t
            markout = getattr(self._strategy, "markout", None)
            mid = self._mid_history[-1][1] if self._mid_history else 0.0
            while event_index < len(events) and events[event_index][0] <= t:
                event_ts, kind, event = events[event_index]
                event_index += 1
                if kind == 0:
                    self._current_book = event
                elif kind == 1:
                    mid = event.mid
                    self._mid_history.append((event.ts, mid))
                    if markout is not None:
                        markout.on_mid(event.ts, mid)
                elif kind == 2:
                    for fill in self._fill_rule(event_ts, event):
                        self._fills.append(fill)
                        side = "sell" if fill.side == "ask" else "buy"
                        fee = fill.price * fill.size * self.config.maker_fee_bps / 1e4
                        self._pnl.on_fill(Fill(ts=event_ts, side=side, price=fill.price,
                                              size=fill.size, fee=fee, mid_at_fill=mid, label="maker"))
                        if markout is not None:
                            markout.on_fill(event_ts, side, fill.price, fill.size)
                else:
                    self._pnl.on_funding(event_ts, event.get("rate", 0.0), mid)
            self._inventory.position = self._pnl.position
            self._resting_orders = [o for o in self._resting_orders if t < o.get("cancel_at", float("inf"))]

            self._pnl.mark(t, mid)
            self._equity = self.start_equity + self._pnl.explain(t, mid, include_events=False).total_pnl

            # Strategy decision every tick: cancel/replace, never stack quotes
            if t - last_decision_ts >= decision_interval_s:
                last_decision_ts = t
                if self._strategy and len(self._mid_history) > 2:
                    intent = self._strategy(self.config, self._inventory, self._equity,
                                           self._mid_history, t, mid)
                    regime = getattr(self._strategy, "last_regime", None)
                    gate = getattr(getattr(self._strategy, "risk", None), "last_regime_gate", None)
                    decision = getattr(self._strategy, "last_decision", None)
                    self._log_decision(BacktestDecisionRecord(
                        source="backtest",
                        ts=t,
                        mid=mid,
                        inventory=self._inventory.position,
                        equity=self._equity,
                        intent=intent,
                        decision=decision.value if decision else None,
                        urgency=getattr(self._strategy, "last_urgency", None),
                        regime=(
                            f"hl={regime.half_life:.0f},h={regime.hurst:.2f}"
                            if regime else None
                        ),
                        regime_half_life=regime.half_life if regime else None,
                        regime_hurst=regime.hurst if regime else None,
                        regime_trending=regime.trending if regime else None,
                        regime_state=gate.state.value if gate else None,
                        regime_transition=gate.transition if gate else None,
                        regime_raw_open=gate.raw_open if gate else None,
                        regime_provisional=gate.provisional if gate else False,
                        regime_sample_count=regime.sample_count if regime else None,
                        regime_sample_interval_s=regime.sample_interval_s if regime else None,
                        regime_history_span_s=regime.history_span_s if regime else None,
                    ))
                    if intent and intent.quote:
                        desired = [
                            (side, getattr(intent.quote, f"{side}_price"),
                             getattr(intent.quote, f"{side}_size"))
                            for side in ("bid", "ask")
                            if getattr(intent.quote, f"{side}_price") is not None
                            and getattr(intent.quote, f"{side}_size") > 0
                        ]
                        if self._current_book:
                            buffer = self.config.quote_post_only_buffer_bps / 1e4
                            desired = [(side, min(price, self._current_book.bids[0][0] * (1 - buffer))
                                        if side == "bid" else max(price, self._current_book.asks[0][0] * (1 + buffer)), size)
                                       for side, price, size in desired]
                        desired = [(side, price, size) for side, price, size in desired
                                   if price * size >= self.config.min_quote_notional]
                        if (self.config.max_market_data_age_s is not None
                                and t - self._mid_history[-1][0] > self.config.max_market_data_age_s):
                            desired = []
                        desired_by_side = {side: (price, size) for side, price, size in desired}
                        needs_refresh = len(self._resting_orders) != len(desired)
                        for order in self._resting_orders:
                            wanted = desired_by_side.get(order["side"])
                            needs_refresh |= wanted is None or (wanted is not None and (
                                order["size"] > wanted[1] + 1e-12 or quote_refresh_reason(
                                    side="buy" if order["side"] == "bid" else "sell",
                                    price=order["price"], desired_price=wanted[0], mid=mid,
                                    age_s=t - last_quote_ts, max_age_s=quote_refresh_s,
                                    reprice_bps=self.config.quote_reprice_bps,
                                    min_edge_bps=self.config.maker_fee_bps + self.config.min_edge_bps,
                                ) is not None))
                        if needs_refresh:
                            for order in self._resting_orders:
                                order.setdefault("cancel_at", t + self.config.cancel_latency_s)
                            self._resting_orders = [o for o in self._resting_orders if t < o.get("cancel_at", float("inf"))]
                        if not self._resting_orders:
                            last_quote_ts = t
                            for side, price, size in desired:
                                self._resting_orders.append(
                                    {
                                        "side": side,
                                        "price": price,
                                        "size": size,
                                        "queue_ahead": self._visible_queue(side, price),
                                        "active_at": t + self.config.place_latency_s,
                                        "reduce_only": ((intent.current_inventory or 0) > 0 and side == "ask")
                                                       or ((intent.current_inventory or 0) < 0 and side == "bid"),
                                    })
                    elif intent:
                        for order in self._resting_orders:
                            order.setdefault("cancel_at", t + self.config.cancel_latency_s)
                        self._resting_orders = [o for o in self._resting_orders if t < o.get("cancel_at", float("inf"))]
                        if not self._resting_orders:
                            self._execute_close(t, mid, intent)

            self._pnl.mark(t, mid)
            self._equity = self.start_equity + self._pnl.explain(t, mid, include_events=False).total_pnl
            self._history.append({
                "ts": t, "mid": mid, "equity": self._equity,
                "position": self._inventory.position,
            })
            t += tick_s

        if self._flatten_at_end and self._history:
            # Terminal liquidation cost, not an extra simulated trading period.
            self._resting_orders = []
            self._execute_close(t, mid, ExecIntent(venue=self.config.exchange,
                                coin=coin, target_inventory=0.0, urgency="terminal"))
            self._pnl.mark(t, mid)
            self._equity = self.start_equity + self._pnl.explain(t, mid, include_events=False).total_pnl
            self._history.append(dict(ts=t, mid=mid, equity=self._equity, position=self._inventory.position))
        if self._decision_log is not None:
            self._decision_log.close()
            self._decision_log = None

        return self._history

    def _execute_close(self, t: float, mid: float, intent: ExecIntent) -> None:
        """Charge touch crossing, taker fee and configured additional slippage.

        ponytail: no full depth/market-impact model; micro-size replay only.
        """
        gap = intent.target_inventory - self._inventory.position
        if abs(gap) < 1e-12 or mid <= 0:
            return
        side = "buy" if gap > 0 else "sell"
        touch = mid if self._current_book is None else (
            self._current_book.asks[0][0] if gap > 0 else self._current_book.bids[0][0])
        price = touch * (1 + (1 if gap > 0 else -1) * self.config.close_slippage_bps / 1e4)
        fee = price * abs(gap) * self.config.taker_fee_bps / 1e4
        self._pnl.on_fill(Fill(ts=t, side=side, price=price, size=abs(gap), fee=fee,
                               mid_at_fill=mid, label=intent.urgency))
        self._inventory.position = self._pnl.position

    def _log_decision(self, record: BacktestDecisionRecord) -> None:
        if self._decision_log is None:
            return
        self._decision_log.write(json.dumps(dataclasses.asdict(record)) + "\n")
        self._decision_log.flush()

    def pnl_explain(self):
        """Full PnL breakdown (spread capture, markout, funding, fees) as
        of the last processed tick, with every fill/funding event."""
        return self._pnl.explain()

    def metrics(self) -> dict:
        """Summary stats for the rollout gates.

        ponytail: net edge + max DD + fill count only; markout ratio (beyond
        the ledger's own markout_pnl) and a liquidation model come with the
        real HL data fetcher.
        """
        breakdown = self._pnl.explain()
        gross = breakdown.gross_traded_notional
        final_equity = self._history[-1]["equity"] if self._history else self.start_equity
        net_pnl = final_equity - self.start_equity
        net_edge_bps = (net_pnl / gross) * 1e4 if gross else 0.0

        peak, max_dd = -float("inf"), 0.0
        for h in self._history:
            peak = max(peak, h["equity"])
            if peak > 0:
                max_dd = max(max_dd, (peak - h["equity"]) / peak)

        adverse_markout = max(-breakdown.markout_pnl, 0.0)
        captured_spread = max(breakdown.spread_capture, 0.0)
        markout_ratio = (
            adverse_markout / captured_spread
            if captured_spread else (float("inf") if adverse_markout else 0.0)
        )

        return {
            "final_equity": final_equity,
            "net_pnl": net_pnl,
            "net_edge_bps": net_edge_bps,
            "max_drawdown": max_dd,
            "n_fills": len(self._fills),
            "spread_capture": breakdown.spread_capture,
            "markout_pnl": breakdown.markout_pnl,
            "markout_ratio": markout_ratio,
            "funding_pnl": breakdown.funding_pnl,
            "fee_pnl": breakdown.fee_pnl,
            "liquidations": None,  # not modelled; never claim zero observed liquidations
            "max_initial_margin_fraction": max((abs(h["position"]) * h["mid"] /
                (self.config.leverage * h["equity"]) if h["equity"] > 0 else float("inf")
                for h in self._history), default=0.0),
        }

    def gate_report(self) -> dict:
        """Pass/fail against the rollout gates before any live order."""
        m = self.metrics()
        checks = {
            "net_edge_bps": (m["net_edge_bps"] > GATE_MIN_NET_EDGE_BPS, m["net_edge_bps"], GATE_MIN_NET_EDGE_BPS),
            "markout_ratio": (m["markout_ratio"] < GATE_MAX_MARKOUT_RATIO, m["markout_ratio"], GATE_MAX_MARKOUT_RATIO),
            "max_drawdown": (m["max_drawdown"] < GATE_MAX_DRAWDOWN, m["max_drawdown"], GATE_MAX_DRAWDOWN),
            "initial_margin": (m["max_initial_margin_fraction"] < .5, m["max_initial_margin_fraction"], .5),
        }
        return {
            "passed": all(passed for passed, _, _ in checks.values()),
            "checks": {name: {"passed": passed, "value": value, "threshold": threshold}
                       for name, (passed, value, threshold) in checks.items()},
        }

@dataclass
class Strategy:
    """The live keeper's decision path: regime -> RiskPolicy -> build_intent.
    Same window, same policy, same actuation as perp_bot.keeper.Keeper."""
    config: Any = None
    risk: RiskPolicy | None = None
    markout: MarkoutTracker = field(
        default_factory=lambda: MarkoutTracker(horizons=(10.0, 30.0, 60.0)))
    last_regime: object | None = field(default=None, init=False)
    last_decision: Decision | None = field(default=None, init=False)
    last_urgency: str | None = field(default=None, init=False)

    def __post_init__(self):
        if self.risk is None and self.config is not None:
            self.risk = RiskPolicy(cfg=self.config.risk)

    def __call__(self, config, inventory, equity, mid_history, ts, mid):
        if not mid_history or len(mid_history) < 2:
            return None
        history = list(mid_history[-MID_HISTORY_LEN:])
        regime = evaluate_regime(history)
        decision, urgency = self.risk.evaluate(
            ts=ts, mid=mid, equity=equity, inventory=inventory,
            regime=regime,
            avg_markout_bps=self.markout.avg_markout_bps(30.0),
            target_inventory=config.target_inventory,
            # initial-margin proxy: replay is never less cautious than live (no liquidation model)
            margin_available=initial_margin_available(
                equity=equity, position=inventory.position, mid=mid, leverage=config.leverage),
        )
        self.last_regime = regime
        self.last_decision = decision
        self.last_urgency = urgency
        return build_intent(config, decision, urgency, mid, inventory.position,
                            history, inventory.caps().max_position)

@dataclass
class BaselineStrategy:
    """Symmetric fixed-spread grid — the rollout baseline the AS strategy must
    beat. Centers on mid, ignores inventory/regime entirely."""
    spread_bps: float = 50.0
    size_frac: float = 0.1
    config: Any = None

    def __call__(self, config, inventory, equity, mid_history, ts, mid):
        if not mid_history:
            return None
        half = mid * (self.spread_bps / 2.0) / 1e4
        size = config.caps.max_position * self.size_frac
        return ExecIntent(
            venue=config.exchange, coin=config.coin,
            target_inventory=inventory.position,
            quote=QuoteSpec(
                bid_price=mid - half, ask_price=mid + half,
                bid_size=size, ask_size=size,
            ),
        )
