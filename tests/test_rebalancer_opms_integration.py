"""End-to-end integration test: BasketRebalancer (execute mode) → native OPMS.

Closes the deployment-plan §7 gap "the rebalancer's OPMS intent path
(execution) is still untested": a drifted basket position drives a real
`ExecIntent` through `perp_bot.OpmsClient.send_intent` over real HTTP into
the native OPMS (`opms.service.app`, uvicorn in-process) with the mock
adapter, and the intent router must land a correctly sized, running
`passive_aggressive_v2` strategy on the right account.

Runs in dex_executor's own venv (fastapi + uvicorn + opms installed);
skips in environments without the OPMS service stack.
"""

import asyncio
import os
import socket
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

# The OPMS database module resolves DATABASE_URL at import time; the default
# points at /app/data (a container path). Point it at a throwaway file
# BEFORE anything imports opms.service — including importorskip below.
if not os.environ.get("DATABASE_URL", "").startswith("sqlite:///"):
    _TMP_DIR = tempfile.mkdtemp(prefix="opms_rebal_it_")
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DIR}/opms.db"

pytest.importorskip("opms.service.app")

from mm_core.inventory import Caps  # noqa: E402

from perp_bot.config import PerpPairConfig  # noqa: E402
from perp_bot.opms_client import OpmsClient  # noqa: E402
from perp_bot.rebalancer import BasketRebalancer, RebalanceConfig  # noqa: E402


@pytest_asyncio.fixture
async def opms_url():
    """Serve the real OPMS app with uvicorn in-process; yield its base URL."""
    import aiohttp
    import uvicorn
    from opms.service.app import app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    server.install_signal_handlers = lambda: None  # pytest owns the loop's signals
    task = asyncio.create_task(server.serve())

    base = f"http://127.0.0.1:{port}"
    async with aiohttp.ClientSession() as session:
        for _ in range(100):  # 10 s worst case
            try:
                async with session.get(f"{base}/health") as resp:
                    if resp.status == 200:
                        break
            except aiohttp.ClientError:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("OPMS did not come up within 10 s")

    yield base

    server.should_exit = True
    await asyncio.wait_for(task, 10)


def _drifted_configs() -> list[PerpPairConfig]:
    """One subaccount of the basket: ETH tilted long +0.4 (currently flat —
    drifted), SOL tilted short −4.0 (on target)."""
    return [
        PerpPairConfig(
            coin="ETH", exchange="mock", account_id="mm-a",
            target_inventory=0.4, leverage=3,
            caps=Caps(max_position=1.0, critical_position=2.0),
        ),
        PerpPairConfig(
            coin="SOL", exchange="mock", account_id="mm-a",
            target_inventory=-4.0, leverage=3,
            caps=Caps(max_position=10.0, critical_position=20.0),
        ),
    ]


@pytest.mark.asyncio
async def test_rebalance_correction_flows_to_opms(opms_url):
    configs = _drifted_configs()

    async def position_provider(account_id: str, coin: str) -> tuple[float, float]:
        # ETH flat vs target +0.4 (drift), SOL on target; equity $2000 so the
        # §1.4 capacity guard (2.5x equity) passes for the $1200 correction.
        return (0.0, 2000.0) if coin == "ETH" else (-4.0, 2000.0)

    async def price_provider(coin: str) -> float:
        return 3000.0 if coin == "ETH" else 150.0

    client = OpmsClient(
        base_url=opms_url, ws_base_url=opms_url.replace("http", "ws"),
        exchange="mock", coin="ETH", api_key="",
        pair_config=configs[0], account_id="mm-a",
    )
    sent: list[dict] = []

    async def intent_sender(intent):
        result = await client.send_intent(intent)
        sent.append(result)
        return result

    rb = BasketRebalancer(
        configs=configs,
        position_provider=position_provider,
        price_provider=price_provider,
        intent_sender=intent_sender,
        cfg=RebalanceConfig(),
        shadow_mode=False,  # execute: this is the path under test
    )

    records = await rb.rebalance_cycle()
    await rb.stop()
    await client.stop()

    # --- rebalancer side: exactly one correction, ETH only --------------
    corrections = [r for r in records if r.trigger == "subaccount_imbalance"]
    assert len(corrections) == 1, records
    rec = corrections[0]
    assert rec.coin == "ETH" and rec.account_id == "mm-a"
    assert rec.hedge_side == "buy" and rec.hedge_size == pytest.approx(0.4)

    # --- OPMS side: one running PA-V2 strategy, sized and routed right ----
    assert len(sent) == 1, sent
    strategy = sent[0]
    assert strategy["algorithm_type"] == "passive_aggressive_v2"
    assert strategy["exchange_type"] == "mock"
    assert strategy["account_id"] == "mm-a"
    assert strategy["symbol"] == "ETH"
    assert strategy["side"] == "buy"
    assert float(strategy["total_quantity"]) == pytest.approx(0.4)
    assert strategy["status"] == "running"
    assert strategy["strategy_kind"] == "execution"

    # ...and it survives as queryable state, not a fire-and-forget POST.
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{opms_url}/api/v1/strategies/{strategy['strategy_id']}") as resp:
            assert resp.status == 200
            persisted = await resp.json()
    assert persisted["strategy_id"] == strategy["strategy_id"]
    assert persisted["status"] == "running"


@pytest.mark.asyncio
async def test_rebalance_intent_is_idempotent_on_client_id(opms_url):
    """The router dedupes on client_id — a rebalancer retry (e.g. the cycle
    loop re-ran before positions updated) must not stack a second strategy."""
    configs = _drifted_configs()

    async def position_provider(account_id: str, coin: str) -> tuple[float, float]:
        return (0.0, 2000.0) if coin == "ETH" else (-4.0, 2000.0)

    async def price_provider(coin: str) -> float:
        return 3000.0 if coin == "ETH" else 150.0

    client = OpmsClient(
        base_url=opms_url, ws_base_url=opms_url.replace("http", "ws"),
        exchange="mock", coin="ETH", api_key="",
        pair_config=configs[0], account_id="mm-a",
    )
    captured: list = []

    async def intent_sender(intent):
        captured.append(intent)
        return await client.send_intent(intent)

    rb = BasketRebalancer(
        configs=configs,
        position_provider=position_provider,
        price_provider=price_provider,
        intent_sender=intent_sender,
        cfg=RebalanceConfig(),
        shadow_mode=False,
    )

    await rb.rebalance_cycle()
    assert captured, "cycle produced no intent"
    first_strategy_id = (await _fetch_strategy(opms_url, client)).strategy_id

    # Simulate the retry: the exact same ExecIntent POSTed again.
    duplicate = await client.send_intent(captured[0])
    await rb.stop()
    await client.stop()

    assert duplicate["strategy_id"] == first_strategy_id, \
        "dedupe must return the existing strategy, not create one"

    # And the persisted state still holds exactly one ETH move.
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{opms_url}/api/v1/strategies?limit=100") as resp:
            data = await resp.json()
    strategies = data.get("strategies", data)
    eth_moves = [
        s for s in strategies
        if s.get("symbol") == "ETH" and s.get("algorithm_type") == "passive_aggressive_v2"
    ]
    assert len(eth_moves) == 1, f"dedupe failed: {eth_moves}"
    assert eth_moves[0]["strategy_id"] == first_strategy_id


class _Record(dict):
    """dict with attribute access for strategy responses."""

    def __getattr__(self, name):
        return self[name]


async def _fetch_strategy(opms_url: str, client: OpmsClient) -> _Record:
    """Fetch the persisted strategy the rebalancer's first correction created."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{opms_url}/api/v1/strategies?limit=100") as resp:
            assert resp.status == 200
            data = await resp.json()
    strategies = data.get("strategies", data)
    moves = [
        s for s in strategies
        if s.get("symbol") == "ETH" and s.get("algorithm_type") == "passive_aggressive_v2"
    ]
    assert len(moves) == 1, strategies
    return _Record(moves[0])
