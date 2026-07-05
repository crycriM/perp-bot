import asyncio
import time

import pytest

from mm_core.contracts import MarketSnapshot
from mm_core.inventory import Caps

from perp_bot.config import PerpPairConfig
from perp_bot.backtest import Backtest, Backtrade, Strategy

@pytest.mark.asyncio
async def test_backtest_basic():
    config = PerpPairConfig(coin="BTC", gamma=1.0, kappa=0.5)
    config.caps = Caps(max_position=10.0, critical_position=20.0)

    bt = Backtest(config, start_equity=10000.0)
    bt.set_strategy(Strategy(config))

    t0 = time.time()
    for i in range(50):
        bt.add_snapshot(
            MarketSnapshot(venue="hl", coin="BTC", ts=t0 + i, mid=50000.0 + i * 10)
        )

    bt.load_trades([
        Backtrade(ts=t0 + 5, side="buy", price=50000.0, size=1.0),
        Backtrade(ts=t0 + 10, side="sell", price=50100.0, size=1.0),
    ])
    bt.load_funding([])

    history = await bt.run(duration_s=60.0)

    assert len(history) > 0
    assert history[0]["equity"] > 0
