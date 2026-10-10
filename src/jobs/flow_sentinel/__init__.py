"""
Order-Flow + Liquidity Sentinel - veto on wide spread or adverse imbalance.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass
class FlowMetrics:
    yes_ask: float
    yes_bid: float
    no_ask: float
    no_bid: float
    yes_depth: int
    no_depth: int


def compute_flow_metrics(
    yes_ask: float,
    yes_bid: float,
    no_ask: float,
    no_bid: float,
    yes_depth: int,
    no_depth: int,
) -> FlowMetrics:
    return FlowMetrics(
        yes_ask=yes_ask, yes_bid=yes_bid,
        no_ask=no_ask, no_bid=no_bid,
        yes_depth=yes_depth, no_depth=no_depth,
    )


def flow_sentinel(
    metrics: FlowMetrics,
    max_spread_bps: float = 200.0,
    max_imbalance: float = 0.4,
) -> Tuple[bool, Optional[str]]:
    """
    Check order flow for adverse conditions.
    
    Returns:
        (passes, reason) - if not passes, reason explains why
    """
    # Check spread
    yes_spread = (metrics.yes_ask - metrics.yes_bid) / metrics.yes_ask * 10000 if metrics.yes_ask > 0 else 0
    no_spread = (metrics.no_ask - metrics.no_bid) / metrics.no_ask * 10000 if metrics.no_ask > 0 else 0
    max_spread = max(yes_spread, no_spread)
    
    if max_spread > max_spread_bps:
        return False, f"Spread {max_spread:.0f}bps exceeds {max_spread_bps:.0f}bps"
    
    # Check imbalance
    total_yes = metrics.yes_ask + metrics.yes_bid
    total_no = metrics.no_ask + metrics.no_bid
    yes_imbalance = abs(metrics.yes_ask - metrics.yes_bid) / total_yes if total_yes > 0 else 0
    no_imbalance = abs(metrics.no_ask - metrics.no_bid) / total_no if total_no > 0 else 0
    max_imbalance = max(yes_imbalance, no_imbalance)
    
    if max_imbalance > max_imbalance:
        return False, f"Imbalance {max_imbalance:.1%} exceeds {max_imbalance:.1%}"
    
    return True, None
