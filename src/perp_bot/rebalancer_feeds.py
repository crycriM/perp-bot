"""Live Hyperliquid feeds for `BasketRebalancer`.

Deployment plan §7 gap: the rebalancer was unit-tested with injected fakes
only. These providers bind it to real account positions and mids. They take
an already-constructed HL `Info`-like object (duck-typed: `user_state`,
`spot_user_state`, `all_mids`) so the module stays import-light and testable
without the SDK or the network.
"""

from __future__ import annotations


def hl_position_provider(info, addresses: dict[str, str]):
    """Provider `(account_id, coin) -> (signed_base_size, equity_usd)`.

    Equity is the unified-account spot USDC total: on HL unified accounts the
    perp `marginSummary.accountValue` only covers allocated margin (+PnL), so
    the keeper and this rebalancer must both use the marked spot total.
    """

    async def provider(account_id: str, coin: str) -> tuple[float, float]:
        address = addresses[account_id]  # KeyError is the loud failure
        perp = info.user_state(address)
        position = 0.0
        for entry in perp.get("assetPositions") or []:
            pos = entry.get("position", {})
            if pos.get("coin") == coin:
                position += float(pos.get("szi", 0.0))
        spot = info.spot_user_state(address)
        usdc = next(
            (b for b in spot.get("balances", []) if b.get("coin") == "USDC"), {},
        )
        return position, float(usdc.get("total", 0.0))

    return provider


def hl_price_provider(info):
    """Provider `coin -> mid price`; missing coin raises instead of guessing."""

    async def provider(coin: str) -> float:
        mids = info.all_mids()
        if coin not in mids:
            raise ValueError(f"no Hyperliquid mid for {coin} (got {sorted(mids)[:8]}…)")
        return float(mids[coin])

    return provider


__all__ = ["hl_position_provider", "hl_price_provider"]
