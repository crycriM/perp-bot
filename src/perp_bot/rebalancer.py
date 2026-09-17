"""
Portfolio-level netting / rebalancing controller (v1 — standalone periodic job).

A slower control loop layered on top of each keeper's own RiskPolicy,
correcting drift in per-subaccount imbalance and portfolio-level per-coin net
exposure.

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
    # Sizing keeps gross notional within the configured equity budget.
    # at 2.5x equity with buffers. The guard below converts that allowance to
    # initial margin using each pair's configured leverage, so a correction
    # can use higher leverage when the account has less collateral.
    max_gross_notional_multiple: float = 2.5
    reference_leverage: float = 3.0


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
            # One snapshot of the account's book: position, equity and price are
            # read once per coin and reused for the imbalance gate, the book and
            # the capacity guard — a fill landing mid-cycle cannot desync two
            # separate reads, and live I/O is not doubled.
            book: list[tuple[PerpPairConfig, float, float, float]] = []
            imbalance = 0.0
            equity = 0.0
            gross = 0.0
            projected_initial_margin = 0.0
            for cfg in cfgs:
                position, acct_equity = await self.position_provider(account_id, cfg.coin)
                price = await self.price_provider(cfg.coin)
                equity = acct_equity
                gross += abs(position) * price
                leverage = max(float(getattr(cfg, "leverage", 1)), 1.0)
                projected_initial_margin += abs(cfg.target_inventory) * price / leverage
                drift = position - cfg.target_inventory
                imbalance += drift * price
                if abs(drift * price) >= 1e-6:
                    book.append((cfg, position, price, drift))

            imbalance_pct = abs(imbalance) / equity if equity > 0 else 0.0
            if imbalance_pct <= self.cfg.imbalance_pct_threshold:
                continue

            # Projected gross after the correction lands: each corrected leg ends
            # at its target, so it removes the drift-sized exposure rather than
            # adding |drift| on top of the current gross. `gross + |drift|`
            # double-counts and blocks exactly the de-risking correction the
            # imbalance trigger exists to make (review 2026-09-16 #1).
            reduced = sum(abs(position) * price for _, position, price, _ in book)
            landed = sum(abs(cfg.target_inventory) * price for cfg, _, price, _ in book)
            projected_gross = gross - reduced + landed
            margin_budget = equity * (
                self.cfg.max_gross_notional_multiple / self.cfg.reference_leverage
            )
            # A correction that reduces gross exposure is always useful even
            # if the already-open book is above the configured margin budget.
            # Only block a correction that would add exposure and require more
            # initial margin than the account's buffered collateral allows.
            if projected_gross > gross and projected_initial_margin > margin_budget:
                logger.warning(
                    f"{account_id}: capacity guard — projected initial margin "
                    f"${projected_initial_margin:.2f} exceeds "
                    f"${margin_budget:.2f} ({self.cfg.max_gross_notional_multiple:.1f}x "
                    f"gross allowance at {self.cfg.reference_leverage:.1f}x) on "
                    f"${equity:.2f} equity; projected gross ${projected_gross:.2f}; "
                    "no correction sent"
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
