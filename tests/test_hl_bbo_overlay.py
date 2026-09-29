"""HL public l2Book is throttled to ~5.5 s; `bbo` is block cadence (~0.1-0.9 s).
The book the strategy sees is l2Book depth with the latest bbo touch on top."""
import json

import pytest

from perp_bot.hl_lob import BookOverlay, load_lob_capture

DEPTH_BIDS = ((10.0, 5.0), (9.9, 3.0), (9.8, 2.0))
DEPTH_ASKS = ((10.1, 4.0), (10.2, 6.0), (10.3, 1.0))


def _overlay():
    overlay = BookOverlay()
    overlay.on_l2(1000, DEPTH_BIDS, DEPTH_ASKS)
    return overlay


def test_bbo_before_any_l2_has_no_depth_to_overlay():
    assert BookOverlay().on_bbo(1000, (10.0, 1.0), (10.1, 1.0)) is None


def test_bbo_drops_stale_better_levels_and_keeps_deeper_ones():
    bids, asks = _overlay().on_bbo(1200, (9.95, 2.0), (10.15, 3.0))
    assert bids == ((9.95, 2.0), (9.9, 3.0), (9.8, 2.0))
    assert asks == ((10.15, 3.0), (10.2, 6.0), (10.3, 1.0))


def test_bbo_inside_the_old_touch_is_added_on_top():
    bids, asks = _overlay().on_bbo(1200, (10.02, 7.0), (10.08, 1.0))
    assert bids[:2] == ((10.02, 7.0), (10.0, 5.0)) and asks[:2] == ((10.08, 1.0), (10.1, 4.0))


def test_bbo_same_price_replaces_size_and_touch_break_removes_levels_through_it():
    bids, asks = _overlay().on_bbo(1200, (10.0, 9.0), (10.1, 8.0))
    assert bids[0] == (10.0, 9.0) and asks[0] == (10.1, 8.0)
    bids, asks = _overlay().on_bbo(1200, (9.8, 1.0), (10.3, 1.0))
    assert bids == ((9.8, 1.0),) and asks == ((10.3, 1.0),)


def test_one_sided_or_crossed_bbo_is_not_applied():
    overlay = _overlay()
    assert overlay.on_bbo(1200, None, (10.1, 1.0)) is None
    with pytest.raises(ValueError):
        overlay.on_bbo(1300, (10.2, 1.0), (10.1, 1.0))


def test_l2_arriving_after_a_newer_bbo_keeps_the_bbo_touch():
    overlay = _overlay()
    overlay.on_bbo(2000, (9.95, 2.0), (10.15, 3.0))
    bids, asks = overlay.on_l2(1900, DEPTH_BIDS, DEPTH_ASKS)  # older than the bbo
    assert bids[0] == (9.95, 2.0) and asks[0] == (10.15, 3.0)
    bids, asks = overlay.on_l2(2100, DEPTH_BIDS, DEPTH_ASKS)  # newer: plain snapshot wins
    assert bids == DEPTH_BIDS and asks == DEPTH_ASKS


def test_healthy_while_l2_touch_matches_the_last_bbo_at_or_before_it():
    overlay = _overlay()
    for i in range(6):
        overlay.on_bbo(2000 + i * 5500, (10.0, 1.0), (10.1, 1.0))
        overlay.on_l2(2000 + i * 5500 + 100, DEPTH_BIDS, DEPTH_ASKS)
    assert overlay.healthy


def test_silent_bbo_stream_turns_unhealthy_after_three_disagreeing_l2_snapshots():
    overlay = BookOverlay()
    overlay.on_bbo(900, (10.0, 1.0), (10.1, 1.0))  # recorded even before depth exists
    overlay.on_l2(1000, DEPTH_BIDS, DEPTH_ASKS)
    moved = ((10.05, 5.0), (9.9, 3.0)), ((10.15, 4.0), (10.2, 6.0))  # market moved, no bbo arrived
    for i in range(2):
        overlay.on_l2(7000 + i * 5500, *moved)
        assert overlay.healthy
    overlay.on_l2(18000, *moved)
    assert not overlay.healthy
    overlay.on_bbo(18100, (10.05, 5.0), (10.15, 4.0))
    overlay.on_l2(23500, *moved)  # agreement resets the streak
    assert overlay.healthy


def test_startup_l2_before_any_bbo_and_no_bbo_at_all_are_distinguished():
    overlay = BookOverlay()
    overlay.on_l2(1000, DEPTH_BIDS, DEPTH_ASKS)
    assert overlay.healthy  # one l2 without a bbo is just startup
    overlay.on_l2(6500, DEPTH_BIDS, DEPTH_ASKS)
    overlay.on_l2(12000, DEPTH_BIDS, DEPTH_ASKS)
    assert not overlay.healthy  # bbo never delivered


def _l2(ts, bid, ask, coin="ENA"):
    return {"channel": "l2Book", "data": {"coin": coin, "time": ts, "levels": [
        [{"px": str(bid), "sz": "100", "n": 1}, {"px": str(bid - 0.01), "sz": "50", "n": 1}],
        [{"px": str(ask), "sz": "200", "n": 1}, {"px": str(ask + 0.01), "sz": "60", "n": 1}]]}}


def _bbo(ts, bid, ask, coin="ENA"):
    side = lambda px: None if px is None else {"px": str(px), "sz": "7", "n": 1}
    return {"channel": "bbo", "data": {"coin": coin, "time": ts, "bbo": [side(bid), side(ask)]}}


def test_loader_emits_a_book_per_bbo_on_top_of_the_latest_l2(tmp_path):
    path = tmp_path / "events.jsonl"
    events = [
        {"channel": "pong"}, {"channel": "subscriptionResponse", "data": {}},
        {"received_at_ms": 1050, **_l2(1000, 1.00, 1.02)},
        {"received_at_ms": 1300, **_bbo(1200, 1.01, 1.02)},
        {"received_at_ms": 1400, **_bbo(1350, None, 1.03)},          # one-sided: ignored
        {"received_at_ms": 1500, **_bbo(1400, 1.005, 1.03, "VVV")},  # other coin: ignored
        {"received_at_ms": 6600, **_l2(6500, 1.01, 1.03)},
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in events))

    replay = load_lob_capture(path, "ENA")

    assert [b.ts for b in replay.books] == [1.05, 1.3, 6.6]
    assert [b.exchange_ts for b in replay.books] == [1.0, 1.2, 6.5]
    assert replay.books[1].bids == ((1.01, 7.0), (1.0, 100.0), (0.99, 50.0))
    assert replay.books[1].asks[0] == (1.02, 7.0)
    assert len(replay.snapshots) == 3 and replay.snapshots[1].mid == pytest.approx(1.015)
