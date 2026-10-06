"""
Real-time trade audit loop - records entry reasons and computes weekly performance.

After every fill: logs entry reason, market context, fill price.
Weekly: computes win rate per (market_type, entry_reason) pair.
Drops entry patterns with 3+ losses >= $4.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiosqlite

from src.utils.logging_setup import get_trading_logger

logger = get_trading_logger(__name__)


class AuditLoop:
    """Real-time audit of trades and weekly pattern analysis."""

    def __init__(self, db_path: str = "trading_system.db"):
        self.db_path = db_path
        self._last_weekly_run = 0.0

    async def initialize_schema(self) -> None:
        """Create audit and pattern tables."""
        sql_statements = [
            """
            CREATE TABLE IF NOT EXISTS trade_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT UNIQUE,
                market_type TEXT,
                market_id TEXT,
                entry_reason TEXT,
                entry_price REAL,
                fill_price REAL,
                position_size REAL,
                pnl REAL DEFAULT 0.0,
                is_win BOOLEAN DEFAULT 0,
                created_at REAL,
                filled_at REAL,
                closed_at REAL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_audit_market_type_reason
                ON trade_audit(market_type, entry_reason, created_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS audit_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_type TEXT,
                entry_reason TEXT,
                window_start REAL,
                window_end REAL,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                losing_trades INTEGER DEFAULT 0,
                win_rate REAL DEFAULT 0.0,
                avg_pnl REAL DEFAULT 0.0,
                large_losses_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_at REAL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_patterns_market_reason_window
                ON audit_patterns(market_type, entry_reason, window_end DESC)
            """,
        ]
        async with aiosqlite.connect(self.db_path) as conn:
            for statement in sql_statements:
                if statement.strip():
                    await conn.execute(statement)
            await conn.commit()

    async def log_fill(
        self,
        trade_id: str,
        market_type: str,
        market_id: str,
        entry_reason: str,
        entry_price: float,
        fill_price: float,
        position_size: float,
    ) -> None:
        """Log a fill event."""
        now = time.time()
        sql = """
        INSERT OR REPLACE INTO trade_audit
        (trade_id, market_type, market_id, entry_reason, entry_price, fill_price,
         position_size, created_at, filled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                sql,
                (
                    trade_id,
                    market_type,
                    market_id,
                    entry_reason,
                    entry_price,
                    fill_price,
                    position_size,
                    now,
                    now,
                ),
            )
            await conn.commit()
        logger.info(
            f"Audit fill: {trade_id} {market_type} {entry_reason} @ ${fill_price:.4f}"
        )

    async def log_close(
        self,
        trade_id: str,
        pnl: float,
        is_win: bool,
    ) -> None:
        """Log a position close event."""
        now = time.time()
        sql = """
        UPDATE trade_audit
        SET pnl = ?, is_win = ?, closed_at = ?
        WHERE trade_id = ?
        """
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(sql, (pnl, 1 if is_win else 0, now, trade_id))
            await conn.commit()
        logger.info(f"Audit close: {trade_id} PnL=${pnl:.2f} Win={is_win}")

    async def run_weekly_analysis(self) -> Dict[str, Any]:
        """Run weekly pattern analysis and drop bad patterns."""
        now = time.time()

        # Throttle to once per week
        if now - self._last_weekly_run < (7 * 24 * 3600):
            logger.debug("Weekly audit already ran recently, skipping")
            return {}

        self._last_weekly_run = now
        logger.info("Running weekly audit pattern analysis...")

        # Get all trade patterns from the last 7 days
        window_start = now - (7 * 24 * 3600)

        sql = """
        SELECT
            market_type,
            entry_reason,
            COUNT(*) as total,
            SUM(CASE WHEN is_win = 1 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN is_win = 0 AND pnl <= -4.0 THEN 1 ELSE 0 END) as large_losses,
            AVG(pnl) as avg_pnl
        FROM trade_audit
        WHERE closed_at >= ? AND closed_at IS NOT NULL
        GROUP BY market_type, entry_reason
        """

        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.execute(sql, (window_start,))
                rows = await cursor.fetchall()

            patterns_analyzed = 0
            patterns_dropped = 0
            report = {
                "timestamp": now,
                "patterns_analyzed": 0,
                "patterns_dropped": 0,
                "dropped_patterns": [],
            }

            for row in rows:
                market_type, entry_reason, total, wins, large_losses, avg_pnl = row
                if not total or total == 0:
                    continue

                patterns_analyzed += 1
                win_rate = float(wins) / float(total) if total > 0 else 0.0

                # Persist pattern data
                await self._insert_pattern(
                    market_type, entry_reason, window_start, now, total, wins, large_losses, avg_pnl
                )

                # Decision: drop pattern if 3+ losses >= $4
                if (large_losses or 0) >= 3:
                    patterns_dropped += 1
                    await self._mark_pattern_dropped(market_type, entry_reason)
                    report["dropped_patterns"].append(
                        {
                            "market_type": market_type,
                            "entry_reason": entry_reason,
                            "total_trades": total,
                            "large_losses": large_losses,
                            "avg_pnl": avg_pnl,
                        }
                    )
                    logger.warning(
                        f"DROPPED pattern: {market_type} {entry_reason} "
                        f"({total} trades, {large_losses} large losses)"
                    )

            report["patterns_analyzed"] = patterns_analyzed
            report["patterns_dropped"] = patterns_dropped

            logger.info(
                f"Weekly audit complete: {patterns_analyzed} patterns, "
                f"{patterns_dropped} dropped"
            )
            return report

        except Exception as exc:
            logger.error(f"Weekly audit failed: {exc}")
            return {"error": str(exc)}

    async def _insert_pattern(
        self,
        market_type: str,
        entry_reason: str,
        window_start: float,
        window_end: float,
        total: int,
        wins: int,
        large_losses: int,
        avg_pnl: float,
    ) -> None:
        """Insert/update pattern record."""
        win_rate = float(wins) / float(total) if total > 0 else 0.0
        losing_trades = total - wins

        sql = """
        INSERT OR REPLACE INTO audit_patterns
        (market_type, entry_reason, window_start, window_end, total_trades,
         winning_trades, losing_trades, win_rate, avg_pnl, large_losses_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                sql,
                (
                    market_type,
                    entry_reason,
                    window_start,
                    window_end,
                    total,
                    wins,
                    losing_trades,
                    win_rate,
                    avg_pnl,
                    large_losses,
                    time.time(),
                ),
            )
            await conn.commit()

    async def _mark_pattern_dropped(self, market_type: str, entry_reason: str) -> None:
        """Mark a pattern as dropped/invalid."""
        sql = """
        UPDATE audit_patterns
        SET status = 'dropped'
        WHERE market_type = ? AND entry_reason = ? AND status = 'active'
        """
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(sql, (market_type, entry_reason))
            await conn.commit()

    async def get_pattern_status(self, market_type: str, entry_reason: str) -> str:
        """Check if a pattern is active or dropped."""
        sql = """
        SELECT status FROM audit_patterns
        WHERE market_type = ? AND entry_reason = ?
        ORDER BY created_at DESC LIMIT 1
        """
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.execute(sql, (market_type, entry_reason))
                row = await cursor.fetchone()
            if row:
                return row[0]
        except Exception:
            pass
        return "active"  # Default to active if not found

    async def get_recent_pattern_stats(
        self, market_type: str, entry_reason: str, days: int = 7
    ) -> Optional[Dict[str, Any]]:
        """Get recent stats for a pattern."""
        window_start = time.time() - (days * 24 * 3600)

        sql = """
        SELECT
            total_trades,
            winning_trades,
            losing_trades,
            win_rate,
            avg_pnl,
            large_losses_count,
            status
        FROM audit_patterns
        WHERE market_type = ? AND entry_reason = ? AND window_end >= ?
        ORDER BY window_end DESC LIMIT 1
        """
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.execute(sql, (market_type, entry_reason, window_start))
                row = await cursor.fetchone()
            if row:
                return {
                    "total_trades": row[0],
                    "winning_trades": row[1],
                    "losing_trades": row[2],
                    "win_rate": row[3],
                    "avg_pnl": row[4],
                    "large_losses_count": row[5],
                    "status": row[6],
                }
        except Exception as exc:
            logger.error(f"Failed to fetch pattern stats: {exc}")
        return None
