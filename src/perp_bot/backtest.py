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

from perp_bot.keeper import MID_HISTORY_LEN, build_intent

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

    def __init__(self, config, start_equity: float = 1.0, decision_log_path: str | None = None):
        self.config = config
        self.start_equity = start_equity
        self._trades: list[Backtrade] = []
        self._funding_events: list = []
        self._snapshots: list[MarketSnapshot] = []
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

    def _fill_rule(self, ts: float, trade: Backtrade) -> list[BacktestFill]:
        """A resting order fills when a trade prints through its price."""
        filled: list[BacktestFill] = []
        remaining = []
        for order in self._resting_orders:
            if trade.size <= 0:
                remaining.append(order)
                continue
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
        return filled

    async def run(self, duration_s: float = 3600.0, tick_s: float = 1.0):
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
        if tick_s <= 0:
            raise ValueError("tick_s must be > 0")

        t = t0
        while t < t_end:
            # Process snapshots up to t
            markout = getattr(self._strategy, "markout", None)
            while self._snapshots and self._snapshots[0].ts <= t:
                snap = self._snapshots.pop(0)
                self._mid_history.append((snap.ts, snap.mid))
                if markout is not None:
                    markout.on_mid(snap.ts, snap.mid)

            mid = self._mid_history[-1][1] if self._mid_history else 0.0

            # Process trades — booked through the shared ledger so realized/
            # unrealized/spread/markout are the same math the live keeper uses
            while next_trade and next_trade.ts <= t:
                for fill in self._fill_rule(t, next_trade):
                    self._fills.append(fill)
                    side = "sell" if fill.side == "ask" else "buy"
                    self._pnl.on_fill(Fill(ts=t, side=side, price=fill.price,
                                            size=fill.size, mid_at_fill=mid))
                    if markout is not None:
                        markout.on_fill(t, side, fill.price, fill.size)

                next_trade = next(trades_iter, None)
            self._inventory.position = self._pnl.position

            # Process funding (longs pay when rate is positive) — these are
            # discrete scheduled payments, not a continuously-quoted rate,
            # so each one applies in full (dt=None) rather than pro-rata.
            while next_funding and next_funding.get("ts", 0) <= t:
                fr = next_funding.get("rate", 0.0)
                self._pnl.on_funding(t, fr, mid)
                next_funding = next(funding_iter, None)

            self._pnl.mark(t, mid)
            self._equity = self.start_equity + self._pnl.explain(t, mid, include_events=False).total_pnl

            # Strategy decision every tick: cancel/replace, never stack quotes
            if t - last_quote_ts >= tick_s:
                last_quote_ts = t
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
                    self._resting_orders = []
                    if intent and intent.quote:
                        for side in ("bid", "ask"):
                            price = getattr(intent.quote, f"{side}_price")
                            size = getattr(intent.quote, f"{side}_size")
                            if price is not None and size > 0:
                                self._resting_orders.append(
                                    {"side": side, "price": price, "size": size})
                    elif intent:
                        self._execute_close(t, mid, intent)

            self._history.append({
                "ts": t, "mid": mid, "equity": self._equity,
                "position": self._inventory.position,
            })
            t += tick_s

        if self._decision_log is not None:
            self._decision_log.close()
            self._decision_log = None

        return self._history

    def _execute_close(self, t: float, mid: float, intent: ExecIntent) -> None:
        """Quote-less intents (stop_quoting / de_risk / emergency_exit): OPMS
        works the position to target_inventory. Filled at once, at mid.

        ponytail: instant mid fill, no taker fee/slippage — optimistic; add a
        taker cost once OPMS close fills are measured.
        """
        gap = intent.target_inventory - self._inventory.position
        if abs(gap) < 1e-12 or mid <= 0:
            return
        side = "buy" if gap > 0 else "sell"
        self._pnl.on_fill(Fill(ts=t, side=side, price=mid, size=abs(gap),
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

        markout_ratio = (
            abs(breakdown.markout_pnl) / abs(breakdown.spread_capture)
            if breakdown.spread_capture else 0.0
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
            # ponytail: no margin/leverage model in this backtester, so there's
            # nothing that can liquidate; gate always reads 0 until one exists.
            "liquidations": 0,
        }

    def gate_report(self) -> dict:
        """Pass/fail against the rollout gates before any live order."""
        m = self.metrics()
        checks = {
            "net_edge_bps": (m["net_edge_bps"] > GATE_MIN_NET_EDGE_BPS, m["net_edge_bps"], GATE_MIN_NET_EDGE_BPS),
            "markout_ratio": (m["markout_ratio"] < GATE_MAX_MARKOUT_RATIO, m["markout_ratio"], GATE_MAX_MARKOUT_RATIO),
            "max_drawdown": (m["max_drawdown"] < GATE_MAX_DRAWDOWN, m["max_drawdown"], GATE_MAX_DRAWDOWN),
            "liquidations": (m["liquidations"] == 0, m["liquidations"], 0),
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
            # ponytail: no margin model in the backtest, so margin checks are off
            margin_available=None,
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
