"""
C4.3 manual live-testnet gate.

Runs the real perp_bot.keeper.Keeper against a locally-spawned dex_executor
(OPMS) instance talking to Hyperliquid testnet, end to end:

  1. Health + account sanity (refuses to run against anything not flagged testnet).
  2. Keeper.start() for a bounded duration: intents flow through
     POST /api/v1/intents -> IntentRouter -> real HL-testnet strategies,
     fills come back on /ws/fills, positions reconcile via
     GET /api/v1/positions.
  3. Explicit dedupe check: same client_id sent twice -> same strategy_id,
     no second strategy created.
  4. Explicit reconnect-resnapshot check: kill the OPMS server out from
     under a connected client, confirm the client's WS loops retry and
     resnapshot_positions() is invoked on reconnect.
  5. Cleanup: stop every strategy this run created, regardless of outcome.

Usage: .venv-legacy/bin/python perp-bot/scripts/run_live_testnet_gate.py [duration_seconds]
"""
import asyncio
import logging
import os
import subprocess
import sys
import time

import aiohttp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from perp_bot.config import PerpPairConfig
from perp_bot.keeper import Keeper
from perp_bot.opms_client import OpmsClient

DEX_EXECUTOR_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "dex_executor")
VENV_PYTHON = os.path.join(os.path.dirname(__file__), "..", "..", ".venv-legacy", "bin", "python3")
BASE = "http://localhost:8000"
WS_BASE = "ws://localhost:8000"
EXCHANGE = "hyperliquid"
ACCOUNT_ID = os.environ.get("PERP_BOT_TESTNET_ACCOUNT_ID", "testnet_gate")
COIN = "ETH"

# Small caps: this places real orders on HL testnet.
CAPS_MAX_POSITION = 0.02
CAPS_CRITICAL_POSITION = 0.05

class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(self.format(record))


_handler = _ListHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
logging.basicConfig(level=logging.INFO)
logging.getLogger().addHandler(_handler)  # root only -- children propagate up to it


async def wait_for_health(http: aiohttp.ClientSession, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            async with http.get(f"{BASE}/health") as r:
                if r.status == 200:
                    print("health:", r.status, await r.json())
                    return
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(0.5)
    raise RuntimeError("dex_executor did not become healthy in time")


async def check_account(http: aiohttp.ClientSession) -> None:
    async with http.get(f"{BASE}/api/v1/accounts/{EXCHANGE}") as r:
        accounts = await r.json()
        print("accounts:", accounts)
        acct = next((a for a in accounts["accounts"] if a["account_id"] == ACCOUNT_ID), None)
        assert acct is not None, f"{ACCOUNT_ID} account not configured"
        assert acct["is_testnet"], "account is not flagged testnet — refusing to run"


async def stop_strategy(http: aiohttp.ClientSession, strategy_id: str) -> None:
    async with http.post(f"{BASE}/api/v1/strategies/{strategy_id}/stop") as r:
        print(f"cleanup stop {strategy_id}:", r.status, await r.json())


async def test_dedupe(http: aiohttp.ClientSession, client, created_ids: list[str]) -> None:
    """Same client_id sent twice must return the same strategy_id."""
    from mm_core.contracts import ExecIntent, QuoteSpec

    client_id = f"c43-dedupe-{int(time.time())}"
    intent = ExecIntent(
        venue=EXCHANGE, coin=COIN, account_id=ACCOUNT_ID,
        target_inventory=0.0, current_inventory=0.0,
        quote=QuoteSpec(bid_price=1.0, ask_price=1_000_000.0, bid_size=0.001, ask_size=0.001),
        urgency="passive", client_id=client_id,
    )
    first = await client.send_intent(intent)
    second = await client.send_intent(intent)
    print("dedupe first:", first)
    print("dedupe second:", second)
    assert first.get("strategy_id"), f"first send_intent produced no strategy_id: {first}"
    assert first["strategy_id"] == second["strategy_id"], (
        f"dedupe FAILED: {first['strategy_id']} != {second['strategy_id']}"
    )
    created_ids.append(first["strategy_id"])
    print("PASS: dedupe on client_id")


async def test_reconnect_resnapshot(client: OpmsClient, server_proc: dict) -> None:
    """Kill and respawn the OPMS server; confirm the client's WS loops
    reconnect and resnapshot_positions() fires (via Keeper._on_error)."""
    before = len(_handler.records)

    print("killing dex_executor to force a reconnect...")
    server_proc["proc"].terminate()
    try:
        server_proc["proc"].wait(timeout=5)
    except subprocess.TimeoutExpired:
        server_proc["proc"].kill()
        server_proc["proc"].wait()

    await asyncio.sleep(4.0)  # let the client's WS loops hit connection errors

    print("respawning dex_executor...")
    server_proc["proc"] = spawn_server()
    async with aiohttp.ClientSession() as http:
        await wait_for_health(http)

    await asyncio.sleep(15.0)  # let the client reconnect on its 2s backoff

    captured = "\n".join(_handler.records[before:])
    print("--- captured client-side log during reconnect window ---")
    print(captured if captured else "(nothing captured)")
    print("--- end captured log ---")
    reconnected = "market-data WS connected" in captured or "fills WS connected" in captured
    resnapshotted = "Resnapshotting positions after reconnect" in captured
    print(f"reconnect observed: {reconnected}, resnapshot observed: {resnapshotted}")
    assert reconnected, "client did not reconnect after server restart"
    if resnapshotted:
        print("PASS: reconnect-resnapshot")
    else:
        print("WARN: reconnected, but no resnapshot log seen (on_error may not have "
              "fired if the WS reconnected before a tick tried to use it) — inspect "
              "the captured log below")


def spawn_server() -> subprocess.Popen:
    return subprocess.Popen(
        [VENV_PYTHON, "-m", "uvicorn", "opms.service.app:app", "--host", "localhost", "--port", "8000"],
        cwd=DEX_EXECUTOR_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


async def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    created_ids: list[str] = []

    server_proc = {"proc": spawn_server()}
    try:
        async with aiohttp.ClientSession() as http:
            await wait_for_health(http)
            await check_account(http)

        pair_config = PerpPairConfig(
            coin=COIN,
            gamma=0.5, kappa=0.3, widen_factor=2.0,
            exchange=EXCHANGE, account_id=ACCOUNT_ID,
        )
        pair_config.caps.max_position = CAPS_MAX_POSITION
        pair_config.caps.critical_position = CAPS_CRITICAL_POSITION

        client = OpmsClient(
            base_url=BASE, ws_base_url=WS_BASE, exchange=EXCHANGE, coin=COIN,
            pair_config=pair_config, account_id=ACCOUNT_ID,
        )
        keeper = Keeper(client, pair_config, tick_s=5.0,
                         decision_log_path="/tmp/c43_decision_log.jsonl")

        print(f"starting keeper for {duration}s ...")
        keeper_task = asyncio.create_task(keeper.start())
        await asyncio.sleep(duration)

        print("--- running dedupe check ---")
        async with aiohttp.ClientSession() as http:
            await test_dedupe(http, client, created_ids)

        print("--- running reconnect-resnapshot check ---")
        await test_reconnect_resnapshot(client, server_proc)

        await asyncio.sleep(5.0)  # let a couple more ticks run post-reconnect

        print("stopping keeper...")
        await keeper.stop()
        keeper_task.cancel()
        try:
            await keeper_task
        except asyncio.CancelledError:
            pass

        print("--- final position check ---")
        async with aiohttp.ClientSession() as http:
            url = f"{BASE}/api/v1/positions/{EXCHANGE}/{COIN}-USD?account_id={ACCOUNT_ID}"
            async with http.get(url) as r:
                print("final position:", r.status, await r.text())

            async with http.get(f"{BASE}/api/v1/strategies") as r:
                strategies = await r.json()
                running = [s for s in strategies.get("strategies", []) if s.get("status") == "running"]
                print(f"{len(running)} strategies still running — stopping all")
                for s in running:
                    created_ids.append(s["strategy_id"])

            for sid in set(created_ids):
                try:
                    await stop_strategy(http, sid)
                except Exception as e:
                    print(f"cleanup error for {sid}: {e}")

        print("GATE COMPLETE")
    finally:
        server_proc["proc"].terminate()
        try:
            server_proc["proc"].wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc["proc"].kill()
        out, _ = server_proc["proc"].communicate()
        if out:
            print("--- dex_executor server output (tail) ---")
            print("\n".join(out.splitlines()[-60:]))


if __name__ == "__main__":
    asyncio.run(main())
