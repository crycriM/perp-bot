import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from fetch_hl_data import candle_trades, derive_venue_rules


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


def test_venue_rules_use_hl_size_and_significant_price_limits():
    vvv = derive_venue_rules(
        {"name": "VVV", "szDecimals": 2}, {"markPx": "27.8661"},
        quote_notional=25,
    )
    ena = derive_venue_rules(
        {"name": "ENA", "szDecimals": 0}, {"markPx": "0.25861"},
        quote_notional=25,
    )

    assert vvv == {
        "coin": "VVV", "mark_price": 27.8661, "size_step": 0.01,
        "price_tick": 0.001, "min_notional": 10.0,
        "min_order_size": 0.36, "quote_notional": 25.0, "quote_size": 0.9,
    }
    assert ena == {
        "coin": "ENA", "mark_price": 0.25861, "size_step": 1.0,
        "price_tick": 0.00001, "min_notional": 10.0,
        "min_order_size": 39.0, "quote_notional": 25.0, "quote_size": 97.0,
    }
