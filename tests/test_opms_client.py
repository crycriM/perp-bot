import asyncio
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from perp_bot.opms_client import OpmsClient, Position

@pytest.mark.asyncio
async def test_client_reconnect_resnapshot():
    """Test that the client resnapshots positions after a WS reconnect."""
    pair_config = MagicMock()
    base_url = "http://localhost:8080"
    ws_url = "ws://localhost:8080/ws"
    api_key = "test_key"

    client = OpmsClient(base_url=base_url, ws_url=ws_url, api_key=api_key, pair_config=pair_config)

    mock_session = AsyncMock()
    mock_session.closed = False

    resp_mock = MagicMock()
    resp_mock.__aenter__ = AsyncMock(return_value=resp_mock)
    resp_mock.__aexit__ = AsyncMock(return_value=False)
    resp_mock.json = AsyncMock(return_value=[{"coin": "BTC", "position": 0.0, "equity": 1000.0}])

    mock_session.get = lambda url: resp_mock

    client._session = mock_session

    positions = await client.get_positions()
    assert "BTC" in positions
    assert positions["BTC"].equity == 1000.0

    await client.resnapshot_positions()
    assert "BTC" in client._positions
