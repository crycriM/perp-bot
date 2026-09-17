"""Live Hyperliquid feeds for `BasketRebalancer`.

The providers only touch the injected info object's methods, so these tests
run with a dict-backed stub — no SDK, no network.
"""

import asyncio

import pytest

from perp_bot.rebalancer_feeds import hl_position_provider, hl_price_provider


class _FakeInfo:
    def __init__(self, perp: dict, spot: dict, mids: dict):
        self._perp = perp
        self._spot = spot
        self._mids = mids
        self.calls: list = []

    def user_state(self, address: str) -> dict:
        self.calls.append(("user_state", address))
        return self._perp[address]

    def spot_user_state(self, address: str) -> dict:
        self.calls.append(("spot_user_state", address))
        return self._spot[address]

    def all_mids(self) -> dict:
        self.calls.append(("all_mids",))
        return self._mids


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _perp(*positions: tuple[str, str]) -> dict:
    return {
        "assetPositions": [
            {"position": {"coin": coin, "szi": szi}} for coin, szi in positions
        ],
        "marginSummary": {"accountValue": "0"},
    }


def _spot(total: str) -> dict:
    return {"balances": [{"coin": "USDC", "total": total}, {"coin": "ETH", "total": "1.0"}]}


class TestHlPositionProvider:
    def test_reads_signed_size_and_unified_equity(self):
        info = _FakeInfo(
            {"0xmm1": _perp(("ETH", "0.012"))}, {"0xmm1": _spot("299.85")}, {},
        )
        provider = hl_position_provider(info, {"e2_mm1": "0xmm1"})
        position, equity = _run(provider("e2_mm1", "ETH"))
        assert position == 0.012
        assert equity == 299.85

    def test_short_position_is_negative(self):
        info = _FakeInfo(
            {"0xmm1": _perp(("ETH", "-0.03"))}, {"0xmm1": _spot("300")}, {},
        )
        provider = hl_position_provider(info, {"e2_mm1": "0xmm1"})
        assert _run(provider("e2_mm1", "ETH"))[0] == -0.03

    def test_missing_coin_is_flat_not_an_error(self):
        info = _FakeInfo(
            {"0xmm1": _perp(("SOL", "4.0"))}, {"0xmm1": _spot("300")}, {},
        )
        provider = hl_position_provider(info, {"e2_mm1": "0xmm1"})
        assert _run(provider("e2_mm1", "ETH"))[0] == 0.0

    def test_reads_the_address_bound_to_the_account(self):
        info = _FakeInfo(
            {"0xmm1": _perp(("ETH", "0.012")), "0xmm2": _perp(("ETH", "-0.4"))},
            {"0xmm1": _spot("300"), "0xmm2": _spot("301")},
            {},
        )
        provider = hl_position_provider(info, {"e2_mm1": "0xmm1", "e2_mm2": "0xmm2"})
        assert _run(provider("e2_mm2", "ETH")) == (-0.4, 301.0)
        assert ("user_state", "0xmm2") in info.calls

    def test_unknown_account_fails_loudly(self):
        info = _FakeInfo({}, {}, {})
        provider = hl_position_provider(info, {"e2_mm1": "0xmm1"})
        with pytest.raises(KeyError, match="e2_unknown"):
            _run(provider("e2_unknown", "ETH"))


class TestHlPriceProvider:
    def test_reads_mid(self):
        info = _FakeInfo({}, {}, {"ETH": "2413.55", "SOL": "150.2"})
        provider = hl_price_provider(info)
        assert _run(provider("ETH")) == 2413.55
        assert _run(provider("SOL")) == 150.2

    def test_missing_mid_fails_loudly(self):
        info = _FakeInfo({}, {}, {"SOL": "150.2"})
        provider = hl_price_provider(info)
        with pytest.raises(ValueError, match="ETH"):
            _run(provider("ETH"))
