from perp_bot.lob_calibration import candidate_passes, rank_candidate


def test_candidate_requires_each_slice_to_pass_gates_and_fill_floor():
    passing = {
        "net_edge_bps": 3.0,
        "markout_ratio": 0.2,
        "max_drawdown": 0.01,
        "max_initial_margin_fraction": .1,
        "n_fills": 10,
    }

    assert candidate_passes(passing, passing, min_fills=10)
    assert not candidate_passes({**passing, "n_fills": 9}, passing, min_fills=10)
    assert not candidate_passes(passing, {**passing, "net_edge_bps": -1.0}, min_fills=10)


def test_rank_candidate_uses_worst_slice_edge_first():
    stable = {
        "train": {"net_edge_bps": 4.0, "markout_ratio": 0.2, "max_drawdown": 0.01},
        "oos": {"net_edge_bps": 3.0, "markout_ratio": 0.2, "max_drawdown": 0.01},
    }
    overfit = {
        "train": {"net_edge_bps": 20.0, "markout_ratio": 0.1, "max_drawdown": 0.01},
        "oos": {"net_edge_bps": 2.5, "markout_ratio": 0.1, "max_drawdown": 0.01},
    }

    assert rank_candidate(stable) > rank_candidate(overfit)
