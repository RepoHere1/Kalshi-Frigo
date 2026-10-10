"""
Risk Overlay Modules - Pure sizing and selection overlay logic.

These functions implement invention #12 risk overlays:
- Drawdown-aware Kelly scaling
- Volatility targeting
- Correlation-aware position sizing  
- Conviction-based exit (cut losers, not winners)
- Flow-based veto
- Scenario filters
"""

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class DrawdownFraction:
    """Current drawdown from peak equity."""
    peak: float
    current: float
    
    def fraction(self) -> float:
        """Fractional drawdown (0.0 = at peak, 1.0 = wiped out)."""
        if self.peak <= 0:
            return 1.0
        return max(0.0, (self.peak - self.current) / self.peak)


def kelly_scale_for_drawdown(
    base_fraction: float,
    drawdown: DrawdownFraction,
    peak_floor: float = 0.18,
) -> float:
    """
    Scale Kelly fraction based on drawdown from peak.
    
    When at peak: full base_fraction
    When at peak_floor drawdown: 0.0 (no trading)
    Linear interpolation in between.
    """
    dd = drawdown.fraction()
    if dd >= peak_floor:
        return 0.0
    return base_fraction * (1.0 - dd / peak_floor)


def vol_target_scale(
    realized_vol: float,
    target_vol: float,
    min_scale: float = 0.5,
) -> float:
    """
    Scale position size based on realized vs target volatility.
    
    When realized_vol > target_vol: shrink positions
    When realized_vol <= target_vol: full size
    """
    if realized_vol <= target_vol or target_vol <= 0:
        return 1.0
    return max(min_scale, target_vol / realized_vol)


def correlation_cap(
    side: str,
    book_depths: Dict[str, Tuple[float, float, float]],
    corr_matrix: Dict[str, Dict[str, float]],
    max_book_usd: float = 15.0,
    underlying: Optional[str] = None,
) -> Tuple[float, float]:
    """
    Compute correlation-adjusted capacity for a side.
    
    Args:
        side: 'UP' or 'DOWN'
        book_depths: ticker -> (yes_depth, no_depth, imbalance)
        corr_matrix: ticker -> ticker -> correlation (-1 to 1)
        max_book_usd: maximum total position for the side
        underlying: the ticker we're about to trade
    
    Returns:
        (capacity_usd, room_left_usd)
    """
    if not book_depths:
        return max_book_usd, max_book_usd
    
    # Compute current same-side exposure
    current_exposure = 0.0
    for ticker, (yes_depth, no_depth, _) in book_depths.items():
        if ticker == underlying:
            continue
        # Imbalance: positive = more on 'yes', negative = more on 'no'
        # For UP trades, we buy YES; for DOWN trades, we buy NO
        if side == "UP":
            position = max(0, yes_depth)
        else:
            position = max(0, no_depth)
        current_exposure += position
    
    # Correlation adjustment
    if not underlying or underlying not in corr_matrix:
        # No correlation data: use average
        avg_corr = 0.3
    else:
        corrs = [
            corr_matrix[underlying].get(ticker, 0.3)
            for ticker in book_depths.keys()
            if ticker != underlying
        ]
        avg_corr = sum(corrs) / len(corrs) if corrs else 0.3
    
    # Adjust capacity by correlation
    # Higher correlation with existing positions = less capacity
    room_left = max(0, max_book_usd - current_exposure)
    if avg_corr > 0:
        capacity = room_left * (1.0 - avg_corr * 0.5)
    else:
        capacity = room_left
    
    return max(0.0, capacity), max(0.0, room_left)


def flow_verdict(
    yes_ask: float,
    yes_bid: float,
    no_ask: float,
    no_bid: float,
    max_spread_bps: float = 300.0,
    adverse_flow_threshold: float = 0.15,
) -> Tuple[bool, Optional[float]]:
    """
    Veto entry based on spread width and order flow imbalance.
    
    Returns:
        (passes, size_multiplier) - if not passes, size_multiplier is None
    """
    # Check spread
    yes_spread = yes_ask - yes_bid
    no_spread = no_ask - no_bid
    mid_price = (yes_ask + yes_bid) / 2
    spread_bps = max(yes_spread, no_spread) / mid_price * 10000
    
    if spread_bps > max_spread_bps:
        return False, None
    
    # Check flow imbalance (adverse flow check)
    yes_imbalance = (yes_ask - yes_bid) / (yes_ask + yes_bid) if (yes_ask + yes_bid) > 0 else 0
    no_imbalance = (no_ask - no_bid) / (no_ask + no_bid) if (no_ask + no_bid) > 0 else 0
    
    # If spread is wide in our favor, it might be adverse flow
    if yes_imbalance > adverse_flow_threshold or no_imbalance > adverse_flow_threshold:
        return True, 0.5  # Halve size
    
    return True, 1.0


def collapse_allowance(
    time_left: float,
    half_life: float = 300.0,
) -> float:
    """
    Allowance for conviction drop as expiry approaches.
    
    Returns:
        maximum allowed conviction drop (as fraction)
    """
    # Allow less drop as we get closer to expiry
    ratio = max(0, time_left / half_life)
    return 0.10 * (1.0 + ratio)  # 10% base, up to 20% when far from expiry


def conviction_collapsed(
    original_conviction: float,
    current_conviction: float,
    allowance: float,
) -> bool:
    """
    Check if conviction has collapsed below allowance.
    
    Returns:
        True if conviction dropped more than allowance
    """
    drop = original_conviction - current_conviction
    return drop > allowance


def scenario_verdict(
    market_title: str,
    scenario_scores: Dict[str, float],
    veto_threshold: float = -0.5,
) -> Tuple[bool, Optional[str]]:
    """
    Veto based on scenario analysis scores.
    
    Returns:
        (passes, veto_reason)
    """
    if not scenario_scores:
        return True, None
    
    # Check for negative scenario scores
    for scenario, score in scenario_scores.items():
        if score < veto_threshold:
            return False, f"Scenario '{scenario}' score {score:.2f} < {veto_threshold}"
    
    return True, None


def analog_filter(
    win_rate: Optional[float],
    min_samples: int = 10,
    threshold: float = 0.45,
) -> Tuple[bool, Optional[str]]:
    """
    Filter trades based on historical analog win rates.
    
    Returns:
        (passes, reason)
    """
    if win_rate is None:
        return True, None  # Not enough data, allow
    
    if win_rate < threshold:
        return False, f"Analog win rate {win_rate:.1%} < {threshold:.1%}"
    
    return True, None


def side_spread(
    yes_ask: float,
    yes_bid: float,
    no_ask: float,
    no_bid: float,
    side: str,
) -> float:
    """
    Compute spread for the requested side.
    """
    if side.upper() == "YES":
        return yes_ask - yes_bid
    else:
        return no_ask - no_bid


def halve_signal(
    signal: Dict[str, Any],
    min_contracts: int = 1,
) -> Dict[str, Any]:
    """
    Halve signal size, ensuring at least min_contracts.
    """
    result = signal.copy()
    result['contracts'] = max(min_contracts, signal.get('contracts', 1) // 2)
    
    # Re-price to match new quantity
    if 'yes_ask' in signal and 'no_ask' in signal:
        avg_price = (signal['yes_ask'] + signal['no_ask']) / 2
        result['yes_ask'] = avg_price
        result['no_ask'] = avg_price
    
    return result


def corr_factor(
    position_size: float,
    room_usd: float,
    min_contracts: int = 1,
) -> Tuple[Optional[float], bool]:
    """
    Compute correlation factor to resize into available room.
    
    Returns:
        (factor, refused)
    """
    if room_usd <= 0:
        return None, True
    
    if position_size <= room_usd:
        return 1.0, False
    
    factor = room_usd / position_size
    if factor < 0.1:  # Resize too much, refuse
        return None, True
    
    return factor, False
