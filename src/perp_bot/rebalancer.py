"""
Portfolio-level netting / rebalancing controller (v1 — standalone periodic job).

Per hl-market-neutral-mm-deployment-plan.md §1.5: a slower control loop layered
on top of each keeper's own RiskPolicy, correcting drift in per-subaccount
imbalance and portfolio-level per-coin net exposure.

This module is pure logic + thin async IO wrappers — position/price providers
and the intent sender are injected callables so the core is fully testable
without a live OPMS.
"""

import asyncio
import dataclasses
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from mm_core.contracts import ExecIntent
from perp_bot.config import PerpPairConfig

logger = logging.getLogger(__name__)

PositionProvider = Callable[[str, str], Awaitable[tuple[float, float]]]
PriceProvider = Callable[[str], Awaitable[float]]
IntentSender = Callable[[ExecIntent], Awaitable[dict]]


@dataclass
class RebalanceConfig:
    imbalance_pct_threshold: float = 0.18
    portfolio_net_threshold: float = 0.05
    cycle_interval_s: float = 600.0
    passive_aggressive: bool = True
    # Plan §1.4 sizing: gross notional ≈ 2.5x equity at 3x leverage with
    # buffers. A correction that would push the account past this is not
    # proposed at all — underfunded accounts must not be market-ordered up
    # to their structural tilt (live finding 2026-09-15).
    max_gross_notional_multiple: float = 2.5


@dataclass
class RebalanceRecord:
    ts: float
    account_id: str
    coin: str
    pre_position: float
    q_target: float
    hedge_size: float
    hedge_side: str
    imbalance_pct: float
    trigger: str


@dataclass
class BasketRebalancer:
    configs: list[PerpPairConfig]
    position_provider: PositionProvider
    price_provider: PriceProvider
    intent_sender: IntentSender
    cfg: RebalanceConfig = field(default_factory=RebalanceConfig)
    decision_log_path: str | None = None
    shadow_mode: bool = False
    _decision_log: object = None
    _running: bool = False

    def __post_init__(self):
        if self.decision_log_path:
            self._decision_log = open(self.decision_log_path, "a")

    async def stop(self):
        self._running = False
        if self._decision_log:
            self._decision_log.close()
            self._decision_log = None

    def _account_configs(self) -> dict[str, list[PerpPairConfig]]:
        by_account: dict[str, list[PerpPairConfig]] = defaultdict(list)
        for cfg in self.configs:
            by_account[cfg.account_id].append(cfg)
        return by_account

    async def compute_imbalance(self, account_id: str) -> tuple[float, float]:
        account_cfgs = self._account_configs().get(account_id, [])
        imbalance = 0.0
        equity = 0.0
        for cfg in account_cfgs:
            position, acct_equity = await self.position_provider(account_id, cfg.coin)
            price = await self.price_provider(cfg.coin)
            drift = position - cfg.target_inventory
            imbalance += drift * price
            equity = acct_equity
        return imbalance, equity

    async def compute_portfolio_net(self) -> dict[str, float]:
        coin_net: dict[str, float] = defaultdict(float)
        for cfg in self.configs:
            position, _ = await self.position_provider(cfg.account_id, cfg.coin)
            coin_net[cfg.coin] += position
        return dict(coin_net)

    def _find_config(self, account_id: str, coin: str) -> PerpPairConfig | None:
        for cfg in self.configs:
            if cfg.account_id == account_id and cfg.coin == coin:
                return cfg
        return None

    async def rebalance_cycle(self) -> list[RebalanceRecord]:
        records: list[RebalanceRecord] = []
        ts = time.time()

        by_account = self._account_configs()
        for account_id, cfgs in by_account.items():
            imbalance, equity = await self.compute_imbalance(account_id)
            imbalance_pct = abs(imbalance) / equity if equity > 0 else 0.0
            if imbalance_pct <= self.cfg.imbalance_pct_threshold:
                continue

            # One snapshot of the account's book, then decide all-or-none.
            book: list[tuple[PerpPairConfig, float, float, float]] = []
            gross = 0.0
            for cfg in cfgs:
                position, _ = await self.position_provider(account_id, cfg.coin)
                price = await self.price_provider(cfg.coin)
                gross += abs(position) * price
                drift = position - cfg.target_inventory
                if abs(drift * price) >= 1e-6:
                    book.append((cfg, position, price, drift))

            proposed = sum(abs(drift) * price for _, _, price, drift in book)
            budget = equity * self.cfg.max_gross_notional_multiple
            if gross + proposed > budget:
                logger.warning(
                    f"{account_id}: capacity guard — gross ${gross:.2f} + correction "
                    f"${proposed:.2f} exceeds {self.cfg.max_gross_notional_multiple:.1f}x "
                    f"equity ${equity:.2f}; no correction sent"
                )
                for cfg, position, _, _ in book:
                    record = RebalanceRecord(
                        ts=ts, account_id=account_id, coin=cfg.coin,
                        pre_position=position, q_target=cfg.target_inventory,
                        hedge_size=0.0, hedge_side="none",
                        imbalance_pct=imbalance_pct, trigger="capacity_guard_skip",
                    )
                    records.append(record)
                    self._log_decision(record)
                continue

            for cfg, position, price, drift in book:
                hedge_size = -drift
                hedge_side = "buy" if hedge_size > 0 else "sell"
                urgency = "normal" if self.cfg.passive_aggressive else "immediate"

                intent = ExecIntent(
                    venue=cfg.exchange,
                    coin=cfg.coin,
                    account_id=account_id,
                    target_inventory=cfg.target_inventory,
                    current_inventory=position,
                    quote=None,
                    urgency=urgency,
                    strategy_hint="passive_aggressive" if self.cfg.passive_aggressive else "twap",
                    client_id=f"rebalance:{account_id}:{cfg.coin}:{ts:.0f}",
                )

                if not self.shadow_mode:
                    await self.intent_sender(intent)

                record = RebalanceRecord(
                    ts=ts,
                    account_id=account_id,
                    coin=cfg.coin,
                    pre_position=position,
                    q_target=cfg.target_inventory,
                    hedge_size=abs(hedge_size),
                    hedge_side=hedge_side,
                    imbalance_pct=imbalance_pct,
                    trigger="subaccount_imbalance",
                )
                records.append(record)
                self._log_decision(record)
                logger.info(
                    f"Rebalance: {account_id}/{cfg.coin} drift={drift:+.4f} "
                    f"hedge={hedge_side} {abs(hedge_size):.4f} "
                    f"imbalance_pct={imbalance_pct:.2%}"
                )

        portfolio_net = await self.compute_portfolio_net()
        for coin, net_pos in portfolio_net.items():
            if abs(net_pos) > self.cfg.portfolio_net_threshold:
                logger.warning(
                    f"Portfolio net drift: {coin} net={net_pos:+.4f} "
                    f"(threshold={self.cfg.portfolio_net_threshold})"
                )

        return records

    def _log_decision(self, record: RebalanceRecord) -> None:
        if self._decision_log is None:
            return
        d = dataclasses.asdict(record)
        self._decision_log.write(json.dumps(d) + "\n")
        self._decision_log.flush()

    async def run(self):
        self._running = True
        while self._running:
            try:
                await self.rebalance_cycle()
            except Exception as e:
                logger.error(f"Rebalance cycle error: {e}")
            await asyncio.sleep(self.cfg.cycle_interval_s)


__all__ = [
    "RebalanceConfig",
    "RebalanceRecord",
    "BasketRebalancer",
    "PositionProvider",
    "PriceProvider",
    "IntentSender",
]
