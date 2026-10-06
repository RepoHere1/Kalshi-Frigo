"""
Rolling 7-day confidence tracking and win rate analytics.

Tracks per-market-type win rates over rolling 7-day windows.
Only allows trading when win_rate > 58% (above random + 1% fee buffer).
"""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from src.utils.logging_setup import get_trading_logger

logger = get_trading_logger(__name__)


def analyze_trades(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze a list of trade records for profitability metrics.
    
    Args:
        trades: List of trade dicts with pnl, market_id, strategy, entry_price, etc.
    
    Returns:
        Dictionary with analysis results including win rate, best/worst trades, etc.
    """
    from datetime import datetime
    
    if not trades:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "best": 0.0,
            "worst": 0.0,
            "best_market": "",
            "worst_market": "",
            "by_strategy": {},
            "by_price_band": [],
            "hold_seconds_avg_win": 0.0,
            "hold_seconds_avg_loss": 0.0,
            "recommendations": ["Insufficient data for analysis"],
        }
    
    wins = 0
    losses = 0
    total_pnl = 0.0
    best_pnl = float('-inf')
    worst_pnl = float('inf')
    best_market = ""
    worst_market = ""
    by_strategy: Dict[str, Dict[str, Any]] = {}
    price_bands: Dict[str, Dict[str, Any]] = {}
    hold_times_win = []
    hold_times_loss = []
    
    for trade in trades:
        pnl = float(trade.get("pnl", 0.0))
        market_id = trade.get("market_id", "UNKNOWN")
        strategy = trade.get("strategy", "unknown")
        entry_price = float(trade.get("entry_price", 0.5))
        side = trade.get("side", "YES")
        
        # Track wins/losses
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        
        total_pnl += pnl
        
        # Track best/worst
        if pnl > best_pnl:
            best_pnl = pnl
            best_market = market_id
        if pnl < worst_pnl:
            worst_pnl = pnl
            worst_market = market_id
        
        # Track by strategy
        if strategy not in by_strategy:
            by_strategy[strategy] = {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "win_rate": 0.0,
            }
        
        by_strategy[strategy]["trades"] += 1
        by_strategy[strategy]["total_pnl"] += pnl
        if pnl > 0:
            by_strategy[strategy]["wins"] += 1
        else:
            by_strategy[strategy]["losses"] += 1
        
        # Determine price band
        if entry_price < 0.10:
            band = "under $0.10"
        elif entry_price >= 0.90:
            band = "$0.90 and up"
        elif entry_price < 0.25:
            band = "$0.10-$0.25"
        elif entry_price < 0.50:
            band = "$0.25-$0.50"
        elif entry_price < 0.75:
            band = "$0.50-$0.75"
        else:
            band = "$0.75-$0.90"
        
        if band not in price_bands:
            price_bands[band] = {
                "band": band,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "win_rate": 0.0,
            }
        
        price_bands[band]["trades"] += 1
        price_bands[band]["total_pnl"] += pnl
        if pnl > 0:
            price_bands[band]["wins"] += 1
        else:
            price_bands[band]["losses"] += 1
        
        # Track hold times
        entry_ts_str = trade.get("entry_timestamp", "")
        exit_ts_str = trade.get("exit_timestamp", "")
        if entry_ts_str and exit_ts_str:
            try:
                entry_ts = datetime.fromisoformat(entry_ts_str)
                exit_ts = datetime.fromisoformat(exit_ts_str)
                hold_seconds = (exit_ts - entry_ts).total_seconds()
                if pnl > 0:
                    hold_times_win.append(hold_seconds)
                else:
                    hold_times_loss.append(hold_seconds)
            except (ValueError, TypeError):
                pass
    
    # Calculate win rate
    total_trades = len(trades)
    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
    
    # Calculate win rates by strategy
    for strategy in by_strategy:
        s = by_strategy[strategy]
        s["win_rate"] = (s["wins"] / s["trades"] * 100.0) if s["trades"] > 0 else 0.0
    
    # Calculate win rates by price band
    for band in price_bands:
        p = price_bands[band]
        p["win_rate"] = (p["wins"] / p["trades"] * 100.0) if p["trades"] > 0 else 0.0
    
    # Sort strategies by PnL
    sorted_strategies = sorted(
        by_strategy.items(), key=lambda x: x[1]["total_pnl"], reverse=True
    )
    by_strategy = dict(sorted_strategies)
    
    # Calculate average hold times
    hold_avg_win = sum(hold_times_win) / len(hold_times_win) if hold_times_win else 0.0
    hold_avg_loss = sum(hold_times_loss) / len(hold_times_loss) if hold_times_loss else 0.0
    
    # Generate recommendations
    recommendations = []
    
    # Check if worst strategy is losing
    by_strategy_list = list(by_strategy.items())
    if by_strategy_list:
        worst_strategy_name, worst_strategy_data = by_strategy_list[-1]
        if worst_strategy_data["total_pnl"] < -50:
            recommendations.append(f"Strategy '{worst_strategy_name}' is losing money - consider disabling")
    
    if win_rate > 60:
        recommendations.append("Strong performance: consider increasing position size")
    elif win_rate < 40:
        recommendations.append("Poor win rate: review strategy logic and market selection")
    
    if not recommendations:
        recommendations.append("Performance within acceptable range")
    
    return {
        "trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "best": best_pnl if best_pnl != float('-inf') else 0.0,
        "worst": worst_pnl if worst_pnl != float('inf') else 0.0,
        "best_market": best_market,
        "worst_market": worst_market,
        "by_strategy": by_strategy,
        "by_price_band": list(price_bands.values()),
        "hold_seconds_avg_win": hold_avg_win,
        "hold_seconds_avg_loss": hold_avg_loss,
        "recommendations": recommendations,
    }





class RollingConfidenceTracker:
    """Tracks 7-day rolling win rates per market type."""

    def __init__(self, db_path: str = "trading_system.db"):
        self.db_path = db_path
        self._cache: Dict[str, Tuple[float, float]] = {}  # market_type -> (win_rate, last_update)
        self._cache_ttl = 300  # Cache for 5 minutes

    async def initialize_schema(self) -> None:
        """Create rolling_confidence table if it doesn't exist."""
        sql_statements = [
            """
            CREATE TABLE IF NOT EXISTS rolling_confidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_type TEXT NOT NULL,
                window_start REAL NOT NULL,
                window_end REAL NOT NULL,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                win_rate REAL DEFAULT 0.0,
                created_at REAL DEFAULT CURRENT_TIMESTAMP,
                updated_at REAL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_rolling_confidence_market_type 
                ON rolling_confidence(market_type, window_end DESC)
            """,
        ]
        async with aiosqlite.connect(self.db_path) as conn:
            for statement in sql_statements:
                if statement.strip():
                    await conn.execute(statement)
            await conn.commit()

    async def record_trade_result(
        self,
        market_type: str,
        is_winning: bool,
        trade_id: Optional[str] = None,
    ) -> None:
        """Record a trade result for win rate calculation."""
        now = time.time()
        window_end = now
        window_start = now - (7 * 24 * 3600)  # 7 days ago

        sql = """
        INSERT INTO rolling_confidence 
        (market_type, window_start, window_end, total_trades, winning_trades, win_rate, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        
        winning_trades = 1 if is_winning else 0
        total_trades = 1
        win_rate = float(winning_trades) / float(total_trades)

        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                sql,
                (
                    market_type,
                    window_start,
                    window_end,
                    total_trades,
                    winning_trades,
                    win_rate,
                    now,
                    now,
                ),
            )
            await conn.commit()

        # Invalidate cache
        if market_type in self._cache:
            del self._cache[market_type]

    async def get_rolling_win_rate(self, market_type: str) -> float:
        """Get 7-day rolling win rate for a market type.

        Returns:
            Win rate (0.0-1.0), or 0.5 if insufficient data.
        """
        # Check cache first
        if market_type in self._cache:
            cached_wr, cached_time = self._cache[market_type]
            if time.time() - cached_time < self._cache_ttl:
                return cached_wr

        now = time.time()
        window_start = now - (7 * 24 * 3600)  # 7 days ago

        sql = """
        SELECT 
            SUM(total_trades) as total,
            SUM(winning_trades) as wins
        FROM rolling_confidence
        WHERE market_type = ? AND window_end >= ?
        """

        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.execute(sql, (market_type, window_start))
                row = await cursor.fetchone()
                
            if row and row[0] is not None and row[0] > 0:
                total = int(row[0])
                wins = int(row[1]) if row[1] is not None else 0
                win_rate = float(wins) / float(total)
            else:
                # Insufficient data: default to neutral (no trading)
                win_rate = 0.5
        except Exception as exc:
            logger.error(f"Failed to fetch rolling win rate for {market_type}: {exc}")
            win_rate = 0.5

        # Cache the result
        self._cache[market_type] = (win_rate, now)
        return win_rate

    async def can_trade_market_type(self, market_type: str, threshold: float = 0.58) -> bool:
        """Check if market type meets minimum win rate threshold.

        Args:
            market_type: The market type to check (e.g., 'sports', 'economics', 'politics')
            threshold: Minimum win rate (default 58% = above random + 1% fee buffer)

        Returns:
            True if win_rate >= threshold, False otherwise.
        """
        win_rate = await self.get_rolling_win_rate(market_type)
        return win_rate >= threshold

    async def reset_market_type(self, market_type: str) -> None:
        """Reset confidence tracking for a market type (e.g., on new market list)."""
        sql = "DELETE FROM rolling_confidence WHERE market_type = ?"
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(sql, (market_type,))
            await conn.commit()
        
        if market_type in self._cache:
            del self._cache[market_type]
        
        logger.info(f"Reset rolling confidence for {market_type}")

    async def get_market_type_stats(self, market_type: str) -> Dict:
        """Get detailed stats for a market type."""
        now = time.time()
        window_start = now - (7 * 24 * 3600)

        sql = """
        SELECT 
            SUM(total_trades) as total,
            SUM(winning_trades) as wins,
            MIN(window_start) as earliest,
            MAX(window_end) as latest,
            COUNT(*) as record_count
        FROM rolling_confidence
        WHERE market_type = ? AND window_end >= ?
        """

        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.execute(sql, (market_type, window_start))
                row = await cursor.fetchone()

            if row and row[0] is not None:
                total = int(row[0])
                wins = int(row[1]) if row[1] is not None else 0
                win_rate = float(wins) / float(total) if total > 0 else 0.0
                return {
                    "market_type": market_type,
                    "total_trades": total,
                    "winning_trades": wins,
                    "win_rate": win_rate,
                    "record_count": row[4],
                    "window_days": 7,
                }
            else:
                return {
                    "market_type": market_type,
                    "total_trades": 0,
                    "winning_trades": 0,
                    "win_rate": 0.0,
                    "record_count": 0,
                    "window_days": 7,
                }
        except Exception as exc:
            logger.error(f"Failed to fetch stats for {market_type}: {exc}")
            return {
                "market_type": market_type,
                "error": str(exc),
            }
