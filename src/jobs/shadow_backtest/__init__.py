"""
Live Shadow Backtester Filter - check historical analogs before executing.
"""

from typing import Any, Dict, List, Tuple


def shadow_backtest(
    ticker: str,
    entry_fair: float,
    entry_price: float,
    all_history: List[Dict[str, Any]],
    min_win_rate: float = 0.57,
    min_payout: float = 2.1,
) -> Tuple[bool, float, float]:
    """
    Check if historical analogs show sufficient win rate and payout.
    
    Returns:
        (passes, win_rate, avg_payout)
    """
    if not all_history:
        return True, 0.0, 0.0  # No data, allow
    
    # Filter similar markets
    similar = [h for h in all_history if _is_similar(h, ticker)]
    
    if len(similar) < 10:
        return True, 0.0, 0.0  # Not enough data
    
    # Compute win rate
    wins = sum(1 for h in similar if h.get("outcome") == "win")
    win_rate = wins / len(similar)
    
    # Compute payout
    payouts = [h.get("payout", 1.0) for h in similar if h.get("payout")]
    avg_payout = sum(payouts) / len(payouts) if payouts else 1.0
    
    passes = win_rate >= min_win_rate and avg_payout >= min_payout
    return passes, win_rate, avg_payout


def _is_similar(h: Dict[str, Any], ticker: str) -> bool:
    """Check if historical market is similar to current."""
    return h.get("category", "") in ticker or h.get("type", "") in ticker
