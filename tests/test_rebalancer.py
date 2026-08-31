import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from mm_core.inventory import Caps

from perp_bot.config import PerpPairConfig
from perp_bot.rebalancer import BasketRebalancer, RebalanceConfig


def _make_basket(
    eth_target: float = 0.0,
    sol_target: float = 0.0,
    account_a: str = "basket_a",
    account_b: str = "basket_b",
) -> list[PerpPairConfig]:
    return [
        PerpPairConfig(
            coin="ETH", exchange="hyperliquid", account_id=account_a,
            target_inventory=eth_target,
            caps=Caps(max_position=1.0, critical_position=2.0),
        ),
        PerpPairConfig(
            coin="SOL", exchange="hyperliquid", account_id=account_a,
            target_inventory=sol_target,
            caps=Caps(max_position=10.0, critical_position=20.0),
        ),
        PerpPairConfig(
            coin="ETH", exchange="hyperliquid", account_id=account_b,
            target_inventory=-eth_target,
            caps=Caps(max_position=1.0, critical_position=2.0),
        ),
        PerpPairConfig(
            coin="SOL", exchange="hyperliquid", account_id=account_b,
            target_inventory=-sol_target,
            caps=Caps(max_position=10.0, critical_position=20.0),
        ),
    ]


def _position_provider(positions: dict[tuple[str, str], tuple[float, float]]):
    async def provider(account_id: str, coin: str) -> tuple[float, float]:
        return positions.get((account_id, coin), (0.0, 10000.0))
    return provider


def _price_provider(prices: dict[str, float]):
    async def provider(coin: str) -> float:
        return prices.get(coin, 100.0)
    return provider


def _intent_sink():
    sent = []
    async def sender(intent):
        sent.append(intent)
        return {"status": "ok"}
    return sent, sender


@pytest.mark.asyncio
async def test_compute_imbalance_at_target_is_zero():
    configs = _make_basket(eth_target=0.5, sol_target=-5.0)
    positions = {
        ("basket_a", "ETH"): (0.5, 10000.0),
        ("basket_a", "SOL"): (-5.0, 10000.0),
    }
    rebalancer = BasketRebalancer(
        configs=configs,
        position_provider=_position_provider(positions),
        price_provider=_price_provider({"ETH": 3000.0, "SOL": 150.0}),
        intent_sender=lambda _: asyncio.coroutine(lambda: {"status": "ok"})(),
    )
    imbalance, equity = await rebalancer.compute_imbalance("basket_a")
    assert abs(imbalance) < 1e-9
    assert equity == 10000.0


@pytest.mark.asyncio
async def test_compute_imbalance_drift_from_target():
    configs = _make_basket(eth_target=0.5)
    positions = {
        ("basket_a", "ETH"): (0.8, 10000.0),
        ("basket_a", "SOL"): (0.0, 10000.0),
    }
    rebalancer = BasketRebalancer(
        configs=configs,
        position_provider=_position_provider(positions),
        price_provider=_price_provider({"ETH": 3000.0, "SOL": 150.0}),
        intent_sender=lambda _: asyncio.coroutine(lambda: {"status": "ok"})(),
    )
    imbalance, equity = await rebalancer.compute_imbalance("basket_a")
    assert imbalance == pytest.approx(0.3 * 3000.0)


@pytest.mark.asyncio
async def test_compute_portfolio_net_mirror_is_zero():
    configs = _make_basket(eth_target=0.5, sol_target=-5.0)
    positions = {
        ("basket_a", "ETH"): (0.5, 10000.0),
        ("basket_a", "SOL"): (-5.0, 10000.0),
        ("basket_b", "ETH"): (-0.5, 10000.0),
        ("basket_b", "SOL"): (5.0, 10000.0),
    }
    rebalancer = BasketRebalancer(
        configs=configs,
        position_provider=_position_provider(positions),
        price_provider=_price_provider({"ETH": 3000.0, "SOL": 150.0}),
        intent_sender=lambda _: asyncio.coroutine(lambda: {"status": "ok"})(),
    )
    net = await rebalancer.compute_portfolio_net()
    assert net["ETH"] == pytest.approx(0.0)
    assert net["SOL"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_rebalance_cycle_no_trigger_below_threshold():
    configs = _make_basket(eth_target=0.5)
    positions = {
        ("basket_a", "ETH"): (0.52, 10000.0),
        ("basket_a", "SOL"): (0.0, 10000.0),
        ("basket_b", "ETH"): (-0.5, 10000.0),
        ("basket_b", "SOL"): (0.0, 10000.0),
    }
    sent, sender = _intent_sink()
    rebalancer = BasketRebalancer(
        configs=configs,
        position_provider=_position_provider(positions),
        price_provider=_price_provider({"ETH": 3000.0, "SOL": 150.0}),
        intent_sender=sender,
        cfg=RebalanceConfig(imbalance_pct_threshold=0.18),
    )
    records = await rebalancer.rebalance_cycle()
    assert len(records) == 0
    assert len(sent) == 0


@pytest.mark.asyncio
async def test_rebalance_cycle_triggers_on_large_imbalance():
    configs = _make_basket(eth_target=0.5)
    positions = {
        ("basket_a", "ETH"): (1.5, 10000.0),
        ("basket_a", "SOL"): (0.0, 10000.0),
        ("basket_b", "ETH"): (-0.5, 10000.0),
        ("basket_b", "SOL"): (0.0, 10000.0),
    }
    sent, sender = _intent_sink()
    rebalancer = BasketRebalancer(
        configs=configs,
        position_provider=_position_provider(positions),
        price_provider=_price_provider({"ETH": 3000.0, "SOL": 150.0}),
        intent_sender=sender,
        cfg=RebalanceConfig(imbalance_pct_threshold=0.18),
    )
    records = await rebalancer.rebalance_cycle()
    assert len(records) >= 1
    eth_record = [r for r in records if r.coin == "ETH" and r.account_id == "basket_a"][0]
    assert eth_record.pre_position == 1.5
    assert eth_record.q_target == 0.5
    assert eth_record.hedge_size == pytest.approx(1.0)
    assert eth_record.hedge_side == "sell"
    assert eth_record.trigger == "subaccount_imbalance"
    assert len(sent) >= 1
    assert sent[0].target_inventory == 0.5
    assert sent[0].strategy_hint == "passive_aggressive"


@pytest.mark.asyncio
async def test_rebalance_cycle_shadow_mode_logs_but_does_not_send():
    configs = _make_basket(eth_target=0.5)
    positions = {
        ("basket_a", "ETH"): (1.5, 10000.0),
        ("basket_a", "SOL"): (0.0, 10000.0),
        ("basket_b", "ETH"): (-0.5, 10000.0),
        ("basket_b", "SOL"): (0.0, 10000.0),
    }
    sent, sender = _intent_sink()
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        log_path = f.name
    rebalancer = BasketRebalancer(
        configs=configs,
        position_provider=_position_provider(positions),
        price_provider=_price_provider({"ETH": 3000.0, "SOL": 150.0}),
        intent_sender=sender,
        cfg=RebalanceConfig(imbalance_pct_threshold=0.18),
        decision_log_path=log_path,
        shadow_mode=True,
    )
    try:
        records = await rebalancer.rebalance_cycle()
        assert len(records) >= 1
        assert len(sent) == 0
        with open(log_path) as f:
            lines = f.readlines()
        assert len(lines) >= 1
        logged = json.loads(lines[0])
        assert logged["coin"] == "ETH"
        assert logged["account_id"] == "basket_a"
    finally:
        await rebalancer.stop()
        Path(log_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_portfolio_net_drift_logged_as_warning():
    configs = _make_basket(eth_target=0.5)
    positions = {
        ("basket_a", "ETH"): (0.5, 10000.0),
        ("basket_a", "SOL"): (0.0, 10000.0),
        ("basket_b", "ETH"): (-0.3, 10000.0),
        ("basket_b", "SOL"): (0.0, 10000.0),
    }
    sent, sender = _intent_sink()
    rebalancer = BasketRebalancer(
        configs=configs,
        position_provider=_position_provider(positions),
        price_provider=_price_provider({"ETH": 3000.0, "SOL": 150.0}),
        intent_sender=sender,
        cfg=RebalanceConfig(imbalance_pct_threshold=0.50, portfolio_net_threshold=0.05),
    )
    records = await rebalancer.rebalance_cycle()
    assert len(records) == 0
    net = await rebalancer.compute_portfolio_net()
    assert abs(net["ETH"]) == pytest.approx(0.2)
