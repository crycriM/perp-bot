import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from capture_hl_lob import _count_message, subscription_messages


def test_capture_subscribes_to_books_bbo_and_trades_for_each_coin():
    # l2Book alone is throttled to ~5.5 s by the venue; bbo is the block-cadence touch.
    assert subscription_messages(["VVV", "ENA"]) == [
        {"method": "subscribe", "subscription": {"type": channel, "coin": coin}}
        for coin in ("VVV", "ENA") for channel in ("l2Book", "bbo", "trades")
    ]


def test_capture_counts_bbo_messages_per_coin():
    counts = {"ENA": {"books": 0, "bbos": 0, "trades": 0}}
    _count_message(counts, {"channel": "bbo", "data": {"coin": "ENA", "time": 1, "bbo": [None, None]}})
    _count_message(counts, {"channel": "bbo", "data": {"coin": "XYZ", "time": 1, "bbo": [None, None]}})
    assert counts == {"ENA": {"books": 0, "bbos": 1, "trades": 0}}
