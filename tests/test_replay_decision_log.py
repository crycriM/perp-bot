import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from replay_decision_log import pair_config, replay  # noqa: E402


def controller_config():
    return {
        "trading_pair": "BTC-USD",
        "venue": "hyperliquid",
        "margin_health_soft": 0.50,
        "margin_health_hard": 0.10,
    }


def test_pair_config_preserves_margin_health_thresholds():
    config = pair_config(controller_config())

    assert config.risk.margin_health_soft == pytest.approx(0.50)
    assert config.risk.margin_health_hard == pytest.approx(0.10)


@pytest.mark.asyncio
async def test_replay_preserves_margin_health_decision(tmp_path):
    out = tmp_path / "replay.jsonl"
    records = [{
        "ts": 1.0,
        "mid": 100.0,
        "inventory": 2.0,
        "equity": 1000.0,
        "margin_available": 300.0,
    }]

    await replay(controller_config(), records, out)

    replayed = json.loads(out.read_text())
    assert replayed["margin_available"] == pytest.approx(300.0)
    assert replayed["decision"] == "de_risk"
