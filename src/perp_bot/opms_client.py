import asyncio
import logging
import time
from dataclasses import dataclass, field

import aiohttp

logger = logging.getLogger(__name__)

@dataclass
class Intent:
    id: str
    target_inventory: float
    quote: dict | None = None
    urgency: str = "normal"

@dataclass
class Position:
    coin: str
    position: float
    equity: float

class OpmsClient:
    """REST + WS client for the OPMS with reconnect and resnapshot-on-reconnect."""

    def __init__(self, base_url: str, ws_url: str, api_key: str, pair_config):
        self.base_url = base_url
        self.ws_url = ws_url
        self.api_key = api_key
        self.pair_config = pair_config
        self._session: aiohttp.ClientSession | None = None
        self._ws = None
        self._task: asyncio.Task | None = None
        self._on_snapshot_cb = None
        self._on_fill_cb = None
        self._on_error_cb = None
        self._positions: dict[str, Position] = {}
        self._intents: dict[str, Intent] = {}
        self._snapshots: list = []
        self._fills: list = []

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.api_key}"}
            )
        return self._session

    async def send_intent(self, intent: Intent) -> dict:
        session = await self._get_session()
        payload = {
            "id": intent.id,
            "target_inventory": intent.target_inventory,
            "quote": intent.quote,
            "urgency": intent.urgency,
        }
        async with session.post(f"{self.base_url}/api/intents", json=payload) as resp:
            return await resp.json()

    async def get_positions(self) -> dict[str, Position]:
        session = await self._get_session()
        async with session.get(f"{self.base_url}/api/positions") as resp:
            data = await resp.json()
            self._positions = {
                p["coin"]: Position(coin=p["coin"], position=p["position"], equity=p["equity"])
                for p in data
            }
            return self._positions

    async def get_intent(self, intent_id: str) -> Intent:
        session = await self._get_session()
        async with session.get(f"{self.base_url}/api/intents/{intent_id}") as resp:
            data = await resp.json()
            return Intent(
                id=data["id"],
                target_inventory=data["target_inventory"],
                quote=data.get("quote"),
                urgency=data.get("urgency", "normal"),
            )

    def on_snapshot(self, callback):
        self._on_snapshot_cb = callback

    def on_fill(self, callback):
        self._on_fill_cb = callback

    def on_error(self, callback):
        self._on_error_cb = callback

    async def _ws_loop(self):
        session = await self._get_session()
        while True:
            try:
                logger.info("Connecting to OPMS WS")
                async with session.ws_connect(self.ws_url) as ws:
                    self._ws = ws
                    logger.info("OPMS WS connected")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = msg.json()
                            if data.get("type") == "market_data":
                                self._snapshots.append(data)
                                if self._on_snapshot_cb:
                                    await self._on_snapshot_cb(data)
                            elif data.get("type") == "fill":
                                self._fills.append(data)
                                if self._on_fill_cb:
                                    await self._on_fill_cb(data)
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"WS error: {e}, reconnecting")
                if self._on_error_cb:
                    await self._on_error_cb(e)
            await asyncio.sleep(2)

    async def start(self):
        self._task = asyncio.create_task(self._ws_loop())
        return self

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def resnapshot_positions(self):
        logger.info("Resnapshotting positions after reconnect")
        await self.get_positions()
