from collections import defaultdict

from perp_bot.config import PerpPairConfig
from perp_bot.venue_capabilities import get_venue_capabilities


def validate_account_topology(configs: list[PerpPairConfig]) -> list[PerpPairConfig]:
    """Reject duplicate netted-venue keepers on the same market/account.

    Hyperliquid-style venues cannot hold separate long/short legs on one
    subaccount, so the safe deployment shape is one keeper per
    (exchange, coin, account_id). Hedge-capable venues are allowed to share an
    account for the same market.
    """
    by_market_account: dict[tuple[str, str, str], list[PerpPairConfig]] = defaultdict(list)
    for cfg in configs:
        caps = get_venue_capabilities(cfg.exchange)
        key = (cfg.exchange, cfg.coin, cfg.account_id)
        siblings = by_market_account[key]
        if siblings and caps.position_mode == "net":
            raise ValueError(
                "Netted venues require one keeper per "
                f"(exchange, coin, account_id); duplicate {cfg.exchange}/{cfg.coin} "
                f"on account '{cfg.account_id}' is unsafe"
            )
        siblings.append(cfg)
    return configs


__all__ = ["validate_account_topology"]
