"""
Kalshi API client for trading operations.
Handles authentication, market data, and trade execution.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Union
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from src.config.settings import settings
from src.utils.logging_setup import TradingLoggerMixin


class KalshiAPIError(Exception):
    """Custom exception for Kalshi API errors."""
    pass




def _as_float_safe(value: Any) -> float:
    """Fixed-point strings ("9.40") and numbers both become a float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class KalshiClient(TradingLoggerMixin):
    """
    Kalshi API client for automated trading.
    Handles authentication, market data retrieval, and trade execution.
    """
    
    def __init__(
        self, 
        api_key: Optional[str] = None, 
        private_key_path: str = None,
        max_retries: int = 5,
        backoff_factor: float = 0.5
    ):
        """
        Initialize Kalshi client.
        
        Args:
            api_key: Kalshi API key (Key ID from the API key generation)
            private_key_path: Path to private key file
            max_retries: Maximum number of retries for failed requests
            backoff_factor: Factor for exponential backoff
        """
        self.api_key = api_key or settings.api.kalshi_api_key
        self.base_url = settings.api.kalshi_base_url
        self.private_key_path = private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")
        self.private_key = None
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        
        # Load private key
        self._load_private_key()
        
        # HTTP client with timeouts
        self.client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)
        )
        
        self.logger.info("Kalshi client initialized", api_key_length=len(self.api_key) if self.api_key else 0)
    
    def _load_private_key(self) -> None:
        """Load private key from file."""
        try:
            private_key_path = Path(self.private_key_path)
            if not private_key_path.exists():
                raise KalshiAPIError(f"Private key file not found: {self.private_key_path}")
            
            with open(private_key_path, 'rb') as f:
                self.private_key = serialization.load_pem_private_key(
                    f.read(),
                    password=None
                )
            self.logger.info("Private key loaded successfully")
        except Exception as e:
            self.logger.error("Failed to load private key", error=str(e))
            raise KalshiAPIError(f"Failed to load private key: {e}")
    
    def _sign_request(self, timestamp: str, method: str, path: str) -> str:
        """
        Sign request using RSA PSS signing method as per Kalshi API docs.
        
        Args:
            timestamp: Request timestamp in milliseconds
            method: HTTP method
            path: Request path
        
        Returns:
            Base64 encoded signature
        """
        # Create message to sign: timestamp + method + path
        message = timestamp + method.upper() + path
        message_bytes = message.encode('utf-8')
        
        try:
            # Sign using RSA PSS as per Kalshi documentation
            signature = self.private_key.sign(
                message_bytes,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH
                ),
                hashes.SHA256()
            )
            
            return base64.b64encode(signature).decode('utf-8')
        except Exception as e:
            self.logger.error("Failed to sign request", error=str(e))
            raise KalshiAPIError(f"Failed to sign request: {e}")
    
    async def _make_authenticated_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        require_auth: bool = True
    ) -> Dict[str, Any]:
        """
        Make authenticated request to Kalshi API with retry logic.
        
        Args:
            method: HTTP method
            endpoint: API endpoint
            params: Query parameters
            json_data: JSON request body
            require_auth: Whether authentication is required
        
        Returns:
            API response data
        """
        # Prepare request
        url = f"{self.base_url}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        # Add authentication headers if required
        if require_auth:
            # Get current timestamp in milliseconds
            timestamp = str(int(time.time() * 1000))
            
            # Create signature
            signature = self._sign_request(timestamp, method, endpoint)
            
            headers.update({
                "KALSHI-ACCESS-KEY": self.api_key,
                "KALSHI-ACCESS-TIMESTAMP": timestamp,
                "KALSHI-ACCESS-SIGNATURE": signature
            })
        
        # Prepare body
        body = None
        if json_data:
            body = json.dumps(json_data, separators=(',', ':'))
        
        # Add query parameters to URL if present
        if params:
            query_string = urlencode(params)
            url = f"{url}?{query_string}"
        
        last_exception = None
        for attempt in range(self.max_retries):
            try:
                self.logger.debug(
                    "Making API request",
                    method=method,
                    endpoint=endpoint,
                    has_auth=require_auth,
                    attempt=attempt + 1
                )
                
                # Rate limit delay to prevent 429s (200ms = 5 req/s)
                await asyncio.sleep(0.2)
                
                response = await self.client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    content=body if body else None
                )
                
                response.raise_for_status()
                return response.json()
                
            except httpx.HTTPStatusError as e:
                last_exception = e
                # Rate limit (429) or server errors (5xx) are worth retrying
                if e.response.status_code == 429 or e.response.status_code >= 500:
                    sleep_time = self.backoff_factor * (2 ** attempt)
                    self.logger.warning(
                        f"API request failed with status {e.response.status_code}. Retrying in {sleep_time:.2f}s...",
                        endpoint=endpoint,
                        attempt=attempt + 1
                    )
                    await asyncio.sleep(sleep_time)
                else:
                    # Don't retry on other client errors (e.g., 400, 401, 404)
                    error_msg = f"HTTP {e.response.status_code}: {e.response.text}"
                    self.logger.error("API request failed without retry", error=error_msg, endpoint=endpoint)
                    raise KalshiAPIError(error_msg)
            except Exception as e:
                last_exception = e
                self.logger.warning(f"Request failed with general exception. Retrying...", error=str(e), endpoint=endpoint)
                sleep_time = self.backoff_factor * (2 ** attempt)
                await asyncio.sleep(sleep_time)
        
        raise KalshiAPIError(f"API request failed after {self.max_retries} retries: {last_exception}")
    
    async def get_balance(self) -> Dict[str, Any]:
        """Get account balance."""
        return await self._make_authenticated_request("GET", "/trade-api/v2/portfolio/balance")
    
    async def get_positions(self, ticker: Optional[str] = None) -> Dict[str, Any]:
        """Get portfolio positions."""
        params = {}
        if ticker:
            params["ticker"] = ticker
        return await self._make_authenticated_request("GET", "/trade-api/v2/portfolio/positions", params=params)
    
    async def get_fills(
        self,
        ticker: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get order fills, optionally continuing from a pagination cursor.

        `limit` caps at 1000 server-side, and the endpoint defaults to 100 rows, so
        a caller that needs the full history has to follow `cursor` or it silently
        reads a truncated account.
        """
        params: Dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        return await self._make_authenticated_request("GET", "/trade-api/v2/portfolio/fills", params=params)
    
    async def get_settlements(
        self,
        ticker: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get settlement records for markets that have resolved.

        This is the only endpoint that reports a completed round trip in full:
        `/portfolio/positions` describes what is still held, so a market that has
        settled and rolled off contributes nothing there, while `/portfolio/settlements`
        reports its cost basis, revenue and fees. Account-level realized P&L cannot
        be derived from positions alone.
        """
        params: Dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        return await self._make_authenticated_request(
            "GET", "/trade-api/v2/portfolio/settlements", params=params
        )

    async def get_orders(self, ticker: Optional[str] = None, status: Optional[str] = None) -> Dict[str, Any]:
        """Get orders."""
        params = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        return await self._make_authenticated_request("GET", "/trade-api/v2/portfolio/orders", params=params)
    
    async def get_markets(
        self,
        limit: int = 100,
        cursor: Optional[str] = None,
        event_ticker: Optional[str] = None,
        series_ticker: Optional[str] = None,
        status: Optional[str] = None,
        tickers: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Get markets data.
        
        Args:
            limit: Maximum number of markets to return
            cursor: Pagination cursor
            event_ticker: Filter by event ticker
            series_ticker: Filter by series ticker
            status: Filter by market status
            tickers: List of specific tickers to fetch
        
        Returns:
            Markets data
        """
        params = {"limit": limit}
        
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if tickers:
            params["tickers"] = ",".join(tickers)
        
        return await self._make_authenticated_request(
            "GET", "/trade-api/v2/markets", params=params, require_auth=True
        )
    
    async def get_market(self, ticker: str) -> Dict[str, Any]:
        """Get specific market data."""
        return await self._make_authenticated_request(
            "GET", f"/trade-api/v2/markets/{ticker}", require_auth=False
        )
    
    async def get_orderbook(self, ticker: str, depth: int = 100) -> Dict[str, Any]:
        """
        Get market orderbook.
        
        Args:
            ticker: Market ticker
            depth: Orderbook depth
        
        Returns:
            Orderbook data
        """
        params = {"depth": depth}
        return await self._make_authenticated_request(
            "GET", f"/trade-api/v2/markets/{ticker}/orderbook", params=params, require_auth=False
        )
    
    async def get_market_history(
        self,
        ticker: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        limit: int = 100
    ) -> Dict[str, Any]:
        """
        Get market price history.
        
        Args:
            ticker: Market ticker
            start_ts: Start timestamp
            end_ts: End timestamp
            limit: Number of records to return
        
        Returns:
            Price history data
        """
        params = {"limit": limit}
        if start_ts:
            params["start_ts"] = start_ts
        if end_ts:
            params["end_ts"] = end_ts
        
        return await self._make_authenticated_request(
            "GET", f"/trade-api/v2/markets/{ticker}/history", params=params, require_auth=False
        )
    
    async def place_order(
        self,
        ticker: str,
        client_order_id: str,
        side: str,
        action: str,
        count: int,
        type_: str = "market",
        yes_price: Optional[int] = None,
        no_price: Optional[int] = None,
        expiration_ts: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Place a trading order.
        
        Args:
            ticker: Market ticker
            client_order_id: Unique client order ID
            side: "yes" or "no"
            action: "buy" or "sell"
            count: Number of contracts
            type_: Order type ("market" or "limit")
            yes_price: Yes price in cents (for limit orders)
            no_price: No price in cents (for limit orders)
            expiration_ts: Order expiration timestamp
        
        Returns:
            Order response
        """
        # Kalshi's legacy write endpoint is gone: POST /portfolio/orders
        # answers HTTP 410 deprecated_v1_order_endpoint on every order. This
        # legacy-shaped call is therefore translated into the V2 request and
        # the answer translated back, so every caller - entries, maker waits,
        # sells, the reaper - reaches the exchange without touching its own
        # code.
        leg_price = yes_price if yes_price is not None else no_price
        if leg_price is None:
            raise ValueError("a V2 order needs an explicit price")
        leg_price_dollars = int(leg_price) / 100.0
        # (action, side) -> the direction the order is actually long:
        # buy yes and sell no are long yes; buy no and sell yes are long no.
        outcome_side = (
            "yes" if (action == "buy") == (str(side).lower() == "yes") else "no"
        )
        # A resting limit wants good_till_canceled, with post_only so it can
        # never cross silently; a market-style order wants an immediate fill.
        resting = str(type_).lower() == "limit"
        response = await self.place_order_v2(
            ticker=ticker,
            client_order_id=client_order_id,
            outcome_side=outcome_side,
            price_dollars=leg_price_dollars,
            count=int(count),
            post_only=resting,
            time_in_force="good_till_canceled" if resting else "fill_or_kill",
        )
        filled = _as_float_safe(response.get("fill_count"))
        return {
            "order": {
                "order_id": response.get("order_id", ""),
                "client_order_id": response.get("client_order_id", client_order_id),
                "ticker": ticker,
                "side": str(side).lower(),
                "action": action,
                "count": count,
                "type": type_,
                "status": "filled" if filled > 0 else "resting",
                "yes_price": yes_price,
                "no_price": no_price,
                "create_time": response.get("ts_ms"),
            },
            "fill_count": filled,
            "remaining_count": _as_float_safe(response.get("remaining_count")),
            "average_fill_price": _as_float_safe(response.get("average_fill_price")),
            "average_fee_paid": _as_float_safe(response.get("average_fee_paid")),
        }
    
    # ------------------------------------------------------------------
    # V2 order endpoints. Writes moved to /portfolio/events/orders; the
    # legacy write path is gone (HTTP 410). Reads (/portfolio/orders/*) are
    # unchanged.
    #
    # V2 has no (action, side) pair: one book side and one YES-leg price.
    #   buy  yes -> bid @ p      sell yes -> ask @ p
    #   sell no  -> bid @ (1-p)  buy  no  -> ask @ (1-p)
    # (Kalshi "Order direction": bid == long yes, ask == long no, and a no-leg
    # price p is the yes-leg price 1-p.)
    # ------------------------------------------------------------------
    async def place_order_v2(
        self,
        *,
        ticker: str,
        client_order_id: str,
        outcome_side: str,
        price_dollars: float,
        count: int,
        post_only: bool = False,
        time_in_force: str = "fill_or_kill",
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """Place one order on the V2 event-order endpoint.

        `price_dollars` is the price of `outcome_side` in dollars (a no at
        30c is passed as 0.30 and converted internally to the 0.70 yes-leg
        price). `post_only` rejects the order if it would cross, which is
        what makes an entry a maker order instead of a hidden taker.
        """
        outcome = str(outcome_side or "yes").strip().lower()
        if outcome not in ("yes", "no"):
            raise ValueError(f"outcome_side must be yes or no, got {outcome_side!r}")
        price = float(price_dollars)
        if outcome == "no":
            price = 1.0 - price
        payload = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": "bid" if outcome == "yes" else "ask",
            "count": f"{int(count)}.00",
            "price": f"{price:.4f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": bool(post_only),
        }
        if reduce_only:
            payload["reduce_only"] = True
        return await self._make_authenticated_request(
            "POST", "/trade-api/v2/portfolio/events/orders", json_data=payload
        )

    async def cancel_order(
        self, order_id: str, market_ticker: Optional[str] = None
    ) -> Dict[str, Any]:
        """Cancel a resting order.

        V2 lives at /portfolio/events/orders/{id} and auto-routes only when
        `market_ticker` is supplied - an order_id alone cannot identify the
        exchange shard. Without a ticker the legacy path is used, which is
        what every pre-V2 caller did.
        """
        if market_ticker:
            return await self._make_authenticated_request(
                "DELETE",
                f"/trade-api/v2/portfolio/events/orders/{order_id}",
                params={"market_ticker": market_ticker},
            )
        return await self._make_authenticated_request(
            "DELETE", f"/trade-api/v2/portfolio/orders/{order_id}"
        )
    
    async def get_trades(
        self,
        ticker: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get trade history.
        
        Args:
            ticker: Filter by ticker
            limit: Maximum number of trades to return
            cursor: Pagination cursor
        
        Returns:
            Trades data
        """
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        
        return await self._make_authenticated_request(
            "GET", "/trade-api/v2/portfolio/trades", params=params
        )
    
    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()
        self.logger.info("Kalshi client closed")
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close() 