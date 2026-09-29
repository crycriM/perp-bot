"""Calibration integrity: capture parsing, known-PnL replay economics, and
train/validation/holdout isolation in the screening driver.

Priority-0 items from the 20260929 review: receipt ordering, duplicates and
backfills must not corrupt the tape; replay economics must reproduce hand-
computable profit/loss; holdout manipulation must not change fitting or
selection; non-finite metrics must not break JSON export.
"""

import asyncio
import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

from perp_bot.backtest import BacktestBook, Backtrade
from perp_bot.hl_lob import LobReplayData, load_lob_capture, split_lob_replay
from perp_bot.lob_calibration import replay_candidate

_scripts = Path(__file__).resolve().parents[1] / "scripts"
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))
_spec = importlib.util.spec_from_file_location(
    "calibrate_hl_lob", _scripts / "calibrate_hl_lob.py")
calibrate_hl_lob = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calibrate_hl_lob)


def _book_line(ts_s, bid, ask, received_ms=None):
    data = {"coin": "SOL", "time": int(ts_s * 1000),
            "levels": [[{"px": str(bid), "sz": "5"},
                        {"px": str(bid - 1), "sz": "5"}],
                       [{"px": str(ask), "sz": "5"},
                        {"px": str(ask + 1), "sz": "5"}]]}
    line = {"channel": "l2Book", "data": data}
    if received_ms is not None:
        line["received_at_ms"] = received_ms
    return line


def _trade_line(ts_s, price, size, tid, side="A"):
    return {"channel": "trades", "data": [
        {"coin": "SOL", "side": side, "px": str(price), "sz": str(size),
         "time": int(ts_s * 1000), "tid": tid}]}


def _write_capture(path, lines):
    with path.open("w") as stream:
        for line in lines:
            stream.write(json.dumps(line) + "\n")


class TestLobCaptureIntegrity:
    def test_receipt_ordering_backfill_and_duplicates(self, tmp_path):
        base = 1_000_000.0
        lines = [
            _trade_line(base + 50, 99.0, 1, 1),            # backfill outside books
            _book_line(base + 2, 99.9, 100.1, int((base + 2) * 1000)),
            _book_line(base + 0, 99.9, 100.1, int((base + 0) * 1000)),
            _book_line(base + 1, 99.9, 100.1, int((base + 1) * 1000)),
            _trade_line(base + 1.5, 100.2, 1, 7),
            _trade_line(base + 0.5, 99.8, 1, 9),
            _trade_line(base + 1.5, 100.2, 1, 7),         # duplicate tid
        ]
        capture = tmp_path / "events.jsonl"
        _write_capture(capture, lines)
        data = load_lob_capture(capture, "SOL")
        assert [b.ts for b in data.books] == [base + 0, base + 1, base + 2]
        assert data.books[0].exchange_ts == base + 0       # stamp preserved
        assert [t.ts for t in data.trades] == [base + 0.5, base + 1.5]  # sorted, deduped
        assert all(t.price != 99.0 for t in data.trades)   # window excludes backfill

    def test_late_receipt_becomes_availability_time_not_exchange_time(self, tmp_path):
        base = 2_000_000.0
        capture = tmp_path / "events.jsonl"
        _write_capture(capture, [_book_line(base, 99.9, 100.1, int((base + 0.7) * 1000))])
        data = load_lob_capture(capture, "SOL")
        assert data.books[0].ts == pytest.approx(base + 0.7)
        assert data.books[0].exchange_ts == pytest.approx(base)

    @pytest.mark.parametrize("levels", [
        [[{"px": "101", "sz": "5"}], [{"px": "100", "sz": "5"}]],   # crossed
        [[{"px": "nan", "sz": "5"}], [{"px": "100", "sz": "5"}]],   # non-finite
        [[{"px": "99", "sz": "5"}, {"px": "100", "sz": "5"}],
         [{"px": "101", "sz": "5"}]],                                # unsorted bids
    ])
    def test_invalid_books_are_rejected_not_silently_skipped(self, tmp_path, levels):
        capture = tmp_path / "events.jsonl"
        record = {"channel": "l2Book",
                  "data": {"coin": "SOL", "time": 1000, "levels": levels}}
        _write_capture(capture, [record])
        with pytest.raises(ValueError, match="book"):
            load_lob_capture(capture, "SOL")

    def test_split_has_no_boundary_leak(self):
        books = tuple(BacktestBook(ts=float(t), bids=((99.9, 5),), asks=((100.1, 5),))
                      for t in range(10))
        trades = tuple(Backtrade(float(t), "buy", 100.0, 1.0) for t in range(10))
        data = LobReplayData((), books, trades)
        train, rest = split_lob_replay(data, 0.5)
        assert train.books[-1].ts < rest.books[0].ts
        assert len(train.trades) + len(rest.trades) == len(data.trades)
        assert {t.ts for t in train.trades}.isdisjoint({t.ts for t in rest.trades})
        assert {b.ts for b in train.books} | {b.ts for b in rest.books} == set(map(float, range(10)))


FLAT_MID = 100.0


def _flat_tape(n_books=16):
    return tuple(BacktestBook(ts=float(i), bids=((99.9, 5.0),), asks=((100.1, 5.0),))
                 for i in range(n_books))


def _flat_policy():
    from mm_core.regime import GateConfig
    from mm_core.risk_policy import RiskConfig
    return dict(pricing_model="fixed", fixed_half_spread_bps=6,
                maker_fee_bps=1.5, taker_fee_bps=4.5, min_edge_bps=0,
                quote_reprice_bps=2, quote_post_only_buffer_bps=1,
                cancel_latency_s=0, place_latency_s=0, close_slippage_bps=2,
                min_quote_notional=0, max_market_data_age_s=None, size_step=None,
                exchange="hyperliquid", target_inventory=0, leverage=3,
                risk=RiskConfig(gate=GateConfig(regime_stop=False), toxic_markout_bps=-1))


async def _replay(books, trades, policy, funding=None):
    data = LobReplayData((), books, trades)
    return await replay_candidate(
        data, coin="SOL", gamma=0.02, kappa=0.4,
        quote_size=1.0, price_tick=0.001, start_equity=1000.0, tick_s=0.25,
        decision_interval_s=1.0, quote_refresh_s=5.0, max_position_multiple=5,
        policy=policy, funding=funding)


class TestReplayKnownEconomics:
    def test_flat_mean_reverting_tape_captures_a_known_spread(self):
        policy = _flat_policy()
        books = _flat_tape()
        trades = tuple(
            Backtrade(t, side, price, 1.0)
            for t, side, price in [
                (2.5, "sell", 99.88), (3.5, "buy", 100.12),
                (4.5, "sell", 99.88), (5.5, "buy", 100.12),
            ])
        metrics = asyncio.run(_replay(books, trades, policy))
        assert metrics["n_fills"] == 4
        # run() buffers the intent prices AFTER tick rounding: the resting
        # prices are the buffered touch values themselves.
        bid = 99.9 * (1 - 1e-4)
        ask = 100.1 * (1 + 1e-4)
        net_per_pair = (ask - bid) - (bid + ask) * 1.5e-4
        assert metrics["net_pnl"] == pytest.approx(2 * net_per_pair, rel=1e-6)
        assert metrics["fee_pnl"] == pytest.approx(-2 * (bid + ask) * 1.5e-4, rel=1e-6)
        assert metrics["spread_capture"] > 0
        assert metrics["liquidations"] is None

    def test_drifting_tape_is_a_known_loss_with_adverse_markout(self):
        policy = _flat_policy()
        books = tuple(BacktestBook(ts=float(i), bids=((99.9 - 0.05 * i, 5.0),),
                                   asks=((100.1 - 0.05 * i, 5.0),))
                      for i in range(16))
        trades = tuple(Backtrade(2.5 + k, "sell", 99.6 - 0.05 * (2 + k), 1.0)
                       for k in range(8))
        metrics = asyncio.run(_replay(books, trades, policy))
        assert 0 < metrics["n_fills"] <= 5            # cap clamps at 5 lots
        assert metrics["net_pnl"] < 0
        assert metrics["markout_ratio"] > 0
        assert metrics["max_drawdown"] > 0

    def test_funding_applies_only_inside_the_slice_window(self):
        policy = _flat_policy()
        books = _flat_tape()
        trades = tuple(Backtrade(2.5 + k * 2, "sell", 99.88, 1.0) for k in range(5))
        inside = [dict(ts=14.0, rate=0.001)]
        outside = [dict(ts=99.0, rate=0.001), dict(ts=-5.0, rate=0.001)]
        with_funding = asyncio.run(_replay(books, trades, policy, funding=inside))
        without = asyncio.run(_replay(books, trades, policy, funding=outside))
        assert with_funding["funding_pnl"] < 0        # long pays a positive rate
        assert without["funding_pnl"] == pytest.approx(0.0)


def _args(tmp_path, rules_dir):
    import argparse
    return argparse.Namespace(
        capture_dir=tmp_path, rules_dir=rules_dir, venue="hyperliquid",
        maker_fee_bps=1.5, taker_fee_bps=4.5, cancel_latency_s=0.5,
        place_latency_s=0.2, decision_interval_s=1.0, close_slippage_bps=2,
        min_edge_bps=3, min_fills=5, start_equity=300.0, output=None)


def _good_quality():
    return dict(passed=True, failures=[], duration_s=40, books=40, trades=200,
                gap_p95_s=1.0, gap_max_s=2.0, receipt_lag_p95_s=0.5)


def _metrics(edge, *, pnl=None, ratio=0.0, dd=0.001, margin=0.1, fills=10):
    return dict(net_edge_bps=edge, net_pnl=pnl if pnl is not None else edge * 0.01,
                markout_ratio=ratio, max_drawdown=dd,
                max_initial_margin_fraction=margin, n_fills=fills)


class TestCalibrateHoldoutIsolation:
    """Fake replay keys economics on the slice window so a poisoned holdout
    can only move holdout numbers — selection must stay on validation."""

    @pytest.fixture()
    def env(self, tmp_path, monkeypatch):
        base = 1_700_000_100.0
        capture = tmp_path / "events.jsonl"
        _write_capture(capture, [
            _book_line(base + t, 99.9, 100.1, int((base + t + 0.3) * 1000))
            for t in range(40)
        ] + [_trade_line(base + 30 + k * 0.4, 99.8, 1, 100 + k) for k in range(20)])
        rules = tmp_path / "rules"
        rules.mkdir()
        (rules / "SOL_rules.json").write_text(json.dumps(
            {"size_step": "0.01", "min_notional": "10", "quote_size": "1",
             "price_tick": "0.001"}))
        (rules / "SOL_funding.csv").write_text(
            f"ts,rate\n{math.floor((base + 39) / 3600) * 3600},0.0001\n")

        calls = []

        async def fake_replay(slice_, *, gamma, kappa, policy, **common):
            start = slice_.books[0].ts
            half = policy.get("fixed_half_spread_bps")
            calls.append((start, half))
            if start < base + 23:
                raise AssertionError("train must not be replayed for selection")
            if start > base + 31:                      # holdout slice
                if half == 16 and len(slice_.trades) > 25:
                    return _metrics(100.0)             # bait only exists on holdout
                return _metrics(2.5 if half == 10 else 1.0)
            return _metrics(3.0 if half == 10 else 1.0)  # validation slice

        monkeypatch.setattr(calibrate_hl_lob, "replay_candidate", fake_replay)
        monkeypatch.setattr(calibrate_hl_lob, "capture_quality", lambda data: _good_quality())
        monkeypatch.setattr(calibrate_hl_lob, "estimate_parameters", lambda train: dict(
            arrival_rate_per_s=2.0, kappa_per_bps=0.4, sigma_bps_sqrt_s=1.0,
            side_fits={}, estimator="test"))
        return dict(tmp=tmp_path, rules=rules, base=base, calls=calls,
                    capture=capture, events=capture.read_text())

    def _run(self, env):
        return asyncio.run(calibrate_hl_lob.calibrate_coin(
            _args(env["tmp"], env["rules"]), "SOL"))

    def test_selection_uses_validation_only_and_evaluates_holdout_once(self, env):
        result = self._run(env)
        assert result["selected"]["parameters"]["fixed_half_spread_bps"] == 10
        holdout = {(s, h) for s, h in env["calls"] if s > env["base"] + 31}
        assert holdout and all(h == 10 for _, h in holdout)
        starts = {s for s, _ in env["calls"] if s > env["base"] + 31}
        assert len(starts) == 1                        # one holdout window only
        assert sum(1 for s, _ in env["calls"] if s > env["base"] + 31) == 2  # normal+stress
        validation = [s for s, _ in env["calls"] if s <= env["base"] + 31]
        assert all(s > env["base"] + 23 for s in validation)

    def test_poisoned_holdout_cannot_change_selection(self, env):
        clean = self._run(env)
        extra = [_trade_line(env["base"] + 32 + k * 0.3, 99.5, 1, 500 + k)
                 for k in range(20)]
        env["capture"].write_text(env["events"] + "\n".join(
            json.dumps(line) for line in extra) + "\n")
        env["calls"].clear()
        poisoned = self._run(env)
        assert poisoned["selected"]["parameters"] == clean["selected"]["parameters"]
        assert poisoned["estimates"] == clean["estimates"]

    def test_approval_needs_positive_stress(self, env):
        approved = self._run(env)
        assert approved["approved_for_micro_soak"] is True

        holder = dict(replay=calibrate_hl_lob.replay_candidate)
        holdout_seen = []

        async def stress_negative(slice_, *, gamma, kappa, policy, **common):
            if slice_.books[0].ts > env["base"] + 31:
                holdout_seen.append(1)
                if len(holdout_seen) == 2:             # the stress evaluation
                    return _metrics(2.5, pnl=-1.0)
            return await holder["replay"](slice_, gamma=gamma, kappa=kappa,
                                          policy=policy, **common)

        env["calls"].clear()
        calibrate_hl_lob.replay_candidate = stress_negative
        blocked = self._run(env)
        assert blocked["approved_for_micro_soak"] is False

    def test_missing_funding_blocks_and_failed_estimation_is_reported(self, env, monkeypatch):
        (env["rules"] / "SOL_funding.csv").unlink()
        result = self._run(env)
        assert result["approved_for_micro_soak"] is False
        assert "funding_coverage" in result["quality"]["failures"]

        def boom(train):
            raise ValueError("intensity not identifiable")
        monkeypatch.setattr(calibrate_hl_lob, "estimate_parameters", boom)
        result = self._run(env)
        assert result["error"] == "intensity not identifiable"
        assert result["approved_for_micro_soak"] is False

    def test_nonfinite_metrics_serialize_as_null_not_crash(self, env, monkeypatch):
        async def inf_replay(slice_, *, gamma, kappa, policy, **common):
            return _metrics(float("inf"), ratio=float("inf"))
        monkeypatch.setattr(calibrate_hl_lob, "replay_candidate", inf_replay)
        monkeypatch.setattr(sys, "argv", [
            "calibrate_hl_lob.py", str(env["tmp"]), "SOL",
            "--rules-dir", str(env["rules"]), "--maker-fee-bps", "1.5",
            "--taker-fee-bps", "4.5", "--cancel-latency-s", "0.5",
            "--place-latency-s", "0.2"])
        calibrate_hl_lob.main()
        report = json.loads((env["tmp"] / "calibration_v2.json").read_text())
        assert report["schema_version"] == 2
        assert len(report["capture_sha256"]) == 64
        row = report["results"][0]["selected"]["holdout"]
        assert row["net_edge_bps"] is None and row["markout_ratio"] is None


class TestScreenLiveDrawdownParity:
    """The prepared live soak stops at 1% account DD (MAX_DRAWDOWN_PCT=1.0).
    A candidate whose validation/holdout history would breach that stop must
    not be screened as eligible; the generic rollout gate (5%) must not be
    used silently for calibration approval."""

    @pytest.fixture()
    def env(self, tmp_path, monkeypatch):
        base = 1_700_000_100.0
        capture = tmp_path / "events.jsonl"
        _write_capture(capture, [
            _book_line(base + t, 99.9, 100.1, int((base + t + 0.3) * 1000))
            for t in range(40)
        ] + [_trade_line(base + 30 + k * 0.4, 99.8, 1, 100 + k) for k in range(20)])
        rules = tmp_path / "rules"
        rules.mkdir()
        (rules / "SOL_rules.json").write_text(json.dumps(
            {"size_step": "0.01", "min_notional": "10", "quote_size": "1",
             "price_tick": "0.001"}))
        (rules / "SOL_funding.csv").write_text(
            f"ts,rate\n{math.floor((base + 39) / 3600) * 3600},0.0001\n")
        monkeypatch.setattr(calibrate_hl_lob, "capture_quality", lambda data: _good_quality())
        monkeypatch.setattr(calibrate_hl_lob, "estimate_parameters", lambda train: dict(
            arrival_rate_per_s=2.0, kappa_per_bps=0.4, sigma_bps_sqrt_s=1.0,
            side_fits={}, estimator="test"))
        return dict(tmp=tmp_path, rules=rules, base=base, capture=capture)

    def _run(self, env):
        return asyncio.run(calibrate_hl_lob.calibrate_coin(
            _args(env["tmp"], env["rules"]), "SOL"))

    def test_validation_dd_above_live_stop_disqualifies_the_edge_leader(self, env, monkeypatch):
        async def dd_split(slice_, *, gamma, kappa, policy, **common):
            if slice_.books[0].ts < env["base"] + 23:
                raise AssertionError("train must not be replayed")
            half = policy.get("fixed_half_spread_bps")
            if slice_.books[0].ts > env["base"] + 31:
                return _metrics(2.5, dd=0.005)
            if half == 10:                      # edge leader breaches the live stop
                return _metrics(9.0, dd=0.02)
            if half == 6:
                return _metrics(4.0, dd=0.005)
            return _metrics(1.0, dd=0.001)
        monkeypatch.setattr(calibrate_hl_lob, "replay_candidate", dd_split)
        result = self._run(env)
        assert result["selected"]["parameters"]["fixed_half_spread_bps"] == 6
        assert result["approved_for_micro_soak"] is True

    def test_only_dd_breaching_candidates_cannot_be_approved(self, env, monkeypatch):
        async def all_hot(slice_, *, gamma, kappa, policy, **common):
            if slice_.books[0].ts > env["base"] + 31:
                return _metrics(9.0, dd=0.02)
            return _metrics(9.0, dd=0.02)
        monkeypatch.setattr(calibrate_hl_lob, "replay_candidate", all_hot)
        result = self._run(env)
        assert result["approved_for_micro_soak"] is False
