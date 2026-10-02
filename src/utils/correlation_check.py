"""
Correlation checker for market sector exposure.

This module prevents overexposure by checking for correlated positions
across different markets within the same sector.
"""
from typing import Dict, Any, List
from src.utils.database import DatabaseManager
from src.utils.logging_setup import get_trading_logger


class CorrelationChecker:
    """
    Checks for correlation limits across market sectors to prevent overexposure.
    
    Monitors sector exposure by checking both market category correlations
    and position counts per sector.
    """

    @staticmethod
    async def check_correlation_limits(
        market_id: str,
        market_category: str,
        max_correlation_threshold: float = 0.7,
        max_positions_per_sector: int = 3,
    ) -> Dict[str, Any]:
        """
        Check correlation limits for a proposed market trade.
        
        Args:
            market_id: The Kalshi market ID to check
            market_category: The category of the market (e.g., "sports", "economics")
            max_correlation_threshold: Maximum correlation coefficient allowed (0-1)
            max_positions_per_sector: Maximum positions allowed per sector
            
        Returns:
            Dictionary with correlation check results:
            {
                "correlation_score": float,
                "positions_in_sector": int,
                "exceeds_limit": bool,
                "reason": str
            }
        """
        logger = get_trading_logger("correlation_checker")
        
        try:
            result = await CorrelationChecker._calculate_correlation_risk(
                market_id,
                market_category,
                max_correlation_threshold,
                max_positions_per_sector
            )
            
            logger.info(
                f"Correlation check result for {market_id}: {result['reason']}",
                correlation_score=result.get('correlation_score', 0),
                positions_in_sector=result.get('positions_in_sector', 0)
            )
            
            return result
            
        except Exception as e:
            logger.error(
                f"Error checking correlation limits for {market_id}: {e}",
                exc_info=True
            )
            
            # Return safe default that skips the trade
            return {
                "correlation_score": 1.0,
                "positions_in_sector": max_positions_per_sector + 1,
                "exceeds_limit": True,
                "reason": f"Error during correlation check: {str(e)}"
            }

    @staticmethod
    async def _calculate_correlation_risk(
        market_id: str,
        market_category: str,
        max_correlation_threshold: float,
        max_positions_per_sector: int,
    ) -> Dict[str, Any]:
        """
        Calculate correlation risk for a market position.
        
        This is a simplified implementation that checks:
        1. Number of existing positions in the same sector
        2. Correlation score (simplified for now)
        
        In production, this would use historical price data and correlation
        analysis to identify truly correlated markets.
        """
        # Get existing positions from database
        db_manager = DatabaseManager()
        
        # Get all open live positions
        positions = await db_manager.get_open_live_positions()
        
        # Count positions in the same sector
        positions_in_sector = 0
        for position in positions:
            # Get market data to find category
            try:
                market_data = await db_manager.get_market_data(position.market_id)
                if market_data and market_data.get("category") == market_category:
                    positions_in_sector += 1
            except Exception:
                # If we can't get market data, continue with count
                continue
        
        # Calculate correlation score (simplified)
        # In production, this would calculate actual correlation from historical data
        correlation_score = CorrelationChecker._estimate_correlation(
            market_id, market_category, positions_in_sector
        )
        
        # Check if we exceed limits
        exceeds_limit = (
            correlation_score >= max_correlation_threshold or
            positions_in_sector >= max_positions_per_sector
        )
        
        reason = ""
        if exceeds_limit:
            if correlation_score >= max_correlation_threshold:
                reason = f"Correlation score {correlation_score:.2f} exceeds threshold {max_correlation_threshold}"
            else:
                reason = f"Positions in sector ({positions_in_sector}) exceeds limit ({max_positions_per_sector})"
        else:
            reason = "Within correlation and sector limits"
        
        return {
            "correlation_score": correlation_score,
            "positions_in_sector": positions_in_sector,
            "exceeds_limit": exceeds_limit,
            "reason": reason
        }

    @staticmethod
    def _estimate_correlation(
        market_id: str,
        market_category: str,
        positions_in_sector: int,
    ) -> float:
        """
        Estimate correlation score for a market.
        
        Simplified implementation that returns a value based on sector exposure.
        In production, this would use historical price data and correlation analysis.
        """
        # Base correlation based on sector exposure
        base_correlation = min(positions_in_sector * 0.2, 0.8)
        
        # Add some randomness to avoid perfect predictability
        # In production, this would use actual market data analysis
        import random
        randomness = random.uniform(-0.1, 0.1)
        
        # Ensure correlation is within valid range
        correlation_score = max(0.0, min(1.0, base_correlation + randomness))
        
        return correlation_score