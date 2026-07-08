import pytest

from perp_bot.config import PerpPairConfig
from perp_bot.topology import validate_account_topology
from perp_bot.venue_capabilities import get_venue_capabilities


def test_hyperliquid_defaults_to_net_position_mode():
    config = PerpPairConfig(coin="BTC", exchange="hyperliquid")
    assert config.position_mode == "net"


def test_hedge_capable_venue_defaults_to_hedge_mode():
    config = PerpPairConfig(coin="BTC", exchange="aster")
    assert config.position_mode == "hedge"


def test_rejects_incompatible_explicit_position_mode():
    with pytest.raises(ValueError, match="position_mode='net'"):
        PerpPairConfig(coin="BTC", exchange="hyperliquid", position_mode="hedge")


def test_validate_account_topology_rejects_duplicate_hyperliquid_market_account():
    configs = [
        PerpPairConfig(coin="BTC", exchange="hyperliquid", account_id="mm-a"),
        PerpPairConfig(coin="BTC", exchange="hyperliquid", account_id="mm-a"),
    ]

    with pytest.raises(ValueError, match="one keeper per"):
        validate_account_topology(configs)


def test_validate_account_topology_allows_duplicate_aster_market_account():
    configs = [
        PerpPairConfig(coin="BTC", exchange="aster", account_id="mm-a"),
        PerpPairConfig(coin="BTC", exchange="aster", account_id="mm-a"),
    ]

    assert validate_account_topology(configs) == configs


def test_venue_capabilities_flag_same_account_hedge_support():
    assert get_venue_capabilities("hyperliquid").supports_same_account_hedge is False
    assert get_venue_capabilities("lighter").supports_same_account_hedge is True
