import json
import time

import pytest

from mm_core.contracts import ExecIntent
from mm_core.inventory import Caps

from perp_bot.config import PerpPairConfig
from perp_bot.keeper import Keeper
from perp_bot.opms_client import Position


class FakeOpmsClient:
    """Fake OPMS client with canned snapshots + configurable position."""

    def __init__(self, snapshots=None, position=0.0, equity=1000.0):
        self.snapshots = snapshots or []
        self.sends: list[ExecIntent] = []
        self.position = position
        self.equity = equity
        self.resnapshot_calls = 0
        self._on_snapshot_cb = None
        self._on_fill_cb = None
        self._on_error_cb = None

    def on_snapshot(self, cb):
        self._on_snapshot_cb = cb

    def on_fill(self, cb):
        self._on_fill_cb = cb

    def on_error(self, cb):
        self._on_error_cb = cb

    async def start(self):
        for snap in self.snapshots:
            if self._on_snapshot_cb:
                await self._on_snapshot_cb(snap)
        return self

    async def stop(self):
        pass

    async def send_intent(self, intent):
        self.sends.append(intent)

    async def get_positions(self):
        return {
            "BTC": Position(coin="BTC", position=self.position, equity=self.equity),
        }

    async def resnapshot_positions(self):
        self.resnapshot_calls += 1
        return await self.get_positions()


def make_keeper(client, config=None, **kwargs):
    config = config or PerpPairConfig(coin="BTC", gamma=1.0, kappa=0.5)
    keeper = Keeper(client, config, tick_s=0.01, **kwargs)
    client.on_snapshot(keeper._on_snapshot)
    client.on_fill(keeper._on_fill)
    client.on_error(keeper._on_error)
    return keeper


def snapshots(n=20, mid=50000.0, drift=0.0):
    t0 = time.time()
    return [
        {"ts": t0 + i, "mid": mid + i * drift, "funding_rate": None}
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_keeper_normal_quote():
    client = FakeOpmsClient(snapshots(drift=10.0))
    keeper = make_keeper(client)
    await client.start()

    for _ in range(5):
        await keeper._tick()

    assert keeper._equity > 0
    assert keeper._inventory.position == 0
    quote_intents = [s for s in client.sends if s.quote is not None]
    assert quote_intents, "keeper should quote in a calm regime"
    q = quote_intents[-1].quote
    assert q.bid_price < q.ask_price
    assert quote_intents[-1].current_inventory == 0.0


@pytest.mark.asyncio
async def test_keeper_intents_use_configured_exchange_as_venue():
    """ExecIntent.venue must be a real AdapterRegistry/AccountRegistry key
    ("hyperliquid"), not the display label "hl" — OPMS routes strategies by
    this field, so a mismatch would 500 at strategy-creation time."""
    config = PerpPairConfig(
        coin="BTC", gamma=1.0, kappa=0.5, exchange="hyperliquid", account_id="mm-a"
    )
    client = FakeOpmsClient(snapshots(drift=10.0))
    keeper = make_keeper(client, config)
    await client.start()

    for _ in range(5):
        await keeper._tick()

    assert client.sends, "keeper should have sent at least one intent"
    assert all(s.venue == "hyperliquid" for s in client.sends)
    assert all(s.account_id == "mm-a" for s in client.sends)


@pytest.mark.asyncio
async def test_keeper_de_risk_on_critical_inventory():
    config = PerpPairConfig(coin="BTC", gamma=1.0, kappa=0.5)
    config.caps = Caps(max_position=5.0, critical_position=8.0)
    client = FakeOpmsClient(snapshots(), position=9.0)  # OPMS says 9 > critical 8
    keeper = make_keeper(client, config)
    await client.start()

    for _ in range(3):
        await keeper._tick()

    de_risk = [
        s for s in client.sends
        if s.quote is None and s.target_inventory == 0.0 and s.urgency != "emergency"
    ]
    assert de_risk, f"expected a de-risk intent, got {client.sends}"
    assert de_risk[-1].current_inventory == pytest.approx(9.0)


@pytest.mark.asyncio
async def test_keeper_emergency_exit_on_drawdown():
    client = FakeOpmsClient(snapshots(), equity=1000.0)
    keeper = make_keeper(client)
    await client.start()

    keeper._risk._peak_equity = 10000.0

    for _ in range(3):
        await keeper._tick()

    assert any(s.urgency == "emergency" for s in client.sends)


@pytest.mark.asyncio
async def test_keeper_reconciles_inventory_from_opms():
    """Local inventory drift is corrected by the authoritative snapshot."""
    client = FakeOpmsClient(snapshots(), position=2.5)
    keeper = make_keeper(client)
    await client.start()

    keeper._inventory.position = 0.0  # local drifted view
    await keeper._tick()

    assert keeper._inventory.position == pytest.approx(2.5)


@pytest.mark.asyncio
async def test_keeper_resnapshot_on_error_applies_positions():
    client = FakeOpmsClient(snapshots(), position=1.5)
    keeper = make_keeper(client)
    await client.start()

    keeper._inventory.position = 0.0
    await keeper._on_error(RuntimeError("ws dropped"))

    assert client.resnapshot_calls == 1
    assert keeper._inventory.position == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_keeper_on_error_survives_resnapshot_failure():
    """If the server is still down when _on_error fires (the common case
    right after a disconnect), resnapshot_positions() itself raises. This
    must not propagate: _on_error is invoked from inside OpmsClient's own
    except block with no protection there, so an uncaught exception here
    silently kills the whole WS reconnect loop task -- no further reconnect
    attempts ever happen again."""
    client = FakeOpmsClient(snapshots(), position=1.5)

    async def failing_resnapshot():
        client.resnapshot_calls += 1
        raise RuntimeError("Cannot connect to host 127.0.0.1:8000")

    client.resnapshot_positions = failing_resnapshot
    keeper = make_keeper(client)
    await client.start()

    keeper._inventory.position = 0.0
    await keeper._on_error(RuntimeError("ws dropped"))  # must not raise

    assert client.resnapshot_calls == 1
    assert keeper._inventory.position == 0.0  # unchanged: reconciliation was skipped


@pytest.mark.asyncio
async def test_keeper_writes_jsonl_decision_log(tmp_path):
    """Every tick appends one parseable JSON line — the shadow artifact."""
    log_path = tmp_path / "decisions.jsonl"
    client = FakeOpmsClient(snapshots(drift=10.0))
    keeper = make_keeper(client, decision_log_path=str(log_path))
    await client.start()

    for _ in range(3):
        await keeper._tick()
    await keeper.stop()

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 3
    rec = json.loads(lines[0])
    assert {"ts", "mid", "regime", "decision", "urgency",
            "inventory", "equity", "total_pnl"} <= rec.keys()
    assert rec["source"] == "live"
    assert rec["intent_sent"] is True


@pytest.mark.asyncio
async def test_keeper_shadow_mode_logs_without_sending(tmp_path):
    log_path = tmp_path / "shadow-decisions.jsonl"
    client = FakeOpmsClient(snapshots(drift=10.0))
    keeper = make_keeper(client, decision_log_path=str(log_path), shadow_mode=True)
    await client.start()

    for _ in range(3):
        await keeper._tick()
    await keeper.stop()

    assert client.sends == []
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert records
    assert all(rec["source"] == "shadow" for rec in records)
    assert all(rec["intent_sent"] is False for rec in records)
    assert any(rec["intent"] is not None for rec in records)


@pytest.mark.asyncio
async def test_keeper_pnl_explain_tracks_fill_with_mid_at_fill():
    """A fill's mid_at_fill must come from the last snapshot, not the fill
    price itself, so spread capture is measurable."""
    client = FakeOpmsClient(snapshots(mid=50000.0, drift=0.0))
    keeper = make_keeper(client)
    await client.start()
    await keeper._tick()

    await keeper._on_fill({"ts": time.time(), "side": "buy", "price": 49990.0, "size": 1.0})

    b = keeper.pnl_explain()
    assert b.n_fills == 1
    assert b.fills[0].mid_at_fill == pytest.approx(50000.0)
    assert b.spread_capture == pytest.approx(10.0)  # bought 10 below mid
    assert b.position == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_keeper_pnl_explain_reflects_fee():
    client = FakeOpmsClient(snapshots())
    keeper = make_keeper(client)
    await client.start()

    await keeper._on_fill({"ts": time.time(), "side": "buy", "price": 50000.0,
                            "size": 1.0, "fee": 2.5})

    b = keeper.pnl_explain()
    assert b.fee_pnl == pytest.approx(-2.5)


@pytest.mark.asyncio
async def test_keeper_funding_accrues_pro_rata_not_per_tick():
    """funding_rate on every snapshot must accrue proportional to elapsed
    time, not the full rate on every tick (which would massively overcount)."""
    config = PerpPairConfig(coin="BTC", gamma=1.0, kappa=0.5, funding_interval_s=3600.0)
    t0 = time.time()
    snaps = [{"ts": t0 + i, "mid": 50000.0, "funding_rate": 0.0003} for i in range(5)]
    client = FakeOpmsClient(snaps)
    keeper = make_keeper(client, config)

    await keeper._on_fill({"ts": t0, "side": "buy", "price": 50000.0, "size": 1.0})
    await client.start()  # feeds all 5 snapshots through _on_snapshot

    b = keeper.pnl_explain()
    # 4 intervals of 1s each, position=1 long, rate=0.0003 over 3600s cadence
    expected = -1.0 * 50000.0 * 0.0003 * (4 * 1.0 / 3600.0)
    assert b.funding_pnl == pytest.approx(expected, rel=1e-6)


@pytest.mark.asyncio
async def test_keeper_reconciles_pnl_ledger_position_on_drift():
    """A missed fill must not leave the ledger's position silently wrong —
    it gets a synthetic reconciling fill so subsequent PnL math is honest."""
    client = FakeOpmsClient(snapshots(mid=50000.0), position=3.0)
    keeper = make_keeper(client)
    await client.start()  # primes _mid_history

    assert keeper._pnl.position == 0.0  # no fills seen yet
    await keeper._tick()  # fetches OPMS position=3.0, ledger never saw the fill

    assert keeper._pnl.position == pytest.approx(3.0)
    b = keeper.pnl_explain()
    assert any(f.label == "reconcile" for f in b.fills)
