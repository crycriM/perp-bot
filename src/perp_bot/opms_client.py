import asyncio
import dataclasses
import logging
import math
import uuid
from dataclasses import dataclass

import aiohttp

from perp_bot.margin_health import fail_closed_margin_available

logger = logging.getLogger(__name__)

@dataclass
class Position:
    coin: str
    position: float
    equity: float
    # Venue-computed liquidation-distance balance (HL unified accounts:
    # spotClearinghouseState.tokenToAvailableAfterMaintenance = spot total −
    # crossMaintenanceMarginUsed). Invalid/missing readings fail closed to
    # zero so the risk policy requests an emergency exit.
    margin_available: float | None = None

    def __post_init__(self) -> None:
        self.margin_available = fail_closed_margin_available(
            self.margin_available,
            source=f"position snapshot for {self.coin}",
        )

class OpmsClient:
    """REST + WS client for the OPMS with reconnect and resnapshot-on-reconnect.

    Talks to the real Stream B4 surface: GET /positions/{exchange}/{symbol},
    GET /accounts/{exchange}/{account_id}/equity, and the two egress
    websockets /ws/md/{exchange}/{symbol} (market data) and
    /ws/fills/{exchange} (fills, venue-scoped so filtered by symbol here).
    """

    def __init__(self, base_url: str, ws_base_url: str, exchange: str, coin: str,
                 pair_config, account_id: str = "default"):
        self.base_url = base_url
        self.ws_base_url = ws_base_url
        self.exchange = exchange
        self.coin = coin
        self.symbol = f"{coin}-USD"
        self.account_id = account_id

        self.pair_config = pair_config
        self._session: aiohttp.ClientSession | None = None
        self._md_task: asyncio.Task | None = None
        self._fills_task: asyncio.Task | None = None
        self._on_snapshot_cb = None
        self._on_fill_cb = None
        self._on_error_cb = None
        self._positions: dict[str, Position] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # aiohttp's default total timeout is 300 s; a hung OPMS must not stall a tick that long
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def send_intent(self, intent) -> dict:
        """POST an mm_core ExecIntent to the OPMS intent endpoint.

        Raises ValueError for a malformed intent (never sent) and RuntimeError on an HTTP error.
        """
        if errors := intent.validation_errors():
            raise ValueError(f"refusing to send invalid intent: {errors}")
        session = await self._get_session()
        payload = dataclasses.asdict(intent)
        if not payload.get("account_id"):
            payload["account_id"] = self.account_id
        if not payload.get("client_id"):
            payload["client_id"] = f"{intent.venue}:{intent.coin}:{uuid.uuid4().hex[:12]}"
        async with session.post(f"{self.base_url}/api/v1/intents", json=payload) as resp:
            body = await resp.json()
            if resp.status >= 400:
                raise RuntimeError(f"OPMS rejected intent ({resp.status}): {body}")
            return body

    async def _get_equity(self) -> float:
        session = await self._get_session()
        url = f"{self.base_url}/api/v1/accounts/{self.exchange}/{self.account_id}/equity"
        async with session.get(url) as resp:
            data = await resp.json()
            if resp.status >= 400:
                raise RuntimeError(f"equity request failed ({resp.status}): {data}")
            return float(data["equity"])

    async def get_positions(self) -> dict[str, Position]:
        """Authoritative single-symbol position + equity, keyed by coin."""
        session = await self._get_session()
        url = (f"{self.base_url}/api/v1/positions/{self.exchange}/{self.symbol}"
               f"?account_id={self.account_id}")
        async with session.get(url) as resp:
            if resp.status == 404:
                self._positions = {}
                return self._positions
            data = await resp.json()
            if resp.status >= 400:
                raise RuntimeError(f"position request failed ({resp.status}): {data}")

        equity = await self._get_equity()
        quantity = float(data["quantity"])
        # anything but exactly long/short would silently flip the sign of the position
        if data["side"] not in ("long", "short") or not math.isfinite(quantity) or quantity < 0:
            raise ValueError(f"malformed OPMS position: {data}")
        signed = quantity if data["side"] == "long" else -quantity
        self._positions = {self.coin: Position(
            coin=self.coin, position=signed, equity=equity,
            margin_available=data.get("margin_available"),
        )}
        return self._positions

    def on_snapshot(self, callback):
        self._on_snapshot_cb = callback

    def on_fill(self, callback):
        self._on_fill_cb = callback

    def on_error(self, callback):
        self._on_error_cb = callback

    async def _ws_loop(self, name: str, url: str, handle) -> None:
        """Reconnecting WS reader. Any error is logged and survived: an exception
        escaping here would end the task and freeze the feed for good."""
        session = await self._get_session()
        while True:
            try:
                logger.info(f"Connecting to OPMS {name} WS")
                async with session.ws_connect(url) as ws:
                    logger.info(f"OPMS {name} WS connected")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                await handle(msg.json())
                            except Exception:
                                logger.exception(f"Bad {name} WS message skipped")
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
            except Exception as e:
                logger.error(f"{name} WS error: {e!r}, reconnecting")
                if self._on_error_cb:
                    try:
                        await self._on_error_cb(e)
                    except Exception:
                        logger.exception("on_error callback failed")
            await asyncio.sleep(2)

    async def _md_ws_loop(self):
        url = (f"{self.ws_base_url}/ws/md/{self.exchange}/{self.symbol}"
               f"?account_id={self.account_id}")

        async def handle(data):
            if data.get("type") == "market_data" and self._on_snapshot_cb:
                await self._on_snapshot_cb(data.get("data", {}))

        await self._ws_loop("market-data", url, handle)

    async def _fills_ws_loop(self):
        url = f"{self.ws_base_url}/ws/fills/{self.exchange}?account_id={self.account_id}"

        async def handle(data):
            if data.get("type") != "fill":
                return
            fill = data.get("data", {})
            if fill.get("account_id") is None:
                logger.warning("Ignoring fill without account_id on fills WS")
                return
            if (fill.get("coin") == self.symbol and fill.get("account_id") == self.account_id
                    and self._on_fill_cb):
                await self._on_fill_cb({
                    "ts": float(fill.get("ts", 0.0)),
                    "side": fill.get("side"),
                    "price": float(fill.get("price", 0.0)),
                    "size": float(fill.get("size", 0.0)),
                    "fee": float(fill.get("fee", 0.0)),
                })

        await self._ws_loop("fills", url, handle)

    async def start(self):
        self._md_task = asyncio.create_task(self._md_ws_loop())
        self._fills_task = asyncio.create_task(self._fills_ws_loop())
        return self

    async def stop(self):
        for task in (self._md_task, self._fills_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._md_task = None
        self._fills_task = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def resnapshot_positions(self) -> dict[str, Position]:
        """Re-read authoritative positions (call after reconnect/drift)."""
        logger.info("Resnapshotting positions after reconnect")
        return await self.get_positions()
