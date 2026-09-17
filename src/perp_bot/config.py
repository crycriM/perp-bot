from dataclasses import dataclass

from mm_core.inventory import Caps
from mm_core.risk_policy import RiskConfig
from mm_core.regime import GateConfig

from perp_bot.venue_capabilities import get_venue_capabilities

@dataclass
class PerpPairConfig:
    """Per-pair configuration for the keeper loop."""
    coin: str
    gamma: float = 0.5
    kappa: float = 0.3
    widen_factor: float = 2.0   # half-spread multiplier on WIDEN
    exchange: str = "hyperliquid"  # hb-enhanced-opms/AccountRegistry/AdapterRegistry routing id;
                                    # also the ExecIntent.venue and PnLLedger.venue tag
    account_id: str = "default"
    position_mode: str | None = None
    funding_interval_s: float = 3600.0  # HL funding cadence; ledger accrues pro-rata
    target_inventory: float = 0.0  # structural tilt, e.g. one leg of a cross-hedged subaccount pair
    leverage: int = 1  # user-selected perp leverage; used by execution and margin sizing
    caps: Caps = None
    gate: GateConfig = None
    risk: RiskConfig = None

    def __post_init__(self):
        caps = get_venue_capabilities(self.exchange)
        if self.position_mode is None:
            self.position_mode = caps.position_mode
        elif self.position_mode != caps.position_mode:
            raise ValueError(
                f"{self.exchange} uses position_mode='{caps.position_mode}', "
                f"not '{self.position_mode}'"
            )
        if self.caps is None:
            self.caps = Caps(max_position=10.0, critical_position=20.0)
        if self.gate is None:
            self.gate = GateConfig()
        if self.risk is None:
            self.risk = RiskConfig(gate=self.gate)
