"""Explicit bps/seconds estimation and screening, not proof of executable alpha."""

import math
import hashlib
from pathlib import Path
from bisect import bisect_right


def source_fingerprint():
    root = Path(__file__).resolve().parents[3]
    paths = ("mm-core/src/mm_core/as_core.py", "mm-core/src/mm_core/quote_refresh.py",
             "mm-core/src/mm_core/risk_policy.py", "mm-core/src/mm_core/markout.py",
             "perp-bot/src/perp_bot/config.py", "perp-bot/src/perp_bot/keeper.py",
             "perp-bot/src/perp_bot/backtest.py", "perp-bot/src/perp_bot/calibration.py",
             "perp-bot/src/perp_bot/lob_calibration.py", "perp-bot/scripts/calibrate_hl_lob.py",
             "hb-enhanced-opms/src/opms/controllers/generic/perp_mm_controller.py",
             "hb-enhanced-opms/src/opms/executors/buffered_maker_executor.py")
    return {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths}


def percentile(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * p))] if values else None


def capture_quality(data) -> dict:
    gaps = [b.ts - a.ts for a, b in zip(data.books, data.books[1:]) if b.ts > a.ts]
    lag = [b.ts - b.exchange_ts for b in data.books if b.exchange_ts is not None]
    duration = data.books[-1].ts - data.books[0].ts if data.books else 0
    checks = dict(duration=duration >= 21600, trades=len(data.trades) >= 1000,
                  receipt_timestamps=len(lag) == len(data.books) and bool(lag),
                  book_cadence=bool(gaps) and percentile(gaps, .95) <= 1,
                  book_gaps=bool(gaps) and max(gaps) <= 5,
                  receipt_lag=bool(lag) and percentile(lag, .95) <= 1)
    return dict(passed=all(checks.values()), failures=[k for k, v in checks.items() if not v],
                duration_s=duration, books=len(data.books), trades=len(data.trades),
                gap_p95_s=percentile(gaps, .95), gap_max_s=max(gaps, default=None),
                receipt_lag_p95_s=percentile(lag, .95))


def fit_exponential_intensity(depths, counts, exposure_s) -> dict:
    """Poisson MLE N_i ~ Poisson(T_i A exp(-k d_i)), including zero counts.

    Profile A analytically, solve the one-dimensional score for positive k.
    Depth bins from the same public trades are correlated: do NOT interpret
    this fit as an independent-sample confidence interval.
    """
    if (len(depths) < 3 or len(depths) != len(counts) or len(counts) != len(exposure_s)
            or any(not math.isfinite(x) or x < 0 for x in list(depths) + list(counts))
            or any(not math.isfinite(x) or x <= 0 for x in exposure_s)
            or len(set(depths)) < 3 or sum(counts) < 10):
        raise ValueError("insufficient valid intensity exposure/counts")
    observed = sum(n * d for n, d in zip(counts, depths)) / sum(counts)
    def predicted(k):
        weights = [t * math.exp(-k * d) for t, d in zip(exposure_s, depths)]
        return sum(w * d for w, d in zip(weights, depths)) / sum(weights)
    lo, hi = 1e-6, 20.0
    if not predicted(hi) < observed < predicted(lo):
        raise ValueError("positive exponential decay is not identifiable")
    for _ in range(80):
        k = (lo + hi) / 2
        if predicted(k) > observed:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    rate = sum(counts) / sum(t * math.exp(-k * d) for t, d in zip(exposure_s, depths))
    return dict(arrival_rate_per_s=rate, kappa_per_bps=k,
                depths_bps=list(depths), counts=list(counts), exposure_s=list(exposure_s))


def estimate_parameters(data, depths=(0, 1, 2, 4, 8, 16, 32)) -> dict:
    """Training-only public penetration-rate proxy and realized volatility.

    This A is NOT the strategy's own fill hazard: queue, size, cancel latency
    and competing makers can reduce it. Replay uses visible queue; a shadow
    then micro-live survival calibration is still required before scaling.
    """
    if len(data.books) < 3:
        raise ValueError("not enough training books")
    elapsed = data.books[-1].ts - data.books[0].ts
    sigma = 1e4 * math.sqrt(sum(math.log(b.mid / a.mid) ** 2
                      for a, b in zip(data.books, data.books[1:])) / elapsed)
    counts = {side: [0] * len(depths) for side in ("buy", "sell")}
    times = [b.ts for b in data.books]
    for trade in data.trades:
        i = bisect_right(times, trade.ts) - 1
        if i < 0 or trade.ts - times[i] > 1:
            continue  # no extrapolation through stale public data
        mid = data.books[i].mid
        distance = (trade.price - mid if trade.side == "buy" else mid - trade.price) / mid * 1e4
        for j, depth in enumerate(depths):
            counts[trade.side][j] += int(distance >= depth)
    # Count exposure only during the same <=1s valid-book windows used above.
    exposure = sum(min(1.0, b - a) for a, b in zip(times, times[1:]) if b > a)
    fits = {side: fit_exponential_intensity(depths, n, [exposure] * len(depths))
            for side, n in counts.items()}
    pooled = fit_exponential_intensity(depths,
        [counts["buy"][j] + counts["sell"][j] for j in range(len(depths))], [2 * exposure] * len(depths))
    return dict(**pooled, sigma_bps_sqrt_s=sigma, side_fits=fits,
                estimator="public_penetration_proxy_not_own_fill_hazard")


def choose_candidate(rows):
    """Selection uses validation ONLY. Holdout is evaluated once after this."""
    return max(rows, key=lambda row: row["validation"]["net_edge_bps"])
