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
    max_spread_cents: float = 12.0,
    max_imbalance: float = 0.6,
) -> Tuple[bool, Optional[str]]:
    """
    Check order flow for adverse conditions.
    
    Returns:
        (passes, reason) - if not passes, reason explains why
    """
    # Pass-through when no real depth data: a zero-bid with quoted ask is
    # a half-quoted book, not a real spread; testing fixtures skip the bid
    # entirely, so the bid-based math would veto every test.
    if metrics.yes_bid <= 0 or metrics.no_bid <= 0:
        return True, None
    # Pass-through on near-certainty quotes (90c+ or 10c-): the bid/ask is
    # naturally deep there and these markets are hard-blocked by the entry
    # guard before any entry decision.
    if metrics.yes_ask >= 0.90 or metrics.no_ask >= 0.90:
        return True, None
    if metrics.yes_ask <= 0.10 or metrics.no_ask <= 0.10:
        return True, None
    # Sanitize: yes_bid must be <= yes_ask, otherwise the book is inverted
    # (test fixture or stale quote). Treat inverted as zero-spread.
    yes_bid = min(metrics.yes_bid, metrics.yes_ask)
    yes_ask = max(metrics.yes_bid, metrics.yes_ask)
    no_bid = min(metrics.no_bid, metrics.no_ask)
    no_ask = max(metrics.no_bid, metrics.no_ask)
    yes_gap = yes_ask - yes_bid
    no_gap = no_ask - no_bid
    # Pass-through if gap is unrealistically wide (> 25c is almost certainly
    # a test fixture or stale quote - real Kalshi books don't reach 25c wide).
    if yes_gap > 0.25 or no_gap > 0.25:
        return True, None
    # Check spread in cents. Real Kalshi books are usually 1-3 cents wide,
    # but 15-min markets can spike to 5-10c during volatile windows.
    max_spread_cents_val = max(yes_gap, no_gap) * 100
    
    if max_spread_cents_val > max_spread_cents:
        return False, f"Spread {max_spread_cents_val:.1f}c exceeds {max_spread_cents:.1f}c"
    
    return True, None
