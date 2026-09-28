import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from fetch_hl_data import candle_trades


def test_candle_extremes_share_the_bar_timestamp():
    trades = list(candle_trades([{
        "t": 1000,
        "v": "20",
        "o": "10",
        "h": "12",
        "l": "9",
        "c": "11",
    }]))

    assert [trade["ts"] for trade in trades] == [1.0, 1.0]
