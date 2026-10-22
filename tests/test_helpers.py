"""
Test helpers to reduce API calls and improve test efficiency.
"""

import asyncio
from typing import List, Optional, Dict, Any
from src.clients.kalshi_client import KalshiClient
from src.utils.database import Market
import pytest

# Cache for markets to avoid repeated API calls
_MARKETS_CACHE = None
_CACHE_LOCK = asyncio.Lock()


async def get_test_markets(limit: int = 10) -> List[Dict[str, Any]]:
    """
    Get a small set of markets for testing, with caching to reduce API calls.

    Args:
        limit: Maximum number of markets to fetch

    Returns:
        List of market data dictionaries
    """
    global _MARKETS_CACHE

    async with _CACHE_LOCK:
        if _MARKETS_CACHE is None:
            kalshi_client = KalshiClient()
            try:
                # Fetch only a small number of markets for testing
                markets_response = await kalshi_client.get_markets(limit=limit)
                _MARKETS_CACHE = markets_response.get("markets", [])[:limit]
                print(f"✅ Cached {len(_MARKETS_CACHE)} markets for testing")
            except Exception as e:
                print(f"❌ Failed to fetch test markets: {e}")
                _MARKETS_CACHE = []
            finally:
                await kalshi_client.close()

    return _MARKETS_CACHE


async def find_suitable_test_market() -> Optional[Market]:
    """
    Find one suitable market for testing without excessive API calls.

    Returns:
        Market object suitable for testing, or None if none found
    """
    markets_data = await get_test_markets(limit=5)  # Only fetch 5 markets

    for market_data in markets_data:
        if (
            market_data.get("status") == "active"
            and market_data.get("volume", 0) > 500  # Lower threshold for testing
            and market_data.get("yes_bid")
            and market_data.get("no_bid")
        ):

            # Create Market object from real data
            return Market(
                market_id=market_data["ticker"],
                title=market_data["title"],
                yes_price=(market_data.get("yes_bid", 0) + market_data.get("yes_ask", 0)) / 200,
                no_price=(market_data.get("no_bid", 0) + market_data.get("no_ask", 0)) / 200,
                volume=market_data.get("volume", 0),
                expiration_ts=market_data.get("close_ts", 0),
                category=market_data.get("category", "test"),
                status=market_data.get("status", "active"),
                last_updated=None,
                has_position=False,
            )

    return None


async def get_test_market_data(market_id: str) -> Optional[Dict[str, Any]]:
    """
    Get specific market data for a known market ID without fetching all markets.

    Args:
        market_id: The specific market ticker to fetch

    Returns:
        Market data dictionary or None if not found
    """
    kalshi_client = KalshiClient()
    try:
        market_response = await kalshi_client.get_market(market_id)
        return market_response.get("market") if market_response else None
    except Exception as e:
        print(f"❌ Failed to fetch market {market_id}: {e}")
        return None
    finally:
        await kalshi_client.close()


def clear_markets_cache():
    """Clear the markets cache for fresh data."""
    global _MARKETS_CACHE
    _MARKETS_CACHE = None
