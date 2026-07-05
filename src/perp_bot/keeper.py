import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

from mm_core.contracts import ExecIntent, MarketSnapshot, QuoteSpec
from mm_core.inventory import PerpInventory
from mm_core.markout import MarkoutTracker
from mm_core.risk_policy import Decision, RiskPolicy
from mm_core.regime import evaluate_regime
from mm_core.as_core import gueant_half_spread, gueant_reservation_price
from mm_core.vol import VOLATILITY_MODELS

from perp_bot.config import PerpPairConfig
from perp_bot.opms_client import OpmsClient

logger = logging.getLogger(__name__)

@dataclass
class DecisionRecord:
    ts: float
    mid: float
    regime: str
    decision: str
    urgency: str
    inventory: float
    equity: float
    intent: ExecIntent | None

class Keeper:
    """Keeper loop: ingest, evaluate, actuate via intents."""

    def __init__(self, client: OpmsClient, config: PerpPairConfig, tick_s: float = 1.0):
        self.client = client
        self.config = config
        self.tick_s = tick_s
        self._risk = RiskPolicy(cfg=config.risk)
        self._markout = MarkoutTracker(horizons=(10.0, 30.0, 60.0))
        self._inventory = PerpInventory(position=0.0, _caps=config.caps)
        self._equity = 0.0
        self._mid_history: deque = deque(maxlen=200)
        self._running = False

    async def _on_snapshot(self, data):
        ts = data.get("ts", time.time())
        mid = data.get("mid", 0.0)
        self._mid_history.append((ts, mid))
        self._markout.on_mid(ts, mid)
        if data.get("funding_rate") is not None:
            pass

    async def _on_fill(self, data):
        ts = data.get("ts", time.time())
        side = data.get("side", "buy")
        price = data.get("price", 0.0)
        size = data.get("size", 0.0)
        self._markout.on_fill(ts, side, price, size)
        delta = size if side == "buy" else -size
        self._inventory.position += delta
        logger.info(f"Fill: {side} {size} @ {price}, pos={self._inventory.position}")

    async def _on_error(self, error):
        logger.error(f"OPMS error: {error}")
        await self.client.resnapshot_positions()

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

    async def _tick(self):
        coin = self.config.coin
        try:
            if not self._mid_history:
                return

            ts, mid = self._mid_history[-1]

            positions = await self.client.get_positions()
            pos_data = positions.get(coin)
            if pos_data:
                self._equity = pos_data.equity
            else:
                self._equity = 1.0

            prices = [p for _, p in self._mid_history]
            regime = evaluate_regime(list(self._mid_history))

            avg_markout = self._markout.avg_markout_bps(30.0)
            decision, urgency = self._risk.evaluate(
                ts=ts, mid=mid, equity=self._equity,
                inventory=self._inventory, regime=regime,
                avg_markout_bps=avg_markout,
            )

            intent = self._actuate(decision, urgency, ts, mid, regime)
            if intent:
                await self.client.send_intent(intent)

            record = DecisionRecord(
                ts=ts, mid=mid,
                regime=f"hl={regime.half_life:.0f},h={regime.hurst:.2f}",
                decision=decision.value, urgency=urgency,
                inventory=self._inventory.position,
                equity=self._equity,
                intent=intent,
            )
            logger.info(
                f"tick t={ts:.0f} mid={mid:.2f} reg={record.regime} "
                f"dec={decision.value} urg={urgency} inv={self._inventory.position:.2f} "
                f"eq={self._equity:.2f}"
            )
        except Exception as e:
            logger.error(f"Tick error: {e}")

    def _actuate(self, decision: Decision, urgency: str, ts: float, mid: float, regime) -> ExecIntent:
        coin = self.config.coin
        gamma = self.config.gamma
        sigma = VOLATILITY_MODELS["close_to_close"](list(self._mid_history))
        kappa = self.config.kappa

        if decision == Decision.QUOTE:
            r = gueant_reservation_price(mid, self._inventory.position, gamma, sigma, kappa)
            hs = gueant_half_spread(gamma, sigma, kappa)
            return ExecIntent(
                venue="hl", coin=coin,
                target_inventory=self._inventory.position,
                quote=QuoteSpec(
                    bid_price=r - hs, ask_price=r + hs,
                    bid_size=self._inventory.caps.max_position * 0.1,
                    ask_size=self._inventory.caps.max_position * 0.1,
                ),
                urgency=urgency,
            )
        elif decision == Decision.WIDEN:
            r = gueant_reservation_price(mid, self._inventory.position, gamma, sigma, kappa)
            hs = gueant_half_spread(gamma, sigma, kappa) * 2.0
            return ExecIntent(
                venue="hl", coin=coin,
                target_inventory=self._inventory.position,
                quote=QuoteSpec(
                    bid_price=r - hs, ask_price=r + hs,
                    bid_size=self._inventory.caps.max_position * 0.05,
                    ask_size=self._inventory.caps.max_position * 0.05,
                ),
                urgency=urgency,
            )
        elif decision == Decision.STOP_QUOTING:
            return ExecIntent(
                venue="hl", coin=coin,
                target_inventory=self._inventory.position,
                quote=None,
                urgency=urgency,
            )
        elif decision == Decision.DE_RISK:
            return ExecIntent(
                venue="hl", coin=coin,
                target_inventory=0.0,
                quote=None,
                urgency=urgency,
                strategy_hint="passive_aggressive",
            )
        elif decision == Decision.EMERGENCY_EXIT:
            return ExecIntent(
                venue="hl", coin=coin,
                target_inventory=0.0,
                quote=None,
                urgency="emergency",
                strategy_hint="twap",
            )
        return None
