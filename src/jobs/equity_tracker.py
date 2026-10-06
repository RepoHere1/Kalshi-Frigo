"""
DRY/LIVE equity tracking with weekly comparison reporting.

Maintains separate cumulative PnL for DRY and LIVE trading.
Weekly: computes ratio LIVE_PnL / DRY_PnL.
If LIVE < 0.95 * DRY for 2 weeks: posts alert (model divergence likely).
"""

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiosqlite

from src.utils.logging_setup import get_trading_logger

logger = get_trading_logger(__name__)


class EquityTracker:
    """Tracks and compares DRY vs LIVE equity curves."""

    def __init__(self, db_path: str = "trading_system.db"):
        self.db_path = db_path
        self._last_weekly_check = 0.0

    async def initialize_schema(self) -> None:
        """Create equity tracking tables."""
        sql_statements = [
            """
            CREATE TABLE IF NOT EXISTS equity_curve (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                timestamp REAL NOT NULL,
                cumulative_pnl REAL,
                daily_pnl REAL,
                balance REAL,
                created_at REAL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_equity_mode_timestamp
                ON equity_curve(mode, timestamp DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS equity_report (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date REAL NOT NULL,
                dry_cumulative_pnl REAL,
                live_cumulative_pnl REAL,
                ratio REAL,
                alert_triggered BOOLEAN DEFAULT 0,
                alert_reason TEXT,
                created_at REAL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_equity_report_date
                ON equity_report(report_date DESC)
            """,
        ]
        async with aiosqlite.connect(self.db_path) as conn:
            for statement in sql_statements:
                if statement.strip():
                    await conn.execute(statement)
            await conn.commit()

    async def record_equity(
        self,
        mode: str,
        cumulative_pnl: float,
        daily_pnl: float = 0.0,
        balance: float = 0.0,
    ) -> None:
        """Record an equity snapshot for DRY or LIVE."""
        now = time.time()
        sql = """
        INSERT INTO equity_curve
        (mode, timestamp, cumulative_pnl, daily_pnl, balance, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                sql,
                (mode, now, cumulative_pnl, daily_pnl, balance, now),
            )
            await conn.commit()

    async def get_latest_equity(self, mode: str) -> Optional[Dict[str, Any]]:
        """Get most recent equity snapshot for a mode."""
        sql = """
        SELECT timestamp, cumulative_pnl, daily_pnl, balance, created_at
        FROM equity_curve
        WHERE mode = ?
        ORDER BY timestamp DESC LIMIT 1
        """
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.execute(sql, (mode,))
                row = await cursor.fetchone()
            if row:
                return {
                    "timestamp": row[0],
                    "cumulative_pnl": row[1],
                    "daily_pnl": row[2],
                    "balance": row[3],
                    "created_at": row[4],
                }
        except Exception as exc:
            logger.error(f"Failed to fetch latest equity for {mode}: {exc}")
        return None

    async def run_weekly_report(self) -> Optional[Dict[str, Any]]:
        """Generate weekly DRY/LIVE comparison report.

        Returns:
            Report dict or None if report recently generated.
        """
        now = time.time()

        # Throttle to once per week
        if now - self._last_weekly_check < (7 * 24 * 3600):
            return None

        self._last_weekly_check = now

        # Get cumulative PnL for both modes over past 7 days
        seven_days_ago = now - (7 * 24 * 3600)

        sql_dry = """
        SELECT COALESCE(SUM(daily_pnl), 0.0) FROM equity_curve
        WHERE mode = 'dry' AND timestamp >= ?
        """
        sql_live = """
        SELECT COALESCE(SUM(daily_pnl), 0.0) FROM equity_curve
        WHERE mode = 'live' AND timestamp >= ?
        """

        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cursor_dry = await conn.execute(sql_dry, (seven_days_ago,))
                row_dry = await cursor_dry.fetchone()
                dry_pnl = row_dry[0] if row_dry else 0.0

                cursor_live = await conn.execute(sql_live, (seven_days_ago,))
                row_live = await cursor_live.fetchone()
                live_pnl = row_live[0] if row_live else 0.0

            # Calculate ratio
            ratio = live_pnl / dry_pnl if dry_pnl != 0.0 else 1.0

            # Check for model divergence (LIVE << DRY)
            alert_triggered = ratio < 0.95
            alert_reason = None
            if alert_triggered:
                alert_reason = f"LIVE underperforming DRY: {ratio:.2%} (threshold: 95%)"
                logger.warning(alert_reason)

            # Persist report
            report_sql = """
            INSERT INTO equity_report
            (report_date, dry_cumulative_pnl, live_cumulative_pnl, ratio, alert_triggered, alert_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute(
                    report_sql,
                    (
                        now,
                        dry_pnl,
                        live_pnl,
                        ratio,
                        1 if alert_triggered else 0,
                        alert_reason,
                        now,
                    ),
                )
                await conn.commit()

            report = {
                "report_date": now,
                "dry_pnl_7day": dry_pnl,
                "live_pnl_7day": live_pnl,
                "ratio": ratio,
                "alert_triggered": alert_triggered,
                "alert_reason": alert_reason,
            }

            logger.info(f"Weekly equity report: DRY=${dry_pnl:.2f} LIVE=${live_pnl:.2f} Ratio={ratio:.2%}")
            return report

        except Exception as exc:
            logger.error(f"Failed to generate equity report: {exc}")
            return None

    async def get_recent_reports(self, count: int = 4) -> List[Dict[str, Any]]:
        """Get recent weekly reports (default last 4 weeks)."""
        sql = """
        SELECT report_date, dry_cumulative_pnl, live_cumulative_pnl, ratio, alert_triggered, alert_reason
        FROM equity_report
        ORDER BY report_date DESC
        LIMIT ?
        """
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.execute(sql, (count,))
                rows = await cursor.fetchall()
            
            reports = []
            for row in rows:
                reports.append({
                    "report_date": row[0],
                    "dry_cumulative_pnl": row[1],
                    "live_cumulative_pnl": row[2],
                    "ratio": row[3],
                    "alert_triggered": row[4],
                    "alert_reason": row[5],
                })
            return reports
        except Exception as exc:
            logger.error(f"Failed to fetch equity reports: {exc}")
            return []

    async def get_equity_curves(
        self, days: int = 7
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Get equity curves for both modes over N days."""
        cutoff = time.time() - (days * 24 * 3600)

        sql = """
        SELECT mode, timestamp, cumulative_pnl, daily_pnl, balance
        FROM equity_curve
        WHERE timestamp >= ?
        ORDER BY mode, timestamp
        """

        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.execute(sql, (cutoff,))
                rows = await cursor.fetchall()

            curves = {"dry": [], "live": []}
            for row in rows:
                mode, ts, cum_pnl, daily_pnl, balance = row
                if mode in curves:
                    curves[mode].append({
                        "timestamp": ts,
                        "cumulative_pnl": cum_pnl,
                        "daily_pnl": daily_pnl,
                        "balance": balance,
                    })
            return curves
        except Exception as exc:
            logger.error(f"Failed to fetch equity curves: {exc}")
            return {"dry": [], "live": []}
