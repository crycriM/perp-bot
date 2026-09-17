"""
Basket configuration for market-neutral ETH/SOL perp MM on Hyperliquid.

Calibrated parameters from 30-day backtest (15m candles, 2026-07-31 to 2026-08-31):
- ETH: gamma=4.0, kappa=0.5, max_position=1.0, target_inventory=±0.4 (40% tilt)
- SOL: gamma=3.0, kappa=0.7, max_position=10.0, target_inventory=±4.0 (40% tilt)

Subaccount structure:
- basket_a: ETH tilt long (+0.4), SOL tilt short (-4.0)
- basket_b: ETH tilt short (-0.4), SOL tilt long (+4.0)

Collateral sizing for the currently funded accounts:
- The configured target is about $1350 gross per subaccount at current prices.
- At 6x leverage, target initial margin is about $225.
- The rebalancer keeps target margin at or below 83.3% of equity by default.
- The max_position caps remain calibrated inventory bounds; the HB budget
  checker and rebalancer margin guard prevent an underfunded account from
  opening the full caps.
"""

from mm_core.inventory import Caps
from perp_bot.config import PerpPairConfig


def get_basket_configs() -> list[PerpPairConfig]:
    """Return the four PerpPairConfig instances for the ETH/SOL basket."""
    
    # ETH parameters (calibrated)
    eth_gamma = 4.0
    eth_kappa = 0.5
    eth_max_pos = 1.0
    eth_critical = 2.0
    eth_q_star = 0.4  # 40% of max_position
    
    # SOL parameters (calibrated)
    sol_gamma = 3.0
    sol_kappa = 0.7
    sol_max_pos = 10.0
    sol_critical = 20.0
    sol_q_star = 4.0  # 40% of max_position
    basket_leverage = 6  # target margin fits the current ~$300 per account with buffer
    
    configs = [
        # Sub A: ETH long tilt, SOL short tilt
        PerpPairConfig(
            coin="ETH",
            exchange="hyperliquid",
            account_id="basket_a",
            gamma=eth_gamma,
            kappa=eth_kappa,
            target_inventory=eth_q_star,
            leverage=basket_leverage,
            caps=Caps(max_position=eth_max_pos, critical_position=eth_critical),
        ),
        PerpPairConfig(
            coin="SOL",
            exchange="hyperliquid",
            account_id="basket_a",
            gamma=sol_gamma,
            kappa=sol_kappa,
            target_inventory=-sol_q_star,
            leverage=basket_leverage,
            caps=Caps(max_position=sol_max_pos, critical_position=sol_critical),
        ),
        # Sub B: ETH short tilt, SOL long tilt (mirror)
        PerpPairConfig(
            coin="ETH",
            exchange="hyperliquid",
            account_id="basket_b",
            gamma=eth_gamma,
            kappa=eth_kappa,
            target_inventory=-eth_q_star,
            leverage=basket_leverage,
            caps=Caps(max_position=eth_max_pos, critical_position=eth_critical),
        ),
        PerpPairConfig(
            coin="SOL",
            exchange="hyperliquid",
            account_id="basket_b",
            gamma=sol_gamma,
            kappa=sol_kappa,
            target_inventory=sol_q_star,
            leverage=basket_leverage,
            caps=Caps(max_position=sol_max_pos, critical_position=sol_critical),
        ),
    ]
    
    return configs


def print_basket_summary():
    """Print a summary of the basket configuration."""
    configs = get_basket_configs()
    
    print("=== Market-Neutral ETH/SOL Basket Configuration ===\n")
    print("Subaccount topology:")
    print("  basket_a: ETH tilt LONG (+0.4), SOL tilt SHORT (-4.0)")
    print("  basket_b: ETH tilt SHORT (-0.4), SOL tilt LONG (+4.0)")
    print()
    print("Per-pair parameters:")
    for cfg in configs:
        print(f"  {cfg.account_id}/{cfg.coin}:")
        print(f"    gamma={cfg.gamma}, kappa={cfg.kappa}")
        print(f"    target_inventory={cfg.target_inventory:+.1f}")
        print(f"    max_position={cfg.caps.max_position}, critical_position={cfg.caps.critical_position}")
    print()
    print("Collateral sizing (per subaccount, cross margin @ 6x leverage):")
    print("  Current target gross notional: ~$1350")
    print("  Target initial margin: ~$225")
    print("  Current funding: ~$300 USDC per subaccount")
    print("  Target margin utilization: ~75%")
    print()
    print("Rebalancing controller:")
    print("  imbalance_pct_threshold: 18%")
    print("  portfolio_net_threshold: 5% of max_position")
    print("  cycle_interval_s: 600 (10 minutes)")


if __name__ == "__main__":
    print_basket_summary()
