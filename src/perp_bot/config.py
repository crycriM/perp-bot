from dataclasses import dataclass

from mm_core.inventory import Caps
from mm_core.risk_policy import RiskConfig
from mm_core.regime import GateConfig

@dataclass
class PerpPairConfig:
    """Per-pair configuration for the keeper loop."""
    coin: str
    gamma: float = 0.5
    kappa: float = 0.3
    caps: Caps = None
    gate: GateConfig = None
    risk: RiskConfig = None

    def __post_init__(self):
        if self.caps is None:
            self.caps = Caps(max_position=10.0, critical_position=20.0)
        if self.gate is None:
            self.gate = GateConfig()
        if self.risk is None:
            self.risk = RiskConfig(gate=self.gate)
