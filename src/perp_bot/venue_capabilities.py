from dataclasses import dataclass


@dataclass(frozen=True)
class VenueCapabilities:
    venue: str
    position_mode: str
    supports_same_account_hedge: bool


_VENUE_CAPABILITIES: dict[str, VenueCapabilities] = {
    "hyperliquid": VenueCapabilities(
        venue="hyperliquid",
        position_mode="net",
        supports_same_account_hedge=False,
    ),
    "aster": VenueCapabilities(
        venue="aster",
        position_mode="hedge",
        supports_same_account_hedge=True,
    ),
    "lighter": VenueCapabilities(
        venue="lighter",
        position_mode="hedge",
        supports_same_account_hedge=True,
    ),
    "mock": VenueCapabilities(
        venue="mock",
        position_mode="hedge",
        supports_same_account_hedge=True,
    ),
}


def get_venue_capabilities(venue: str) -> VenueCapabilities:
    key = venue.lower()
    if key not in _VENUE_CAPABILITIES:
        raise ValueError(f"Unsupported venue capabilities for exchange '{venue}'")
    return _VENUE_CAPABILITIES[key]


__all__ = ["VenueCapabilities", "get_venue_capabilities"]
