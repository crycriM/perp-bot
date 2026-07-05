import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from mm_core.contracts import ExecIntent, MarketSnapshot
from mm_core.inventory import Caps, PerpInventory
from mm_core.risk_policy import Decision

from perp_bot.config import PerpPairConfig
from perp_bot.keeper import Keeper, DecisionRecord
from perp_bot.opms_client import OpmsClient

class FakeOpmsClient:
    """Fake OPMS client with canned snapshots for keeper testing."""

    def __init__(self, snapshots=None):
        self.snapshots = snapshots or []
        self.sends: list[ExecIntent] = []
        self._on_snapshot_cb = None
        self._on_fill_cb = None
        self._on_error_cb = None

    def on_snapshot(self, cb):
        self._on_snapshot_cb = cb

    def on_fill(self, cb):
        self._on_fill_cb = cb

    def on_error(self, cb):
        self._on_error_cb = cb

    async def start(self):
        for snap in self.snapshots:
            if self._on_snapshot_cb:
                await self._on_snapshot_cb(snap)
        return self

    async def stop(self):
        pass

    async def send_intent(self, intent):
        self.sends.append(intent)

    async def get_positions(self):
        from perp_bot.opms_client import Position
        return {
            "BTC": Position(coin="BTC", position=0.0, equity=1000.0),
        }

    async def resnapshot_positions(self):
        pass

@pytest.mark.asyncio
async def test_keeper_normal_quote():
    config = PerpPairConfig(coin="BTC", gamma=1.0, kappa=0.5)
    t0 = time.time()
    snapshots = [
        {"ts": t0 + i, "mid": 50000.0 + i * 10, "funding_rate": None}
        for i in range(20)
    ]
    client = FakeOpmsClient(snapshots)
    keeper = Keeper(client, config, tick_s=0.01)
    # Wire up callbacks manually (bypassing Keeper.start which does the loop)
    client.on_snapshot(keeper._on_snapshot)
    client.on_fill(keeper._on_fill)
    client.on_error(keeper._on_error)
    await client.start()

    for _ in range(5):
        await keeper._tick()

    assert keeper._equity > 0
    assert keeper._inventory.position == 0

@pytest.mark.asyncio
async def test_keeper_de_risk():
    config = PerpPairConfig(coin="BTC", gamma=1.0, kappa=0.5)
    config.caps = Caps(max_position=5.0, critical_position=8.0)
    t0 = time.time()
    snapshots = [
        {"ts": t0 + i, "mid": 50000.0, "funding_rate": None}
        for i in range(20)
    ]
    client = FakeOpmsClient(snapshots)
    keeper = Keeper(client, config, tick_s=0.01)
    client.on_snapshot(keeper._on_snapshot)
    client.on_fill(keeper._on_fill)
    client.on_error(keeper._on_error)
    await client.start()

    keeper._inventory.position = 9.0

    for _ in range(3):
        await keeper._tick()

    assert any(isinstance(s, ExecIntent) and s.target_inventory == 0.0 for s in client.sends)

@pytest.mark.asyncio
async def test_keeper_emergency_exit():
    config = PerpPairConfig(coin="BTC", gamma=1.0, kappa=0.5)
    t0 = time.time()
    snapshots = [
        {"ts": t0 + i, "mid": 50000.0, "funding_rate": None}
        for i in range(20)
    ]
    client = FakeOpmsClient(snapshots)
    keeper = Keeper(client, config, tick_s=0.01)
    client.on_snapshot(keeper._on_snapshot)
    client.on_fill(keeper._on_fill)
    client.on_error(keeper._on_error)
    await client.start()

    keeper._equity = 1000.0
    keeper._risk._peak_equity = 10000.0

    for _ in range(3):
        await keeper._tick()

    assert any(isinstance(s, ExecIntent) and s.urgency == "emergency" for s in client.sends)
