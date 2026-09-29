"""Shared fail-closed handling for the margin-health safety input."""

import logging
import math


logger = logging.getLogger(__name__)


def fail_closed_margin_available(value: object, *, source: str) -> float:
    """Return a finite margin reading, treating invalid data as a hard breach.

    ``0.0`` is deliberately used for invalid data because the risk policy
    already maps a zero available-after-maintenance ratio to EMERGENCY_EXIT.
    This keeps every producer on the same safety path instead of allowing a
    missing value or NaN to disable the stop through ordinary comparisons.
    """
    try:
        if value is None or isinstance(value, bool):
            raise ValueError("missing or boolean margin value")
        margin_available = float(value)
        if not math.isfinite(margin_available):
            raise ValueError("non-finite margin value")
    except (TypeError, ValueError, OverflowError) as exc:
        logger.critical(
            "Invalid margin_available from %s (%r: %s); failing closed at 0.0 "
            "to trigger an emergency exit",
            source,
            value,
            exc,
        )
        return 0.0
    return margin_available


def initial_margin_available(*, equity: float, position: float, mid: float, leverage: float) -> float:
    """Replay stand-in for the venue's available-after-maintenance balance.

    Uses initial margin (notional / leverage), which is at least the maintenance
    margin live uses, so replay is never less cautious than live. Not a
    liquidation model.
    """
    return equity - abs(position) * mid / leverage
