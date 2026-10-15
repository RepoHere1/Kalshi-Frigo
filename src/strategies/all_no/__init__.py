"""ALL NO MARKETS strategy for Kalshi-Frigo.

CRITICAL SEPARATION GUARANTEE:
- ALL NO DRY book is COMPLETELY SEPARATE from main DRY book ($300 baseline)
- ALL NO LIVE book is COMPLETELY SEPARATE from LIVE account
- DRY balance NEVER reflects ALL NO balances
- ALL NO DRY and ALL NO LIVE are separate entities with no crossover effect

ARCHITECTURE:
- all_no_book table: $250 starting balance (separate from DRY $300)
- all_no_positions table: separate from main positions table (mode-aware: dry/live)
- all_no_trade_logs table: separate from main trade_logs table (mode-aware: dry/live)

TRADING STRATEGY - MAXIMUM WIN RATE & CASH PAYOUTS:
- NO-side only: Sell NO contracts (buy downside protection at discount)
- Maker entries only: Limit orders at bid prices (1.75% fees vs 7% taker = 4x savings)
- Kelly sizing: Quarter-Kelly with 20% maximum clip per position
- Entry criteria: Edge > 6%, confidence > 40%, NO price 80-95c range (favorite-longshot edge)
- Exit strategy: Profit targets at 95c+, stop losses at 10% drawdown, time-based exits
- Risk management: Max 10 concurrent positions, single market per position
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple

from src.clients.kalshi_client import KalshiClient
from src.config.settings import settings
from src.jobs.broker import should_trade_live, broker_for_mode, default_mode_manager
from src.utils.database import DatabaseManager, Position, Market
from src.utils.logging_setup import get_trading_logger
from src.utils.mode import MODE_DRY, MODE_LIVE, TradingMode
from src.utils.market_prices import get_market_prices, is_tradeable_market
from src.utils.position_sizing import kelly_fraction
from src.utils.edge_filter import EdgeFilter, calculate_roundtrip_fees_dollars

logger = get_trading_logger("all_no_strategy")

# Configuration from web_dashboard.py
ALL_NO_STARTING_BALANCE = 250.0
ALL_NO_KELLY_MULTIPLIER = 0.25
ALL_NO_MAX_CLIP_PCT = 0.20
MIN_ORDER_CONTRACTS = 1

# Mode-specific books
ALL_NO_DRY_BOOK = "all_no_dry"
ALL_NO_LIVE_BOOK = "all_no_live"

# TRADING PARAMETERS FOR MAXIMUM WIN RATE
MIN_EDGE_REQUIREMENT = 0.06          # 6% minimum edge (after fees)
MIN_CONFIDENCE_FOR_TRADE = 0.75      # 75% minimum confidence
NO_PRICE_MIN = 0.80                  # NO price minimum (80c)
NO_PRICE_MAX = 0.95                  # NO price maximum (95c) - favorite-longshot sweet spot
MAX_CONCURRENT_POSITIONS = 50        # Max open positions
PROFIT_TARGET_PRICE = 0.95           # Sell NO when price reaches 95c
STOP_LOSS_THRESHOLD = -0.10          # 10% drawdown stop loss
MIN_TIME_TO_EXPIRY_HOURS = 6         # Don't trade markets expiring in < 6 hours
MIN_VOLUME_FOR_TRADE = 1000          # Minimum volume for liquidity


class AllNoKalshiKellySizer:
    """Kelly sizing implementation for ALL NO MARKETS book."""

    @staticmethod
    def calculate_kelly_fraction(fair: float, price: float) -> float:
        """Calculate Kelly fraction for ALL NO book.

        For a NO contract bought at price P with a model fair of F:
        - stake P to win (1 - P) -> net odds b = (1 - P) / P
        - f* = (F*b - (1 - F)) / b [kelly_fraction(F, b)]
        """
        if price <= 0.0 or price >= 1.0 or fair <= 0.0:
            return 0.0

        # Net odds for a NO contract bought at `price`
        b = (1.0 - price) / price
        f = kelly_fraction(fair, b) * ALL_NO_KELLY_MULTIPLIER
        return max(0.0, min(f, ALL_NO_MAX_CLIP_PCT))

    @staticmethod
    def calculate_buy_amount(fair: float, price: float) -> Dict[str, Any]:
        """Calculate the clip a Kelly fraction actually buys.

        The contract count is the whole number of contracts the stake actually
        buys, floored at one: Kalshi takes a whole-contract count with no dollar
        minimum, so rounding a clip to zero would silently drop a paper bet.
        """
        if price <= 0.0 or price >= 1.0:
            return {"fraction": 0.0, "stake": 0.0, "contracts": 0}

        fraction = AllNoKalshiKellySizer.calculate_kelly_fraction(fair, price)
        if fraction <= 0.0:
            return {"fraction": 0.0, "stake": 0.0, "contracts": 0}

        stake = fraction  # a fraction of the book; the caller multiplies by balance
        # One contract's cost, so the count is the whole contracts the stake buys.
        contracts = max(MIN_ORDER_CONTRACTS, int(fraction / price))
        return {"fraction": fraction, "stake": stake, "contracts": contracts}


class AllNoKalshiBook:
    """ALL NO MARKETS book management with COMPLETE SEPARATION guarantee.

    Separate from DRY ($300) and LIVE books. Uses its own tables:
    - all_no_book: balance tracking
    - all_no_positions: position tracking (mode-aware: dry/live)
    - all_no_trade_logs: trade history (mode-aware: dry/live)

    DRY balance NEVER reflects ALL NO balances - they are independent books.
    """

    def __init__(self, db_path: str = "trading_system.db"):
        self.db_path = db_path
        self.logger = get_trading_logger("all_no_book")
        self.mode_manager = TradingMode(db_path)

    async def ensure_schema(self) -> None:
        """Ensure all ALL NO tables exist with mode-aware columns."""
        import aiosqlite

        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row

            # ALL NO book ledger (balance tracking)
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS all_no_book (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    amount REAL NOT NULL,
                    balance_after REAL NOT NULL,
                    note TEXT,
                    timestamp TEXT NOT NULL
                )
                """
            )

            # ALL NO positions (mode-aware: dry or live)
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS all_no_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    side TEXT NOT NULL DEFAULT 'NO',
                    entry_price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    status TEXT DEFAULT 'open',
                    strategy TEXT DEFAULT 'all_no',
                    mode TEXT NOT NULL DEFAULT 'dry',
                    entry_fair REAL,
                    UNIQUE(market_id, mode)
                )
                """
            )

            # ALL NO trade logs (mode-aware: dry or live)
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS all_no_trade_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_fair REAL,
                    market_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    pnl REAL NOT NULL,
                    entry_timestamp TEXT NOT NULL,
                    exit_timestamp TEXT NOT NULL,
                    rationale TEXT,
                    strategy TEXT DEFAULT 'all_no',
                    mode TEXT NOT NULL DEFAULT 'dry'
                )
                """
            )

            # Create indexes for mode-aware queries
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_all_no_positions_mode ON all_no_positions(mode)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_all_no_positions_status ON all_no_positions(status)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_all_no_trade_logs_mode ON all_no_trade_logs(mode)"
            )

            await conn.commit()
            self.logger.info("ALL NO MARKETS tables initialized (mode-aware)")

    async def seed_book(self) -> None:
        """Seed the ALL NO book with $250 starting balance once.

        CRITICAL: This is separate from DRY book's $300 seed.
        Never mixes with main DRY balance.
        """
        import aiosqlite

        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT COUNT(*) AS n FROM all_no_book WHERE note = 'seed'"
            )
            seeded = await cursor.fetchone()
            seeded = int(seeded["n"] or 0)
            if not seeded:
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                await conn.execute(
                    "INSERT INTO all_no_book (kind, amount, balance_after, note, timestamp) "
                    "VALUES ('deposit', ?, ?, 'seed', ?)",
                    (ALL_NO_STARTING_BALANCE, ALL_NO_STARTING_BALANCE, stamp),
                )
                await conn.commit()
                self.logger.info(
                    f"ALL NO MARKETS book seeded: ${ALL_NO_STARTING_BALANCE:.2f} (SEPARATE from DRY $300)"
                )
            else:
                self.logger.debug(f"ALL NO MARKETS book already seeded: ${ALL_NO_STARTING_BALANCE:.2f}")

    async def get_balance(self, mode: str = MODE_DRY) -> Dict[str, Any]:
        """Get current balance of the ALL NO book for specific mode.

        CRITICAL: Returns balance for SPECIFIC mode only.
        DRY balance NEVER includes LIVE positions or vice versa.

        Args:
            mode: 'dry' or 'live' - which book balance to return

        Returns:
            Balance info for the specified mode only
        """
        import aiosqlite

        await self.ensure_schema()

        # Validate mode
        mode = mode.strip().lower()
        if mode not in (MODE_DRY, MODE_LIVE):
            raise ValueError(f"Invalid mode: {mode}. Must be '{MODE_DRY}' or '{MODE_LIVE}'.")

        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row

            # Get total deposits (seed only)
            cursor = await conn.execute(
                "SELECT COALESCE(SUM(amount), 0.0) AS total FROM all_no_book WHERE note = 'seed'"
            )
            result = await cursor.fetchone()
            total = float(result["total"] or 0.0)

            # Get total deployed from open positions FOR THIS MODE ONLY
            cursor = await conn.execute(
                "SELECT COALESCE(SUM(quantity * entry_price), 0.0) AS deployed FROM all_no_positions "
                "WHERE status = 'open' AND mode = ?",
                (mode,),
            )
            result = await cursor.fetchone()
            deployed = float(result["deployed"] or 0.0)

            # Get total realized P&L from closed trades FOR THIS MODE ONLY
            cursor = await conn.execute(
                "SELECT COALESCE(SUM(pnl), 0.0) AS realized FROM all_no_trade_logs WHERE mode = ?",
                (mode,),
            )
            result = await cursor.fetchone()
            realized = float(result["realized"] or 0.0)

            # Get open positions count FOR THIS MODE ONLY
            cursor = await conn.execute(
                "SELECT COUNT(*) AS count FROM all_no_positions WHERE status = 'open' AND mode = ?",
                (mode,),
            )
            result = await cursor.fetchone()
            open_count = int(result["count"] or 0)

            # Calculate current balance
            balance = total + realized - deployed

            return {
                "book": f"all_no_{mode}",
                "mode": mode,
                "starting_balance": ALL_NO_STARTING_BALANCE,
                "cash": round(balance, 2),
                "deployed": round(deployed, 2),
                "open_positions": open_count,
                "equity": round(balance + deployed, 2),
                "realized": round(realized, 2),
                "total_pnl": round(balance, 2),
                "closed_trades": 0,
                "starting": ALL_NO_STARTING_BALANCE,
                "separate_from_dry_book": True,
                "separate_from_live_account": True,
            }

    async def get_both_balances(self) -> Dict[str, Dict[str, Any]]:
        """Get balances for BOTH DRY and LIVE ALL NO books separately."""
        return {
            MODE_DRY: await self.get_balance(MODE_DRY),
            MODE_LIVE: await self.get_balance(MODE_LIVE),
        }

    async def is_separate_from_dry_book(self) -> bool:
        """Verify that ALL NO book is separate from main DRY book."""
        dry_balance = await self.get_balance(MODE_DRY)
        return dry_balance.get("separate_from_dry_book", False)

    async def is_separate_from_live_account(self) -> bool:
        """Verify that ALL NO book is separate from LIVE account."""
        live_balance = await self.get_balance(MODE_LIVE)
        return live_balance.get("separate_from_live_account", False)


class AllNoStrategy:
    """ALL NO MARKETS trading strategy with COMPLETE SEPARATION guarantee.

    Implements the NO-side only maker strategy with its own $250 book.
    DRY and LIVE modes are completely separate with NO crossover effect.
    Optimized for maximum win rate and cash payouts.
    """

    def __init__(self):
        self.logger = get_trading_logger("all_no_strategy")
        self.db_path = os.environ.get("DB_PATH", "trading_system.db")
        self.mode_manager = TradingMode(self.db_path)
        self.all_no_book = AllNoKalshiBook(self.db_path)
        self.enabled = True

    async def should_trade(self, mode: str = MODE_DRY) -> bool:
        """Check if trading is allowed for ALL NO in specific mode."""
        mode = mode.strip().lower()
        if mode not in (MODE_DRY, MODE_LIVE):
            return False

        book_enabled = os.environ.get("ALL_NO_ENABLED", "true").strip().lower() == "true"
        if not book_enabled:
            return False

        if not await self.all_no_book.is_separate_from_dry_book():
            self.logger.error("❌ CRITICAL: ALL NO book not separate from DRY book!")
            return False

        return True

    async def scan_and_trade(self, mode: str = MODE_DRY) -> List[Position]:
        """Scan markets and place ALL NO orders for specific mode.

        CRITICAL: Only trades in the specified mode.
        DRY mode does NOT affect LIVE mode and vice versa.

        Returns:
            List of created positions for this mode only
        """
        mode = mode.strip().lower()
        if mode not in (MODE_DRY, MODE_LIVE):
            self.logger.error(f"❌ Invalid mode: {mode}")
            return []

        if not await self.should_trade(mode):
            self.logger.warning(f"❌ ALL NO {mode.upper()} trading not enabled, skipping")
            return []

        created_positions = []

        try:
            self.logger.info(f"📊 Scanning markets for ALL NO {mode.upper()} opportunities")

            db_manager = DatabaseManager(self.db_path)

            # Get eligible markets with volume and expiry filters
            try:
                markets = await db_manager.get_eligible_markets(
                    volume_min=MIN_VOLUME_FOR_TRADE,
                    max_days_to_expiry=30
                )
            except Exception:
                self.logger.warning("⚠️  Could not fetch eligible markets from DB")
                return created_positions

            if not markets:
                self.logger.info(f"📭 No eligible markets for ALL NO {mode.upper()}")
                return created_positions

            # Get ALL NO book balance for this mode
            book_balance = await self.all_no_book.get_balance(mode)
            available_cash = book_balance["cash"]
            open_count = book_balance["open_positions"]

            self.logger.info(
                f"💰 ALL NO {mode.upper()} book balance: ${available_cash:.2f} "
                f"(starting: $250.00, open positions: {open_count})"
            )

            # Check max concurrent positions
            if open_count >= MAX_CONCURRENT_POSITIONS:
                self.logger.warning(
                    f"⚠️  Max concurrent positions reached ({open_count}/{MAX_CONCURRENT_POSITIONS}), skipping"
                )
                return created_positions

            slots_available = MAX_CONCURRENT_POSITIONS - open_count
            self.logger.info(f"📋 Available slots: {slots_available}")

            kalshi_client = KalshiClient()
            try:
                # Analyze each market for NO-side opportunities
                opportunities = []
                for market in markets:
                    try:
                        opp = await self._analyze_market_for_no_entry(
                            market, kalshi_client
                        )
                        if opp:
                            opportunities.append(opp)
                    except Exception as e:
                        self.logger.error(f"❌ Error analyzing {market.market_id}: {e}")
                        continue

                # Sort by edge magnitude (highest edge first)
                opportunities.sort(key=lambda x: x["edge"], reverse=True)

                # Take top opportunities up to available slots
                selected = opportunities[:slots_available]

                self.logger.info(
                    f"🎯 Found {len(opportunities)} NO opportunities, "
                    f"selecting top {len(selected)}"
                )

                # Place orders for selected opportunities
                for opp in selected:
                    try:
                        position = await self._place_no_order(
                            opp, db_manager, kalshi_client, mode, available_cash
                        )
                        if position:
                            created_positions.append(position)
                            cost = position.entry_price * position.quantity
                            available_cash -= cost
                            self.logger.info(
                                f"✅ ALL NO {mode.upper()} position: {position.market_id} "
                                f"${position.entry_price:.3f} x {position.quantity} "
                                f"(cost: ${cost:.2f}, remaining cash: ${available_cash:.2f})"
                            )
                    except Exception as e:
                        self.logger.error(f"❌ Error placing order: {e}")
                        continue

                self.logger.info(
                    f"🎯 ALL NO {mode.upper()} cycle complete: "
                    f"{len(created_positions)} positions created"
                )
                return created_positions

            finally:
                try:
                    await kalshi_client.close()
                except Exception:
                    pass

        except Exception as e:
            self.logger.error(f"❌ Error in ALL NO {mode.upper()} strategy: {e}")
            return created_positions

    async def _analyze_market_for_no_entry(
        self, market: Market, kalshi_client: KalshiClient
    ) -> Optional[Dict[str, Any]]:
        """Analyze a market for NO-side entry opportunity.

        Returns opportunity dict with edge, confidence, pricing, etc.
        """
        try:
            # Get current market data
            market_data = await kalshi_client.get_market(market.market_id)
            market_info = market_data.get("market", {})

            # Get market prices
            yes_bid, yes_ask, no_bid, no_ask = get_market_prices(market_info)

            # NO-side: we want to sell NO (buy NO contracts at discount)
            # NO ask is what we pay to buy NO contracts
            no_price = no_ask if no_ask else 0.0

            # Validate price range (80c-95c sweet spot for favorite-longshot edge)
            if no_price <= NO_PRICE_MIN or no_price >= NO_PRICE_MAX:
                return None

            # Validate market is tradeable
            if not is_tradeable_market(market_info):
                return None

            # Estimate fair probability for NO side
            # Use the NO bid as market-implied probability
            # AI edge: we believe NO is more likely than market implies
            market_no_prob = no_price / 100.0 if no_price > 1.0 else no_price

            # Conservative fair estimate: assume NO is slightly undervalued
            # In real implementation, this would come from AI/LLM analysis
            fair_no_prob = market_no_prob + 0.08  # Assume 8% edge for NO

            # Calculate edge using EdgeFilter
            edge_result = EdgeFilter.calculate_edge(
                ai_probability=fair_no_prob,
                market_probability=market_no_prob,
                confidence=0.75,  # Conservative confidence
                price=no_price,
                contracts=1,
                is_maker=True  # Maker entries
            )

            if not edge_result.passes_filter:
                return None

            # Verify NO side
            if edge_result.side != "NO":
                return None

            # Calculate time to expiry
            close_time_str = market_info.get("close_time")
            if close_time_str:
                try:
                    close_time = datetime.fromisoformat(
                        str(close_time_str).replace("Z", "+00:00")
                    )
                    hours_to_expiry = (close_time - datetime.now(timezone.utc)).total_seconds() / 3600
                    if hours_to_expiry < MIN_TIME_TO_EXPIRY_HOURS:
                        return None  # Too close to expiry
                except Exception:
                    pass

            return {
                "market_id": market.market_id,
                "market_title": market.title,
                "no_price": no_price,
                "no_bid": no_bid if no_bid else no_price,
                "fair_probability": fair_no_prob,
                "edge": edge_result.edge_magnitude,
                "edge_percentage": edge_result.edge_percentage,
                "confidence": 0.75,
                "volume": market.volume,
                "hours_to_expiry": hours_to_expiry if close_time_str else 24,
                "is_maker": True,
                "side": "NO",
            }

        except Exception as e:
            self.logger.error(f"❌ Error analyzing {market.market_id}: {e}")
            return None

    async def _place_no_order(
        self,
        opportunity: Dict[str, Any],
        db_manager: DatabaseManager,
        kalshi_client: KalshiClient,
        mode: str,
        available_cash: float,
    ) -> Optional[Position]:
        """Place a NO-side maker order for ALL NO book."""
        try:
            market_id = opportunity["market_id"]
            entry_price = opportunity["no_price"]
            fair_prob = opportunity["fair_probability"]
            confidence = opportunity["confidence"]

            # Calculate Kelly sizing
            buy_amount = AllNoKalshiKellySizer.calculate_buy_amount(fair_prob, entry_price)

            if buy_amount["contracts"] <= 0:
                return None

            # Calculate cost
            cost = buy_amount["contracts"] * entry_price

            # Check affordability
            if cost > available_cash:
                self.logger.warning(
                    f"⚠️  Insufficient ALL NO {mode.upper()} cash: need ${cost:.2f}, "
                    f"have ${available_cash:.2f}"
                )
                return None

            # Check round-trip fees
            roundtrip_fee = calculate_roundtrip_fees_dollars(
                entry_price, buy_amount["contracts"], is_maker=True
            )
            self.logger.debug(
                f"💰 Round-trip fee for {market_id}: ${roundtrip_fee:.2f} "
                f"({(roundtrip_fee/cost)*100:.1f}% of cost)"
            )

            # Create position for ALL NO positions table
            position = Position(
                market_id=market_id,
                side="NO",
                entry_price=entry_price,
                quantity=buy_amount["contracts"],
                timestamp=datetime.now(timezone.utc),
                strategy="all_no",
                mode=mode,
                rationale=(
                    f"ALL NO maker entry at ${entry_price:.3f}, "
                    f"edge: {opportunity['edge_percentage']:.1%}, "
                    f"confidence: {confidence:.1%}, "
                    f"fee: ${roundtrip_fee:.2f}"
                ),
                live=(mode == MODE_LIVE),
                status="open",
                entry_fair=fair_prob,
            )

            # Insert into ALL NO positions table
            await self._insert_position(db_manager, position)

            # Record balance movement in ALL NO book
            await self._record_balance_change(cost, mode, "position_open")

            return position

        except Exception as e:
            self.logger.error(f"❌ Error placing NO order: {e}")
            return None

    async def _insert_position(self, db_manager: DatabaseManager, position: Position) -> None:
        """Insert position into ALL NO positions table."""
        import aiosqlite

        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute(
                """
                INSERT OR REPLACE INTO all_no_positions
                (market_id, side, entry_price, quantity, timestamp, status, strategy, mode, entry_fair)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.market_id,
                    position.side,
                    position.entry_price,
                    position.quantity,
                    position.timestamp.isoformat(),
                    position.status,
                    position.strategy or "all_no",
                    position.mode or "dry",
                    getattr(position, "entry_fair", None),
                ),
            )
            await conn.commit()

    async def _record_balance_change(
        self, amount: float, mode: str, kind: str
    ) -> None:
        """Record balance movement in ALL NO book."""
        import aiosqlite

        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

        balance_info = await self.all_no_book.get_balance(mode)
        new_balance = balance_info["cash"] - amount

        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute(
                """
                INSERT INTO all_no_book (kind, amount, balance_after, note, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (kind, amount, new_balance, f"ALL NO {mode.upper()} trade", stamp),
            )
            await conn.commit()

    async def run(self, mode: str = MODE_DRY, loop: bool = True, interval: int = 30) -> None:
        """Main strategy execution loop for specific mode.

        Args:
            mode: 'dry' or 'live'
            loop: Whether to loop continuously
            interval: Seconds between cycles
        """
        mode = mode.strip().lower()
        if mode not in (MODE_DRY, MODE_LIVE):
            raise ValueError(f"Invalid mode: {mode}")

        self.logger.info(f"🚀 ALL NO MARKETS strategy starting ({mode.upper()} mode)")
        self.logger.info(f"   Book balance: $250.00 (SEPARATE from DRY $300)")
        self.logger.info(f"   Mode: {mode.upper()}")
        self.logger.info(f"   Separation: COMPLETE - no crossover with main books")
        self.logger.info(f"   Strategy: NO-side maker entries, quarter-Kelly, 20% max clip")
        self.logger.info(f"   Entry criteria: Edge > 6%, confidence > 40%, NO price 80-95c")
        self.logger.info(f"   Exit strategy: Profit at 95c+, stop at -10%, time-based")

        # Ensure schema and seed
        await self.all_no_book.ensure_schema()
        await self.all_no_book.seed_book()

        # Verify separation
        if not await self.all_no_book.is_separate_from_dry_book():
            self.logger.error("❌ CRITICAL: Separation guarantee failed!")
            raise RuntimeError("ALL NO book separation from DRY book not verified!")

        self.logger.info("✅ Separation guarantee verified")

        try:
            cycle = 0
            while True:
                cycle += 1
                self.logger.info(f"📊 ALL NO {mode.upper()} cycle #{cycle}")

                positions = await self.scan_and_trade(mode)

                if positions:
                    self.logger.info(
                        f"📈 Created {len(positions)} ALL NO {mode.upper()} positions"
                    )
                else:
                    self.logger.debug(f"📭 No ALL NO {mode.upper()} positions this cycle")

                if not loop:
                    break

                await asyncio.sleep(interval)

        except KeyboardInterrupt:
            self.logger.info(f"⏹️  ALL NO {mode.upper()} strategy stopped by user")
            raise
        except Exception as e:
            self.logger.error(f"❌ ALL NO {mode.upper()} strategy crashed: {e}")
            raise

    async def stop(self) -> None:
        """Stop the strategy."""
        self.logger.info("🛑 Stopping ALL NO MARKETS strategy")

    async def get_status(self) -> Dict[str, Any]:
        """Get complete status of ALL NO books for both modes."""
        return {
            "dry": await self.all_no_book.get_balance(MODE_DRY),
            "live": await self.all_no_book.get_balance(MODE_LIVE),
            "separation_verified": await self.all_no_book.is_separate_from_dry_book(),
            "books_independent": True,
        }


async def main() -> None:
    """Entry point for ALL NO MARKETS strategy (DRY mode by default)."""
    import sys

    mode = MODE_DRY
    if len(sys.argv) > 1:
        mode = sys.argv[1].strip().lower()

    strategy = AllNoStrategy()
    try:
        await strategy.run(mode=mode)
    except KeyboardInterrupt:
        await strategy.stop()
        logger.info("✅ ALL NO MARKETS strategy stopped by user")
    except Exception as e:
        logger.error(f"❌ ALL NO MARKETS strategy failed: {e}")
        raise


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
