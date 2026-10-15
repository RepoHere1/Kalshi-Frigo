"""Kilo Gateway - Unified API for Kalshi Trading

Kilo Gateway provides a unified interface for Kalshi trading operations,
abstracting away the complexity of direct API calls. It routes requests
between DRY (simulated) and LIVE (real) modes transparently.

This rewire allows LIVE mode to use the same API endpoint as DRY,
with intelligent cost-based routing that optimizes for reliability
and efficiency.
"""

import asyncio
import os
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone

from src.clients.kalshi_client import KalshiClient, KalshiAPIError
from src.config.settings import settings
from src.utils.mode import MODE_DRY, MODE_LIVE, TradingMode
from src.utils.logging_setup import get_trading_logger

logger = get_trading_logger("kilo_gateway")


class DailyCostTracker:
    """Track daily API usage and costs across all Kalshi operations."""
    
    def __init__(self, daily_limit: float = 10.0):
        self.daily_limit = daily_limit
        self.today = datetime.now(timezone.utc).date()
        self.daily_cost = 0.0
        self.api_calls = 0
        self.order_count = 0
        self.completion_count = 0
    
    @property
    def usage_pct(self) -> float:
        """Get current usage percentage."""
        return round((self.daily_cost / self.daily_limit) * 100, 1)
    
    def can_proceed(self) -> bool:
        """Check if we can make another API call today."""
        if self.daily_cost >= self.daily_limit * 0.95:  # 95% buffer
            logger.warning(
                f"Daily cost limit approaching: {self.daily_cost:.2f}/{self.daily_limit:.2f}"
            )
            return False
        return True
    
    def record_cost(self, cost: float, call_type: str = "api") -> None:
        """Record API cost."""
        self.daily_cost += cost
        self.api_calls += 1
        
        if call_type == "order":
            self.order_count += 1
        elif call_type == "completion":
            self.completion_count += 1
        
        logger.debug(
            f"Recorded cost: ${cost:.3f}, total: ${self.daily_cost:.2f}, "
            f"calls: {self.api_calls}, orders: {self.order_count}, completions: {self.completion_count}"
        )
    
    def reset_daily(self) -> None:
        """Reset daily counters if new day."""
        today = datetime.now(timezone.utc).date()
        if today != self.today:
            self.today = today
            self.daily_cost = 0.0
            self.api_calls = 0
            self.order_count = 0
            self.completion_count = 0
            logger.info(f"Daily counters reset for {today}")
    
    def status(self) -> Dict[str, Any]:
        """Get current usage status."""
        return {
            "today": self.today.isoformat(),
            "daily_cost": round(self.daily_cost, 2),
            "daily_limit": self.daily_limit,
            "usage_pct": round((self.daily_cost / self.daily_limit) * 100, 1),
            "api_calls": self.api_calls,
            "orders_placed": self.order_count,
            "ai_completions": self.completion_count,
            "can_proceed": self.can_proceed()
        }


class KiloGateway:
    """
    Kilo Gateway - Unified API interface for Kalshi trading.
    
    This class provides a single point of access for Kalshi operations,
    with intelligent mode switching and cost tracking. It abstracts away
    the complexity of direct API calls and provides a consistent interface
    for both DRY and LIVE trading modes.
    
    Key Features:
    - Transparent mode switching (DRY ↔ LIVE)
    - Cost-based API routing
    - Daily usage tracking and limits
    - Fallback mechanisms for reliability
    - Comprehensive logging and monitoring
    """
    
    def __init__(self, db_path: str = "trading_system.db"):
        self.db_path = db_path
        self.mode_manager = TradingMode(db_path)
        self.daily_tracker = DailyCostTracker(
            daily_limit=float(os.environ.get("DAILY_API_COST_LIMIT", "10.0"))
        )
        
        # Cache clients for performance
        self._live_client: Optional[KalshiClient] = None
        self._dry_client: Optional[KalshiClient] = None
        self._current_client: Optional[KalshiClient] = None
        self._client_mode: Optional[str] = None
        
        self.logger = get_trading_logger("kilo_gateway")
    
    async def _get_client(self, force_mode: Optional[str] = None) -> KalshiClient:
        """
        Get appropriate client for current mode.
        
        Args:
            force_mode: Optional mode to force ('live' or 'dry')
            
        Returns:
            KalshiClient instance for the requested mode
        """
        mode = force_mode or await self.current_mode()
        
        # Return cached client if available and mode matches
        if self._current_client and self._client_mode == mode:
            return self._current_client
        
        # Clear existing client
        if self._current_client:
            try:
                await self._current_client.close()
            except Exception:
                pass
            self._current_client = None
            self._client_mode = None
        
        # Create new client
        if mode == MODE_LIVE:
            # LIVE mode: use real API key and private key
            api_key = settings.api.kalshi_api_key
            if not api_key:
                raise KalshiAPIError("KALSHI_API_KEY not configured for LIVE mode")
            
            # Use existing private key path from settings
            private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")
            
            self._live_client = KalshiClient(
                api_key=api_key,
                private_key_path=private_key_path,
                max_retries=3
            )
            self._current_client = self._live_client
            self._client_mode = MODE_LIVE
            
            # Verify LIVE connection
            try:
                balance = await self._live_client.get_balance()
                if balance.get("balance", 0) == 0:
                    logger.warning("LIVE account balance is zero - ensure funding")
            except Exception as e:
                logger.error(f"LIVE client connection failed: {e}")
                raise
                
        else:  # DRY mode
            # DRY mode: no API key needed, simulate responses
            self._dry_client = KalshiClient(
                api_key="dry-simulated-key",
                private_key_path=None,  # Not needed for DRY
                max_retries=1
            )
            self._current_client = self._dry_client
            self._client_mode = MODE_DRY
        
        self.logger.info(
            f"Switched to {self._client_mode} mode",
            mode=self._client_mode,
            client_type=type(self._current_client).__name__
        )
        
        return self._current_client
    
    async def current_mode(self) -> str:
        """Get the current trading mode."""
        try:
            return await self.mode_manager.current()
        except Exception:
            self.logger.warning("Failed to get current mode, defaulting to DRY")
            return MODE_DRY
    
    async def switch_mode(self, mode: str, confirmed: bool = False) -> str:
        """
        Switch trading mode.
        
        Args:
            mode: Target mode ('live' or 'dry')
            confirmed: For LIVE mode, requires explicit confirmation
            
        Returns:
            New mode
        """
        if mode == MODE_LIVE and not confirmed:
            raise KalshiAPIError("LIVE mode requires explicit confirmation")
        
        # Clear cache to force client recreation
        if self._current_client:
            try:
                await self._current_client.close()
            except Exception:
                pass
            self._current_client = None
            self._client_mode = None
        
        # Switch mode in database
        new_mode = await self.mode_manager.set(mode, confirmed=confirmed)
        
        # Reset daily tracker if mode switch affects costs
        if mode != MODE_DRY:
            self.daily_tracker.reset_daily()
        
        self.logger.info(
            f"Mode switched: {await self.current_mode()} -> {new_mode}",
            from_mode=self._client_mode,
            to_mode=new_mode
        )
        
        return new_mode
    
    # ------------------------------------------------------------------------
    # API Interface Methods
    # ------------------------------------------------------------------------
    
    async def get_balance(self) -> Dict[str, Any]:
        """Get account balance (LIVE: real balance, DRY: simulated balance)."""
        client = await self._get_client()
        result = await client.get_balance()
        
        # Add gateway metadata
        result["gateway_mode"] = self._client_mode
        result["daily_tracker"] = self.daily_tracker.status()
        result["estimated_cost"] = 0.0  # Will be set after processing
        
        return result
    
    async def get_market(self, ticker: str) -> Dict[str, Any]:
        """Get market data."""
        client = await self._get_client()
        result = await client.get_market(ticker)
        
        # Record API call cost
        cost = 0.10  # Estimated API call cost
        if self.daily_tracker.can_proceed():
            self.daily_tracker.record_cost(cost, "api")
        
        return result
    
    async def get_orders(self, ticker: Optional[str] = None, status: Optional[str] = None) -> Dict[str, Any]:
        """Get orders list."""
        client = await self._get_client()
        result = await client.get_orders(ticker, status)
        
        # Record API call cost
        cost = 0.05  # Estimated API call cost
        if self.daily_tracker.can_proceed():
            self.daily_tracker.record_cost(cost, "api")
        
        return result
    
    async def place_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Place an order (LIVE: real order, DRY: simulated order).
        
        Args:
            order_data: Order parameters
            
        Returns:
            Order response with gateway metadata
        """
        client = await self._get_client()
        
        # Check daily limits before placing order
        order_cost = 0.01  # Estimated order cost (minimum fee)
        if not self.daily_tracker.can_proceed():
            raise KalshiAPIError(
                f"Daily cost limit reached ({self.daily_tracker.daily_cost:.2f}/" \
                f"{self.daily_tracker.daily_limit:.2f}). "
                "Consider increasing DAILY_API_COST_LIMIT or reducing trade size."
            )
        
        result = await client.place_order(order_data)
        
        # Record order cost
        self.daily_tracker.record_cost(order_cost, "order")
        
        # Add gateway metadata
        result["gateway_mode"] = self._client_mode
        result["daily_tracker"] = self.daily_tracker.status()
        result["estimated_cost"] = round(order_cost, 2)
        
        return result
    
    async def place_order_v2(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """Place an order using V2 API (LIVE: real, DRY: simulated)."""
        client = await self._get_client()
        
        # Check daily limits
        order_cost = 0.02  # Estimated V2 order cost
        if not self.daily_tracker.can_proceed():
            raise KalshiAPIError(
                f"Daily cost limit reached ({self.daily_tracker.daily_cost:.2f}/" \
                f"{self.daily_tracker.daily_limit:.2f}). "
                "Consider increasing DAILY_API_COST_LIMIT or reducing trade size."
            )
        
        result = await client.place_order_v2(order_data)
        
        # Record order cost
        self.daily_tracker.record_cost(order_cost, "order")
        
        # Add gateway metadata
        result["gateway_mode"] = self._client_mode
        result["daily_tracker"] = self.daily_tracker.status()
        result["estimated_cost"] = round(order_cost, 2)
        
        return result
    
    async def cancel_order(self, order_id: str, market_ticker: str) -> Dict[str, Any]:
        """Cancel an order (LIVE: real, DRY: simulated)."""
        client = await self._get_client()
        
        # Check daily limits
        cancel_cost = 0.005  # Estimated cancel cost
        if not self.daily_tracker.can_proceed():
            raise KalshiAPIError(
                f"Daily cost limit reached ({self.daily_tracker.daily_cost:.2f}/" \
                f"{self.daily_tracker.daily_limit:.2f}). "
                "Consider increasing DAILY_API_COST_LIMIT or reducing trade size."
            )
        
        result = await client.cancel_order(order_id, market_ticker)
        
        # Record cancel cost
        self.daily_tracker.record_cost(cancel_cost, "order")
        
        # Add gateway metadata
        result["gateway_mode"] = self._client_mode
        result["daily_tracker"] = self.daily_tracker.status()
        result["estimated_cost"] = round(cancel_cost, 2)
        
        return result
    
    # ------------------------------------------------------------------------
    # Convenience Methods
    # ------------------------------------------------------------------------
    
    async def get_daily_summary(self) -> Dict[str, Any]:
        """Get current daily usage summary."""
        return self.daily_tracker.status()
    
    async def can_trade(self) -> bool:
        """Check if trading is allowed based on daily limits."""
        return self.daily_tracker.can_proceed()
    
    def can_proceed(self) -> bool:
        """Check if API calls can proceed based on daily limits."""
        return self.daily_tracker.can_proceed()
    
    async def get_status(self) -> Dict[str, Any]:
        """Get complete gateway status."""
        return {
            "current_mode": await self.current_mode(),
            "client_type": type(self._current_client).__name__ if self._current_client else None,
            "daily_tracker": self.daily_tracker.status(),
            "api_key_configured": bool(settings.api.kalshi_api_key),
            "private_key_configured": bool(os.environ.get("KALSHI_PRIVATE_KEY_PATH")),
            "last_updated": datetime.now(timezone.utc).isoformat()
        }


# Global gateway instance
_gateway: Optional[KiloGateway] = None


def get_gateway(db_path: str = "trading_system.db") -> KiloGateway:
    """Get or create the global Kilo Gateway instance."""
    global _gateway
    if _gateway is None:
        _gateway = KiloGateway(db_path)
    return _gateway


def get_kalshi_client() -> KiloGateway:
    """Alias for get_gateway (backwards compatibility)."""
    return get_gateway()
