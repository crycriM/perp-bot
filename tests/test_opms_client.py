import asyncio

import aiohttp
import pytest
from unittest.mock import AsyncMock, MagicMock

from perp_bot.opms_client import OpmsClient, Position


def make_client():
    return OpmsClient(
        base_url="http://localhost:8080",
        ws_base_url="ws://localhost:8080",
        exchange="hyperliquid",
        coin="BTC",
        api_key="test_key",
        pair_config=MagicMock(),
        account_id="default",
    )


def json_response(data, status=200):
    resp = MagicMock()
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    resp.status = status
    resp.json = AsyncMock(return_value=data)
    return resp


@pytest.mark.asyncio
async def test_get_positions_hits_real_endpoints_and_merges_equity():
    client = make_client()

    position_resp = json_response({
        "venue": "hyperliquid", "coin": "BTC-USD", "side": "long",
        "quantity": "1.5", "entry_price": "50000", "unrealized_pnl": "10",
    })
    equity_resp = json_response({"exchange": "hyperliquid", "account_id": "default", "equity": "1000.0"})

    calls = []
    def fake_get(url):
        calls.append(url)
        return equity_resp if "equity" in url else position_resp

    mock_session = AsyncMock()
    mock_session.closed = False
    mock_session.get = fake_get
    client._session = mock_session

    positions = await client.get_positions()

    assert calls[0] == "http://localhost:8080/api/v1/positions/hyperliquid/BTC-USD?account_id=default"
    assert calls[1] == "http://localhost:8080/api/v1/accounts/hyperliquid/default/equity"
    assert "BTC" in positions
    assert positions["BTC"].position == pytest.approx(1.5)
    assert positions["BTC"].equity == pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_get_positions_short_side_is_negative():
    client = make_client()
    mock_session = AsyncMock()
    mock_session.closed = False
    mock_session.get = lambda url: (
        json_response({"exchange": "hyperliquid", "account_id": "default", "equity": "500.0"})
        if "equity" in url else
        json_response({"side": "short", "quantity": "2.0", "entry_price": "1", "unrealized_pnl": "0"})
    )
    client._session = mock_session

    positions = await client.get_positions()
    assert positions["BTC"].position == pytest.approx(-2.0)


@pytest.mark.asyncio
async def test_get_positions_flat_on_404():
    """No open position -> flat, not an error."""
    client = make_client()
    mock_session = AsyncMock()
    mock_session.closed = False
    mock_session.get = lambda url: json_response({}, status=404)
    client._session = mock_session

    positions = await client.get_positions()
    assert positions == {}


@pytest.mark.asyncio
async def test_resnapshot_positions_calls_get_positions():
    client = make_client()
    mock_session = AsyncMock()
    mock_session.closed = False
    mock_session.get = lambda url: (
        json_response({"equity": "1000.0"}) if "equity" in url else
        json_response({"side": "long", "quantity": "0.0", "entry_price": "0", "unrealized_pnl": "0"})
    )
    client._session = mock_session

    await client.resnapshot_positions()
    assert "BTC" in client._positions


@pytest.mark.asyncio
async def test_send_intent_serializes_exec_intent():
    """send_intent must POST an ExecIntent as a dict to /api/v1/intents
    with a client_id (redelivery must not double-start)."""
    from mm_core.contracts import ExecIntent, QuoteSpec

    client = make_client()

    captured = {}
    resp_mock = json_response({"status": "ok"})

    def fake_post(url, json=None):
        captured["url"] = url
        captured["payload"] = json
        return resp_mock

    mock_session = AsyncMock()
    mock_session.closed = False
    mock_session.post = fake_post
    client._session = mock_session

    intent = ExecIntent(
        venue="hl", coin="BTC",
        target_inventory=0.0, current_inventory=2.0,
        quote=QuoteSpec(bid_price=1.0, ask_price=2.0, bid_size=1.0, ask_size=1.0),
        urgency="normal",
    )
    await client.send_intent(intent)

    assert captured["url"].endswith("/api/v1/intents")
    p = captured["payload"]
    assert p["venue"] == "hl" and p["coin"] == "BTC"
    assert p["current_inventory"] == 2.0
    assert p["quote"]["bid_price"] == 1.0
    assert p["client_id"], "client_id must be generated when missing"


@pytest.mark.asyncio
async def test_md_ws_loop_dispatches_market_data_only():
    client = make_client()
    received = []
    async def on_snapshot(data):
        received.append(data)
    client.on_snapshot(on_snapshot)

    msg_data = MagicMock(type=aiohttp.WSMsgType.TEXT)
    msg_data.json.return_value = {"type": "market_data", "data": {"mid": 100.0, "ts": 1.0}}
    msg_other = MagicMock(type=aiohttp.WSMsgType.TEXT)
    msg_other.json.return_value = {"type": "something_else", "data": {}}
    msg_close = MagicMock(type=aiohttp.WSMsgType.CLOSED)

    ws_cm = MagicMock()
    ws_cm.__aenter__ = AsyncMock(return_value=_AsyncIter([msg_data, msg_other, msg_close]))
    ws_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.closed = False
    mock_session.ws_connect = lambda url: ws_cm
    client._session = mock_session

    task = asyncio.create_task(client._md_ws_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert received == [{"mid": 100.0, "ts": 1.0}]


class _AsyncIter:
    """Minimal async-iterable stand-in for an aiohttp ClientWebSocketResponse."""
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)
