import asyncio
import dataclasses
import json
import logging
import time
from collections import deque
from dataclasses import dataclass

from mm_core.contracts import ExecIntent, MarketSnapshot, QuoteSpec
from mm_core.inventory import PerpInventory
from mm_core.markout import MarkoutTracker
from mm_core.pnl import Fill, PnLLedger
from mm_core.risk_policy import Decision, RiskPolicy
from mm_core.regime import evaluate_regime
from mm_core.as_core import gueant_half_spread, gueant_reservation_price
from mm_core.vol import VOLATILITY_MODELS

from perp_bot.config import PerpPairConfig
from perp_bot.opms_client import OpmsClient

logger = logging.getLogger(__name__)

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
    total_pnl: float
    intent_sent: bool
    intent: ExecIntent | None

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
        self._markout = MarkoutTracker(horizons=(10.0, 30.0, 60.0))
        self._pnl = PnLLedger(venue=config.exchange, symbol=config.coin)
        self._inventory = PerpInventory(position=0.0, _caps=config.caps)
        self._equity = 0.0
        self._mid_history: deque = deque(maxlen=200)
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

            prices = [p for _, p in self._mid_history]
            regime = evaluate_regime(list(self._mid_history))

            avg_markout = self._markout.avg_markout_bps(30.0)
            decision, urgency = self._risk.evaluate(
                ts=ts, mid=mid, equity=self._equity,
                inventory=self._inventory, regime=regime,
                avg_markout_bps=avg_markout,
                target_inventory=self.config.target_inventory,
            )

            intent = self._actuate(decision, urgency, ts, mid, regime)
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
                total_pnl=total_pnl,
                intent_sent=intent_sent,
                intent=intent,
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

    def _actuate(self, decision: Decision, urgency: str, ts: float, mid: float, regime) -> ExecIntent:
        coin = self.config.coin
        gamma = self.config.gamma
        sigma = VOLATILITY_MODELS["close_to_close"](list(self._mid_history))
        kappa = self.config.kappa

        pos = self._inventory.position
        q_target = self.config.target_inventory

        if decision == Decision.QUOTE:
            r = gueant_reservation_price(mid, pos, gamma, sigma, kappa, q_target=q_target)
            hs = gueant_half_spread(gamma, sigma, kappa)
            return ExecIntent(
                venue=self.config.exchange, coin=coin, account_id=self.config.account_id,
                target_inventory=pos,
                current_inventory=pos,
                quote=QuoteSpec(
                    bid_price=r - hs, ask_price=r + hs,
                    bid_size=self._inventory.caps().max_position * 0.1,
                    ask_size=self._inventory.caps().max_position * 0.1,
                ),
                urgency=urgency,
            )
        elif decision == Decision.WIDEN:
            r = gueant_reservation_price(mid, pos, gamma, sigma, kappa, q_target=q_target)
            hs = gueant_half_spread(gamma, sigma, kappa) * self.config.widen_factor
            return ExecIntent(
                venue=self.config.exchange, coin=coin, account_id=self.config.account_id,
                target_inventory=pos,
                current_inventory=pos,
                quote=QuoteSpec(
                    bid_price=r - hs, ask_price=r + hs,
                    bid_size=self._inventory.caps().max_position * 0.05,
                    ask_size=self._inventory.caps().max_position * 0.05,
                ),
                urgency=urgency,
            )
        elif decision == Decision.STOP_QUOTING:
            return ExecIntent(
                venue=self.config.exchange, coin=coin, account_id=self.config.account_id,
                target_inventory=pos,
                current_inventory=pos,
                quote=None,
                urgency=urgency,
            )
        elif decision == Decision.DE_RISK:
            return ExecIntent(
                venue=self.config.exchange, coin=coin, account_id=self.config.account_id,
                target_inventory=q_target,
                current_inventory=pos,
                quote=None,
                urgency=urgency,
                strategy_hint="passive_aggressive",
            )
        elif decision == Decision.EMERGENCY_EXIT:
            return ExecIntent(
                venue=self.config.exchange, coin=coin, account_id=self.config.account_id,
                target_inventory=0.0,  # full flatten always overrides any structural tilt
                current_inventory=pos,
                quote=None,
                urgency="emergency",
                strategy_hint="twap",
            )
        return None
