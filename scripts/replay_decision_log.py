"""Phase 1 decision-log parity: replay an HB-hosted controller's log through a
standalone Keeper and diff the two.

The HB-hosted `PerpMMController` (shadow mode) writes one decision record per
control cycle with the exact inputs it fed the keeper: `ts`, `mid`, `inventory`
(venue position after reconciliation), `equity`, and the validated
`margin_available`. This script feeds those same inputs, in order, to a bare
`perp_bot.keeper.Keeper` — no Hummingbot, no InProcessClient — and compares the
resulting records with `diff_decision_logs.compare`. Replaying the recorded
inputs keeps the comparison deterministic: two live processes would tick at
different instants and the diff would measure timing jitter, not hosting.

Known, accounted-for delta: the decision record does not carry the funding rate,
so the replay accrues no funding and `total_pnl` differs. `total_pnl` is not a
compared field, and funding cannot change a decision (RiskPolicy reads equity,
inventory, margin health, regime and markout only).

Usage:
  python scripts/replay_decision_log.py <controller.yml> <hb_decisions.jsonl> [--out replay.jsonl]
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import yaml

from mm_core.inventory import Caps
from mm_core.risk_policy import RiskConfig

from perp_bot.config import PerpPairConfig
from perp_bot.keeper import Keeper
from perp_bot.opms_client import Position

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diff_decision_logs import compare, load  # noqa: E402


class _ReplayClient:
    """Answers the keeper's position query with the recorded venue position."""

    def __init__(self):
        self.positions: dict[str, Position] = {}

    async def get_positions(self):
        return self.positions

    async def stop(self):
        pass

    async def send_intent(self, intent):  # shadow mode never sends
        raise AssertionError("replay keeper must run in shadow mode")


def pair_config(controller: dict) -> PerpPairConfig:
    """Same mapping as PerpMMController.__init__ (kept in step by the parity diff itself)."""
    return PerpPairConfig(
        coin=controller["trading_pair"].split("-")[0],
        gamma=float(controller.get("gamma", 0.5)),
        kappa=float(controller.get("kappa", 0.3)),
        widen_factor=float(controller.get("widen_factor", 2.0)),
        exchange=controller["venue"],
        account_id=controller.get("account_id", "default"),
        target_inventory=float(controller.get("target_inventory", 0.0)),
        caps=Caps(max_position=float(controller.get("max_position", 10.0)),
                  critical_position=float(controller.get("critical_position", 20.0))),
        risk=RiskConfig(
            margin_health_soft=float(controller.get("margin_health_soft", 0.20)),
            margin_health_hard=float(controller.get("margin_health_hard", 0.10)),
        ),
    )


async def replay(controller: dict, records: list[dict], out: Path) -> None:
    config = pair_config(controller)
    client = _ReplayClient()
    out.unlink(missing_ok=True)
    keeper = Keeper(client, config, decision_log_path=str(out), shadow_mode=True)
    for rec in records:
        client.positions = {
            config.coin: Position(
                coin=config.coin,
                position=rec["inventory"],
                equity=rec["equity"],
                margin_available=rec.get("margin_available"),
            )
        }
        await keeper._on_snapshot({"ts": rec["ts"], "mid": rec["mid"], "funding_rate": None})
        await keeper._tick()
    await keeper.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("controller_yml", type=Path)
    ap.add_argument("hb_log", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    controller = yaml.safe_load(args.controller_yml.read_text())
    reference = load(args.hb_log)
    out = args.out or args.hb_log.with_suffix(".replay.jsonl")
    asyncio.run(replay(controller, reference, out))
    result = compare(reference, load(out))
    print(json.dumps(result, indent=2, sort_keys=True))
    clean = (result["reference_rows"] == result["candidate_rows"]
             and not any(result["mismatches"].values()))
    print("PARITY OK" if clean else "PARITY DELTAS — account for every one")
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
