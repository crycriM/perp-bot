from dataclasses import dataclass
import math

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
    pricing_model: str = "legacy"  # historical replay only; new runs: glft or fixed
    arrival_rate_per_s: float | None = None  # A per side, quote-lot fills / second
    sigma_bps_sqrt_s: float | None = None  # frozen training estimate, not annual vol
    fixed_half_spread_bps: float = 10.0  # fee-aware experimental baseline
    maker_fee_bps: float = 1.5  # override with actual account tier, rebates negative
    taker_fee_bps: float = 4.5
    min_edge_bps: float = 0.0  # adverse-selection allowance + required margin
    quote_reprice_bps: float = 2.0
    quote_post_only_buffer_bps: float = 0.0
    cancel_latency_s: float = 0.0  # replay: use measured p95, not these legacy defaults
    place_latency_s: float = 0.0
    close_slippage_bps: float = 2.0
    widen_factor: float = 2.0   # half-spread multiplier on WIDEN
    exchange: str = "hyperliquid"  # hb-enhanced-opms/AccountRegistry/AdapterRegistry routing id;
                                    # also the ExecIntent.venue and PnLLedger.venue tag
    account_id: str = "default"
    position_mode: str | None = None
    funding_interval_s: float = 3600.0  # HL funding cadence; ledger accrues pro-rata
    target_inventory: float = 0.0  # structural tilt, e.g. one leg of a cross-hedged subaccount pair
    leverage: int = 1  # user-selected perp leverage; used by execution and margin sizing
    quote_size: float | None = None  # explicit venue-quantized base size; defaults to 10% of cap
    price_tick: float | None = None  # venue tick used by replay/live intent prices
    size_step: float | None = None
    max_market_data_age_s: float | None = None
    min_quote_notional: float = 0.0  # venue min order value (quote ccy); WIDEN never halves below it
    caps: Caps = None
    gate: GateConfig = None
    risk: RiskConfig = None

    def __post_init__(self):
        if self.pricing_model not in {"legacy", "glft", "fixed"}:
            raise ValueError("unknown pricing_model")
        if self.pricing_model == "glft":
            required = (self.arrival_rate_per_s, self.sigma_bps_sqrt_s, self.quote_size)
            if any(x is None or not math.isfinite(x) or x <= 0 for x in required):
                raise ValueError("GLFT requires calibrated A, sigma and explicit quote_size")
        for name in ("gamma", "kappa", "fixed_half_spread_bps", "quote_reprice_bps"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("min_edge_bps", "quote_post_only_buffer_bps", "cancel_latency_s",
                     "place_latency_s", "close_slippage_bps", "min_quote_notional"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if not all(math.isfinite(x) for x in (self.maker_fee_bps, self.taker_fee_bps)):
            raise ValueError("fees must be finite")
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
        if not (math.isfinite(self.caps.max_position) and 0 < self.caps.max_position
                <= self.caps.critical_position < math.inf):
            raise ValueError("caps must satisfy 0 < max_position <= critical_position (finite)")
        if self.quote_size is not None and self.quote_size <= 0:
            raise ValueError("quote_size must be positive")
        if self.price_tick is not None and self.price_tick <= 0:
            raise ValueError("price_tick must be positive")
        for name in ("size_step", "max_market_data_age_s"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be finite and positive")
        if self.gate is None:
            self.gate = GateConfig()
        if self.risk is None:
            self.risk = RiskConfig(gate=self.gate)
