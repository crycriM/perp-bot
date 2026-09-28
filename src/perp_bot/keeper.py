import asyncio
import dataclasses
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from mm_core.contracts import ExecIntent, MarketSnapshot, QuoteSpec
from mm_core.inventory import PerpInventory
from mm_core.markout import MarkoutTracker
from mm_core.pnl import Fill, PnLLedger
from mm_core.risk_policy import Decision, RiskPolicy
from mm_core.regime import evaluate_regime
from mm_core.as_core import gueant_quote_prices
from mm_core.vol import VOLATILITY_MODELS

from perp_bot.config import PerpPairConfig
from perp_bot.margin_health import fail_closed_margin_available
from perp_bot.opms_client import OpmsClient

logger = logging.getLogger(__name__)

MID_HISTORY_LEN = 200  # regime/vol window; the backtest slices to the same length


def _quote_price(
    price: float, size: float, tick: float | None, *, round_up: bool,
) -> float | None:
    if size <= 0 or not math.isfinite(price) or price <= 0:
        return None
    if tick is not None:
        units = price / tick
        price = (math.ceil(units - 1e-12) if round_up
                 else math.floor(units + 1e-12)) * tick
    return price if price > 0 else None

@dataclass
class DecisionRecord:
    source: str
    ts: float
    mid: float
    regime: str
    decision: str
    urgency: str
    inventory: float
    equity: float
    margin_available: float
    total_pnl: float
    intent_sent: bool
    intent: ExecIntent | None
    regime_state: str = "unknown"
    regime_transition: str | None = None
    regime_raw_open: bool | None = None
    regime_provisional: bool = False
    regime_half_life: float | None = None
    regime_hurst: float | None = None
    regime_trending: bool | None = None
    regime_sample_count: int | None = None
    regime_sample_interval_s: float | None = None
    regime_history_span_s: float | None = None

class Keeper:
    """Keeper loop: ingest, evaluate, actuate via intents."""

    def __init__(
        self,
        client: OpmsClient,
        config: PerpPairConfig,
        tick_s: float = 1.0,
        decision_log_path: str | None = None,
        shadow_mode: bool = False,
    ):
        self.client = client
        self.config = config
        self.tick_s = tick_s
        self.shadow_mode = shadow_mode
        self._risk = RiskPolicy(cfg=config.risk)
        self.intent_transform: Callable[[Decision, ExecIntent | None, float, float], ExecIntent | None] | None = None
        self._markout = MarkoutTracker(horizons=(10.0, 30.0, 60.0))
        self._pnl = PnLLedger(venue=config.exchange, symbol=config.coin)
        self._inventory = PerpInventory(position=0.0, _caps=config.caps)
        self._equity = 0.0
        self._margin_available: float | None = None
        self._mid_history: deque = deque(maxlen=MID_HISTORY_LEN)
        self._running = False
        self._last_funding_ts: float | None = None
        # JSON-lines decision record — the shadow-mode artifact
        self._decision_log = open(decision_log_path, "a") if decision_log_path else None

    async def _on_snapshot(self, data):
        ts = data.get("ts", time.time())
        mid = data.get("mid", 0.0)
        self._mid_history.append((ts, mid))
        self._markout.on_mid(ts, mid)
        self._pnl.mark(ts, mid)
        rate = data.get("funding_rate")
        if rate is not None:
            self._accrue_funding(ts, rate, mid)

    def _accrue_funding(self, ts: float, rate: float, mid: float) -> None:
        """Funding is a continuously-quoted rate paid once per interval —
        accrue it pro-rata over elapsed time rather than applying the full
        rate on every tick (which would massively overcount)."""
        if self._last_funding_ts is not None:
            dt = ts - self._last_funding_ts
            fraction = dt / self.config.funding_interval_s
            self._pnl.on_funding(ts, rate, mid, dt=fraction)
        self._last_funding_ts = ts

    async def _on_fill(self, data):
        ts = data.get("ts", time.time())
        side = data.get("side", "buy")
        price = data.get("price", 0.0)
        size = data.get("size", 0.0)
        fee = data.get("fee", 0.0)
        mid_at_fill = self._mid_history[-1][1] if self._mid_history else price
        self._markout.on_fill(ts, side, price, size)
        self._pnl.on_fill(Fill(ts=ts, side=side, price=price, size=size,
                                fee=fee, mid_at_fill=mid_at_fill))
        delta = size if side == "buy" else -size
        self._inventory.position += delta
        logger.info(f"Fill: {side} {size} @ {price}, pos={self._inventory.position}")

    async def _on_error(self, error):
        logger.error(f"OPMS error: {error}")
        try:
            positions = await self.client.resnapshot_positions()
        except Exception as e:
            # Best-effort reconciliation: the server may still be down right
            # after a disconnect. This is called from inside OpmsClient's own
            # except block with no protection there, so letting this raise
            # kills the WS loop's task outright — no further reconnect
            # attempts ever happen. Skip and let the next reconnect retry.
            logger.warning(f"Resnapshot after error failed, will retry on next reconnect: {e}")
            return
        self._apply_positions(positions)

    def _apply_positions(self, positions: dict) -> None:
        """Reconcile local inventory with the authoritative OPMS snapshot."""
        pos = positions.get(self.config.coin) if positions else None
        if pos is not None:
            if pos.position != self._inventory.position:
                logger.warning(
                    f"Inventory drift: local={self._inventory.position} "
                    f"opms={pos.position} — adopting OPMS value"
                )
            self._reconcile_pnl_position(pos.position)
            self._inventory.position = pos.position
            self._equity = pos.equity
            self._margin_available = pos.margin_available

    def _reconcile_pnl_position(self, opms_position: float) -> None:
        """A missed fill (dropped websocket message, restart) leaves the
        ledger's own position tracker out of sync with OPMS truth. Rather
        than let every PnL figure silently drift, inject a synthetic
        reconciling fill sized to close the gap, priced at the last known
        mid — an honest "we don't know what we actually paid" assumption
        that carries zero spread/markout impact of its own."""
        gap = opms_position - self._pnl.position
        if abs(gap) < 1e-12 or not self._mid_history:
            return
        ts, mid = self._mid_history[-1]
        logger.warning(f"PnL ledger position drift: local={self._pnl.position} "
                        f"opms={opms_position} — reconciling {gap:+.6f} at mid={mid}")
        side = "buy" if gap > 0 else "sell"
        self._pnl.on_fill(Fill(ts=ts, side=side, price=mid, size=abs(gap),
                                mid_at_fill=mid, label="reconcile"))

    async def start(self):
        coin = self.config.coin
        client = self.client
        client.on_snapshot(self._on_snapshot)
        client.on_fill(self._on_fill)
        client.on_error(self._on_error)
        await client.start()
        self._running = True
        while self._running:
            await self._tick()
            await asyncio.sleep(self.tick_s)

    async def stop(self):
        self._running = False
        await self.client.stop()
        if self._decision_log is not None:
            self._decision_log.close()
            self._decision_log = None

    def pnl_explain(self):
        """Full PnL breakdown as of the last snapshot mid, with every
        fill/funding event that produced it."""
        return self._pnl.explain()

    async def _tick(self):
        coin = self.config.coin
        try:
            if not self._mid_history:
                return

            ts, mid = self._mid_history[-1]

            positions = await self.client.get_positions()
            self._apply_positions(positions)
            if positions.get(coin) is None:
                self._equity = 1.0
                self._margin_available = fail_closed_margin_available(
                    None,
                    source=f"missing OPMS position snapshot for {coin}",
                )

            prices = [p for _, p in self._mid_history]
            regime = evaluate_regime(list(self._mid_history))

            avg_markout = self._markout.avg_markout_bps(30.0)
            decision, urgency = self._risk.evaluate(
                ts=ts, mid=mid, equity=self._equity,
                inventory=self._inventory, regime=regime,
                avg_markout_bps=avg_markout,
                target_inventory=self.config.target_inventory,
                margin_available=self._margin_available,
            )
            gate = self._risk.last_regime_gate

            intent = self._actuate(decision, urgency, ts, mid, regime)
            if self.intent_transform is not None:
                intent = self.intent_transform(decision, intent, self._inventory.position, mid)
            intent_sent = False
            if intent:
                if self.shadow_mode:
                    logger.info("Shadow mode: logging intent without sending to OPMS")
                else:
                    await self.client.send_intent(intent)
                    intent_sent = True

            total_pnl = self._pnl.explain(ts, mid, include_events=False).total_pnl
            record = DecisionRecord(
                source="shadow" if self.shadow_mode else "live",
                ts=ts, mid=mid,
                regime=f"hl={regime.half_life:.0f},h={regime.hurst:.2f}",
                decision=decision.value, urgency=urgency,
                inventory=self._inventory.position,
                equity=self._equity,
                margin_available=self._margin_available,
                total_pnl=total_pnl,
                intent_sent=intent_sent,
                intent=intent,
                regime_state=gate.state.value if gate else "unknown",
                regime_transition=gate.transition if gate else None,
                regime_raw_open=gate.raw_open if gate else None,
                regime_provisional=gate.provisional if gate else False,
                regime_half_life=regime.half_life,
                regime_hurst=regime.hurst,
                regime_trending=regime.trending,
                regime_sample_count=regime.sample_count,
                regime_sample_interval_s=regime.sample_interval_s,
                regime_history_span_s=regime.history_span_s,
            )
            self._log_decision(record)
            logger.info(
                f"tick t={ts:.0f} mid={mid:.2f} reg={record.regime} "
                f"dec={decision.value} urg={urgency} inv={self._inventory.position:.2f} "
                f"eq={self._equity:.2f}"
            )
        except Exception as e:
            logger.error(f"Tick error: {e}")

    def _log_decision(self, record: DecisionRecord) -> None:
        """Append the per-cycle decision record as a JSON line (shadow artifact)."""
        if self._decision_log is None:
            return
        d = dataclasses.asdict(record)
        self._decision_log.write(json.dumps(d) + "\n")
        self._decision_log.flush()

    def _actuate(self, decision: Decision, urgency: str, ts: float, mid: float, regime) -> ExecIntent | None:
        return build_intent(
            self.config, decision, urgency, mid, self._inventory.position,
            list(self._mid_history), self._inventory.caps().max_position,
        )


def build_intent(
    config: PerpPairConfig, decision: Decision, urgency: str, mid: float,
    pos: float, mid_history: list, max_position: float,
) -> ExecIntent | None:
    """Decision -> ExecIntent. Pure, so the live keeper and the backtest
    actuate through the exact same code."""
    coin = config.coin
    gamma = config.gamma
    sigma = VOLATILITY_MODELS["close_to_close"](mid_history)
    kappa = config.kappa

    q_target = config.target_inventory
    nominal_size = config.quote_size or max_position * 0.1

    if decision == Decision.QUOTE:
        bid_price, ask_price = gueant_quote_prices(
            mid, pos, gamma, sigma, kappa, q_target=q_target,
        )
        bid_size, ask_size = bounded_quote_sizes(
            pos, q_target, max_position, nominal_size
        )
        return ExecIntent(
            venue=config.exchange, coin=coin, account_id=config.account_id,
            target_inventory=q_target,
            current_inventory=pos,
            quote=QuoteSpec(
                bid_price=_quote_price(
                    bid_price, bid_size, config.price_tick, round_up=False,
                ),
                ask_price=_quote_price(
                    ask_price, ask_size, config.price_tick, round_up=True,
                ),
                bid_size=bid_size,
                ask_size=ask_size,
            ),
            urgency=urgency,
        )
    elif decision == Decision.WIDEN:
        bid_price, ask_price = gueant_quote_prices(
            mid, pos, gamma, sigma, kappa, q_target=q_target,
            spread_multiplier=config.widen_factor,
        )
        bid_size, ask_size = bounded_quote_sizes(
            pos, q_target, max_position, nominal_size * 0.5
        )
        return ExecIntent(
            venue=config.exchange, coin=coin, account_id=config.account_id,
            target_inventory=q_target,
            current_inventory=pos,
            quote=QuoteSpec(
                bid_price=_quote_price(
                    bid_price, bid_size, config.price_tick, round_up=False,
                ),
                ask_price=_quote_price(
                    ask_price, ask_size, config.price_tick, round_up=True,
                ),
                bid_size=bid_size,
                ask_size=ask_size,
            ),
            urgency=urgency,
        )
    elif decision == Decision.STOP_QUOTING:
        # Cancel this leg's quotes. Only the portfolio coordinator may set a
        # nontrivial stop target: zero net USDC is a basket, not a leg, goal.
        return ExecIntent(
            venue=config.exchange, coin=coin, account_id=config.account_id,
            target_inventory=pos,
            current_inventory=pos,
            quote=None,
            urgency=urgency,
            strategy_hint="passive_aggressive",
        )
    elif decision == Decision.DE_RISK:
        return ExecIntent(
            venue=config.exchange, coin=coin, account_id=config.account_id,
            target_inventory=q_target,
            current_inventory=pos,
            quote=None,
            urgency=urgency,
            strategy_hint="passive_aggressive",
        )
    elif decision == Decision.EMERGENCY_EXIT:
        return ExecIntent(
            venue=config.exchange, coin=coin, account_id=config.account_id,
            target_inventory=0.0,  # full flatten always overrides any structural tilt
            current_inventory=pos,
            quote=None,
            urgency="emergency",
            strategy_hint="twap",
        )
    return None

def bounded_quote_sizes(
    position: float, target: float, cap: float, nominal_size: float
) -> tuple[float, float]:
    """Return quote sizes whose *single full fill* stays inside safety bounds.

    Inventory is capped relative to the structural target.  The quote that
    moves inventory toward that target is additionally clipped to land on
    it, never jump across it.  For a directional structural leg, the
    opposite quote is clipped at flat as well; the execution layer also
    marks that side reduce-only to protect against stale overlapping
    orders and position-cache lag.
    """
    lower = target - cap
    upper = target + cap
    bid_size = min(nominal_size, max(upper - position, 0.0))
    ask_size = min(nominal_size, max(position - lower, 0.0))

    if position < target:
        bid_size = min(bid_size, target - position)
    elif position > target:
        ask_size = min(ask_size, position - target)

    # An inventory-reducing maker order is sent reduce-only by the
    # execution layer. Cap it to the position it can actually close, both
    # to avoid venue rejection and to preserve the current sign. This
    # closed a live-soak failure where a Buy orde was followed by a
    # larger ask fill, leaving an unintended short position.
    if position > 0:
        ask_size = min(ask_size, position)
    elif position < 0:
        bid_size = min(bid_size, -position)
    if target > 0 and position <= 0:
        ask_size = 0.0
    elif target < 0 and position >= 0:
        bid_size = 0.0

    epsilon = 1e-12
    return (
        0.0 if bid_size < epsilon else bid_size,
        0.0 if ask_size < epsilon else ask_size,
    )
