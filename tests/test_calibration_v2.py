import math

import pytest

from perp_bot.calibration import fit_exponential_intensity, choose_candidate, capture_quality
from perp_bot.backtest import BacktestBook
from perp_bot.hl_lob import LobReplayData


def test_intensity_fit_recovers_units_and_includes_zero_count_exposure():
    depths = [0, 1, 2, 3, 5]
    exposure = [10000] * 5
    counts = [round(2 * math.exp(-.4 * d) * t) for d, t in zip(depths, exposure)]
    fit = fit_exponential_intensity(depths, counts, exposure)
    assert fit["arrival_rate_per_s"] == pytest.approx(2, rel=.001)
    assert fit["kappa_per_bps"] == pytest.approx(.4, rel=.001)
    zeros = fit_exponential_intensity(depths, counts[:-1] + [0], exposure)
    assert zeros["kappa_per_bps"] > fit["kappa_per_bps"]


def test_choose_candidate_cannot_see_holdout():
    rows = [dict(model="a", validation={"net_edge_bps": 3}, holdout={"net_edge_bps": -100}),
            dict(model="b", validation={"net_edge_bps": 2}, holdout={"net_edge_bps": 100})]
    assert choose_candidate(rows)["model"] == "a"


def test_sparse_or_unstamped_capture_cannot_pass():
    books = tuple(BacktestBook(ts=t, bids=((99, 1),), asks=((101, 1),)) for t in range(0, 36000, 5))
    data = LobReplayData((), books, ())
    q = capture_quality(data)
    assert not q["passed"]
    assert "book_cadence" in q["failures"]
    assert "receipt_timestamps" in q["failures"]
