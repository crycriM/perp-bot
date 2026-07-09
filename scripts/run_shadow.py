"""C4.2: run the live keeper in shadow mode on OPMS testnet data.

Consumes live market data + fills from OPMS, evaluates the keeper normally,
logs each decision/intention to JSONL, but never submits intents back to OPMS.
"""

import argparse
import asyncio
import logging
from pathlib import Path

from mm_core.inventory import Caps

from perp_bot.config import PerpPairConfig
from perp_bot.keeper import Keeper
from perp_bot.opms_client import OpmsClient
from perp_bot.topology import validate_account_topology


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("coin")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--ws-base-url", default="ws://127.0.0.1:8000")
    ap.add_argument("--exchange", default="hyperliquid")
    ap.add_argument("--account-id", default="test")
    ap.add_argument("--gamma", type=float, default=0.25)
    ap.add_argument("--kappa", type=float, default=0.5)
    ap.add_argument("--tick-s", type=float, default=5.0)
    ap.add_argument("--duration-s", type=float, default=900.0)
    ap.add_argument("--max-position", type=float, default=0.1)
    ap.add_argument("--critical-position", type=float, default=0.2)
    ap.add_argument("--decision-log-path", type=Path, default=Path("shadow-decisions.jsonl"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)

    config = PerpPairConfig(
        coin=args.coin,
        exchange=args.exchange,
        account_id=args.account_id,
        gamma=args.gamma,
        kappa=args.kappa,
        caps=Caps(
            max_position=args.max_position,
            critical_position=args.critical_position,
        ),
    )
    validate_account_topology([config])

    client = OpmsClient(
        base_url=args.base_url,
        ws_base_url=args.ws_base_url,
        exchange=config.exchange,
        coin=config.coin,
        api_key="",
        pair_config=config,
        account_id=config.account_id,
    )
    keeper = Keeper(
        client,
        config,
        tick_s=args.tick_s,
        decision_log_path=str(args.decision_log_path),
        shadow_mode=True,
    )

    try:
        await asyncio.wait_for(keeper.start(), timeout=args.duration_s)
    except asyncio.TimeoutError:
        logging.info("Shadow run reached duration limit; stopping keeper")
    finally:
        await keeper.stop()


if __name__ == "__main__":
    asyncio.run(main())
