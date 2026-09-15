"""Shadow the basket rebalancer against real Hyperliquid positions and prices.

Closes the deployment plan §7 gap ("still needs live integration testing with
real position/price feeds"): builds the four basket instances
(`basket_config.py`), feeds the rebalancer live account positions and mids,
and runs `BasketRebalancer.rebalance_cycle()` in **shadow** mode — metrics are
computed and every decision is logged, nothing signs.

Read-only. `--execute` additionally submits ExecIntents to a running native
OPMS and requires `OPMS_REBALANCE_EXECUTE=confirm` in the environment.

Usage (shadow, live reads):
  python scripts/run_rebalancer_shadow.py --cycles 3
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

load_dotenv(REPO_ROOT / ".env")

from basket_config import get_basket_configs  # noqa: E402  (scripts-local)

from perp_bot.rebalancer import BasketRebalancer, RebalanceConfig  # noqa: E402
from perp_bot.rebalancer_feeds import hl_position_provider, hl_price_provider  # noqa: E402


def _account_address(account_id: str) -> str:
    address = os.environ.get(f"HYPERLIQUID_{account_id.upper()}_ACCOUNT_ADDRESS")
    if not address:
        raise SystemExit(f"Missing HYPERLIQUID_{account_id.upper()}_ACCOUNT_ADDRESS in .env")
    return address.lower()


def build_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cycles", type=int, default=1, help="rebalance cycles to run (0 = forever)")
    ap.add_argument("--interval-s", type=float, default=600.0, help="cycle interval (plan: 10 min)")
    ap.add_argument("--account-a", default="e2_mm1", help="live account mapped to basket_a")
    ap.add_argument("--account-b", default="e2_mm2", help="live account mapped to basket_b")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000", help="native OPMS (only with --execute)")
    ap.add_argument("--decision-log", default=None, help="JSONL path (default logs/rebalancer_shadow_<ts>.jsonl)")
    ap.add_argument("--execute", action="store_true", help="send ExecIntents to OPMS (needs confirm env)")
    return ap.parse_args(argv)


async def run(args) -> int:
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    mapping = {"basket_a": args.account_a, "basket_b": args.account_b}
    addresses = {live: _account_address(live) for live in mapping.values()}
    configs = [dataclasses.replace(cfg, account_id=mapping.get(cfg.account_id, cfg.account_id))
               for cfg in get_basket_configs()]

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    intent_sender = None
    shadow = True
    if args.execute:
        if os.environ.get("OPMS_REBALANCE_EXECUTE") != "confirm":
            raise SystemExit("refusing --execute: set OPMS_REBALANCE_EXECUTE=confirm")
        from perp_bot.opms_client import OpmsClient
        client = OpmsClient(base_url=args.base_url, ws_base_url=args.base_url.replace("http", "ws"),
                            exchange="hyperliquid", coin="ETH", api_key="",
                            pair_config=configs[0], account_id=args.account_a)
        intent_sender = client.send_intent
        shadow = False

    stamp = time.strftime("%Y%m%dT%H%M%S")
    log_path = args.decision_log or str(REPO_ROOT / "perp-bot" / "logs" / f"rebalancer_shadow_{stamp}.jsonl")
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)

    async def never_send(intent):
        raise RuntimeError("shadow run attempted to send an intent")

    rb = BasketRebalancer(
        configs=configs,
        position_provider=hl_position_provider(info, addresses),
        price_provider=hl_price_provider(info),
        intent_sender=intent_sender or never_send,
        cfg=RebalanceConfig(cycle_interval_s=args.interval_s),
        decision_log_path=log_path,
        shadow_mode=shadow,
    )

    print(f"accounts: basket_a -> {mapping['basket_a']} ({addresses[mapping['basket_a']][:8]}…)  "
          f"basket_b -> {mapping['basket_b']} ({addresses[mapping['basket_b']][:8]}…)")
    print(f"mode: {'SHADOW (no intents)' if shadow else 'EXECUTE via OPMS ' + args.base_url}; log={log_path}")

    cycle = 0
    while args.cycles == 0 or cycle < args.cycles:
        cycle += 1
        by_account = rb._account_configs()
        for account_id in by_account:
            imbalance, equity = await rb.compute_imbalance(account_id)
            pct = abs(imbalance) / equity if equity > 0 else 0.0
            positions = {}
            for cfg in by_account[account_id]:
                positions[cfg.coin] = await rb.position_provider(account_id, cfg.coin)
            print(f"[{cycle}] {account_id}: equity=${equity:.2f} imbalance=${imbalance:+.2f} "
                  f"({pct:.2%} vs {rb.cfg.imbalance_pct_threshold:.0%}) positions="
                  f"{ {c: round(p, 4) for c, (p, _) in positions.items()} }")
        net = await rb.compute_portfolio_net()
        print(f"[{cycle}] portfolio net: {{{', '.join(f'{c}: {n:+.4f}' for c, n in net.items())}}} "
              f"(threshold {rb.cfg.portfolio_net_threshold})")
        records = await rb.rebalance_cycle()
        print(f"[{cycle}] rebalance records: {len(records)}")
        if args.cycles == 0 or cycle < args.cycles:
            await asyncio.sleep(args.interval_s)
    await rb.stop()
    return 0


def main(argv=None) -> int:
    args = build_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
