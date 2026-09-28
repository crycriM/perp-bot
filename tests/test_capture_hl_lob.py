import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from capture_hl_lob import subscription_messages


def test_capture_subscribes_to_books_and_trades_for_each_coin():
    assert subscription_messages(["VVV", "ENA"]) == [
        {"method": "subscribe", "subscription": {"type": "l2Book", "coin": "VVV"}},
        {"method": "subscribe", "subscription": {"type": "trades", "coin": "VVV"}},
        {"method": "subscribe", "subscription": {"type": "l2Book", "coin": "ENA"}},
        {"method": "subscribe", "subscription": {"type": "trades", "coin": "ENA"}},
    ]
