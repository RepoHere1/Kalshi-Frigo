"""
Position limits manager for enforcing trading limits.

This module manages and enforces position limits to prevent overexposure
and ensure disciplined trading within risk parameters.
"""
from typing import Dict, Any
from src.utils.database import DatabaseManager
from src.utils.logging_setup import get_trading_logger
from src.clients.kalshi_client import KalshiClient


class PositionLimitsManager:
    """
    Manages position limits to prevent overexposure and ensure disciplined trading.
    
    Tracks position count, sector exposure, and value limits to ensure trading
    remains within established risk parameters.
    """

    def __init__(self, db_manager: DatabaseManager, kalshi_client: KalshiClient):
        self.db_manager = db_manager
        self.kalshi_client = kalshi_client
        self.logger = get_trading_logger("position_limits")

    async def _get_position_count(self) -> int:
        """Get the current number of open positions."""
        try:
            positions = await self.db_manager.get_open_live_positions()
            return len(positions)
        except Exception as e:
            self.logger.error(f"Error getting position count: {e}")
            return 0

    async def check_position_limits(self) -> Dict[str, Any]:
        """
        Check if current position count is within limits.
        
        Returns:
            Dictionary with limit check results:
            {
                "within_limit": bool,
                "current_count": int,
                "max_allowed": int,
                "limit_reached": bool
            }
        """
        try:
            current_count = await self._get_position_count()
            max_allowed = settings.trading.max_positions
            
            result = {
                "within_limit": current_count < max_allowed,
                "current_count": current_count,
                "max_allowed": max_allowed,
                "limit_reached": current_count >= max_allowed
            }
            
            if not result["within_limit"]:
                self.logger.warning(
                    f"Position limit reached: {current_count}/{max_allowed} positions"
                )
                
            return result
            
        except Exception as e:
            self.logger.error(f"Error checking position limits: {e}")
            return {
                "within_limit": False,
                "current_count": 999999,  # Assume limit exceeded on error
                "max_allowed": max_allowed,
                "limit_reached": True,
                "error": str(e)
            }

    async def can_add_position(self, position_value: float) -> bool:
        """
        Check if a position can be added based on current limits.
        
        Args:
            position_value: Value of the proposed position in dollars
            
        Returns:
            True if position can be added, False otherwise
        """
        try:
            # Check position count
            limits_check = await self.check_position_limits()
            if not limits_check["within_limit"]:
                self.logger.info(
                    f"Cannot add position: position limit reached "
                    f"({limits_check['current_count']}/{limits_check['max_allowed']})"
                )
                return False
                
            # Check position value (can be enhanced later with specific value limits)
            # For now, just check if we have enough available balance
            balance_response = await self.kalshi_client.get_balance()
            available_balance = balance_response.get("balance", 0) / 100
            
            if position_value > available_balance:
                self.logger.info(
                    f"Cannot add position: insufficient balance "
                    f"(${position_value:.2f} > ${available_balance:.2f})"
                )
                return False
                
            self.logger.debug(
                f"Position can be added: within limits "
                f"(value: ${position_value:.2f}, balance: ${available_balance:.2f})"
            )
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error checking if position can be added: {e}")
            return False

    async def get_position_summary(self) -> Dict[str, Any]:
        """
        Get a summary of current position status.
        
        Returns:
            Dictionary with position summary:
            {
                "current_positions": int,
                "max_positions": int,
                "positions_under_limit": bool,
                "position_limit_percent": float
            }
        """
        try:
            current_count = await self._get_position_count()
            max_allowed = settings.trading.max_positions
            
            if max_allowed > 0:
                position_limit_percent = (current_count / max_allowed) * 100
            else:
                position_limit_percent = 0.0
                
            return {
                "current_positions": current_count,
                "max_positions": max_allowed,
                "positions_under_limit": current_count < max_allowed,
                "position_limit_percent": position_limit_percent
            }
            
        except Exception as e:
            self.logger.error(f"Error getting position summary: {e}")
            return {
                "current_positions": 0,
                "max_positions": max_allowed,
                "positions_under_limit": True,
                "position_limit_percent": 0.0,
                "error": str(e)
            }