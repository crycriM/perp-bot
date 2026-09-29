"""Replay must feed the risk policy a margin input like live does (conservatively)."""
import time

import pytest

from mm_core.inventory import Caps, PerpInventory
from mm_core.risk_policy import Decision

from perp_bot.backtest import Strategy
from perp_bot.config import PerpPairConfig
from perp_bot.margin_health import initial_margin_available


def test_initial_margin_available_is_equity_less_notional_over_leverage():
    assert initial_margin_available(equity=300.0, position=-1.5, mid=100.0, leverage=3.0) == pytest.approx(250.0)
    assert initial_margin_available(equity=100.0, position=0.0, mid=100.0, leverage=3.0) == pytest.approx(100.0)


def _decision(position, equity=100.0):
    config = PerpPairConfig(coin="ENA", gamma=.02, kappa=.4)
    config.caps = Caps(max_position=1e9, critical_position=1e9)  # margin, not inventory caps, must decide
    config.leverage = 3
    strategy = Strategy(config)
    t0 = time.time()
    history = [(t0 + i, 1.0) for i in range(60)]
    strategy(config, PerpInventory(position, config.caps), equity, history, t0 + 60, 1.0)
    return strategy.last_decision


def test_replay_ample_margin_does_not_trigger_margin_decisions():
    assert _decision(100.0) not in (Decision.DE_RISK, Decision.EMERGENCY_EXIT)  # 67% available


def test_replay_soft_margin_breach_derisks():
    assert _decision(250.0) is Decision.DE_RISK  # 16.7% available < 20%


def test_replay_hard_margin_breach_emergency_exits():
    assert _decision(-290.0) is Decision.EMERGENCY_EXIT  # 3.3% available < 10%, either side
