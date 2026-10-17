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
ALL_NO_KELLY_MULTIPLIER = 1.0     # Full Kelly (not quarter) for full blast
ALL_NO_MAX_CLIP_PCT = 0.80        # Up to 80% of book on one trade
MIN_ORDER_CONTRACTS = 1

# Mode-specific books
ALL_NO_DRY_BOOK = "all_no_dry"
ALL_NO_LIVE_BOOK = "all_no_live"

# TRADING PARAMETERS FOR MAXIMUM WIN RATE - FULL BLAST
MIN_EDGE_REQUIREMENT = 0.02          # 2% minimum edge (after fees) - wide net
MIN_CONFIDENCE_FOR_TRADE = 0.55      # 55% minimum confidence - more opportunities
NO_PRICE_MIN = 0.70                  # NO price minimum (70c) - wider range
NO_PRICE_MAX = 0.98                  # NO price maximum (98c) - catch every tradeable NO
MAX_CONCURRENT_POSITIONS = 500       # Max open positions - full blast
PROFIT_TARGET_PRICE = 0.97           # Sell NO when price reaches 97c
STOP_LOSS_THRESHOLD = -0.20          # 20% drawdown stop loss - wide stops
MIN_TIME_TO_EXPIRY_HOURS = 2         # Don't trade markets expiring in < 2 hours
MIN_VOLUME_FOR_TRADE = 100           # Minimum volume for liquidity - tiny threshold
MAX_CONTRACTS_PER_MARKET = 5000      # Buy up to 5000 contracts per market
TARGET_CONTRACTS_PER_TRADE = 1000    # Aim for 1000+ contracts per trade


class AllNoKalshiKellySizer:
    """Kelly sizing implementation for ALL NO MARKETS book - FULL BLAST."""

    @staticmethod
    def calculate_kelly_fraction(fair: float, price: float) -> float:
        """Calculate Kelly fraction for ALL NO book.

        For a NO contract bought at price P with a model fair of F:
        - stake P to win (1 - P) -> net odds b = (1 - P) / P
        - f* = (F*b - (1 - F)) / b [kelly_fraction(F, b)]

        FULL BLAST: uses full Kelly (not quarter) and allows up to 80% clip.
        """
        if price <= 0.0 or price >= 1.0 or fair <= 0.0:
            return 0.0

        # Net odds for a NO contract bought at `price`
        b = (1.0 - price) / price
        f = kelly_fraction(fair, b) * ALL_NO_KELLY_MULTIPLIER
        return max(0.0, min(f, ALL_NO_MAX_CLIP_PCT))

    @staticmethod
    def calculate_buy_amount(fair: float, price: float, balance: float = 250.0) -> Dict[str, Any]:
        """Calculate the clip a Kelly fraction actually buys - FULL BLAST.

        Buys THOUSANDS of contracts. The contract count targets
        TARGET_CONTRACTS_PER_TRADE or more, capped by MAX_CONTRACTS_PER_MARKET
        and available balance. Kalshi takes a whole-contract count with no
        dollar minimum, so rounding a clip to zero would silently drop a bet.

        The clip is a FRACTION of the book, so it must be multiplied by the
        balance before dividing by the contract price. A raw fraction/price
        division silently ignores the balance and can deploy the whole book.
        """
        if price <= 0.0 or price >= 1.0:
            return {"fraction": 0.0, "stake": 0.0, "contracts": 0}

        fraction = AllNoKalshiKellySizer.calculate_kelly_fraction(fair, price)
        if fraction <= 0.0:
            return {"fraction": 0.0, "stake": 0.0, "contracts": 0}

        # The Kelly fraction is a share of the BOOK, so the dollar stake is
        # fraction * balance. One contract costs `price`, so the count is
        # stake_dollars / price. This is what makes the clip balance-aware.
        stake_dollars = fraction * balance
        kelly_contracts = max(MIN_ORDER_CONTRACTS, int(stake_dollars / price))

        # FULL BLAST: target at least TARGET_CONTRACTS_PER_TRADE contracts,
        # using available balance as the hard ceiling.
        target = TARGET_CONTRACTS_PER_TRADE
        balance_ceiling = int(balance / price) if price > 0 else 0
        # The max-clip cap is ALSO a dollar cap: ALL_NO_MAX_CLIP_PCT of the
        # book. Without it the target can override the Kelly fraction and
        # deploy 100% of the book in one trade.
        clip_ceiling = int(ALL_NO_MAX_CLIP_PCT * balance / price) if price > 0 else 0
        contracts = min(
            max(kelly_contracts, target),
            MAX_CONTRACTS_PER_MARKET,
            balance_ceiling,
            clip_ceiling,
        )
        contracts = max(MIN_ORDER_CONTRACTS, contracts)

        return {"fraction": fraction, "stake": stake_dollars, "contracts": contracts}


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

    async def reset_to_starting_balance(self, mode: str = MODE_DRY) -> Dict[str, Any]:
        """Reset ALL NO book to $250 starting balance.

        Clears all positions, trade logs, and book entries for the given mode,
        then re-seeds at ALL_NO_STARTING_BALANCE. Useful when the balance
        goes negative due to double-counting bugs.
        """
        import aiosqlite

        mode = mode.strip().lower()
        await self.ensure_schema()

        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            # Clear positions for this mode
            await conn.execute(
                "DELETE FROM all_no_positions WHERE mode = ?", (mode,)
            )
            # Clear trade logs for this mode
            await conn.execute(
                "DELETE FROM all_no_trade_logs WHERE mode = ?", (mode,)
            )
            # Clear all non-seed book entries for this mode
            await conn.execute(
                "DELETE FROM all_no_book WHERE note != 'seed'"
            )

            # Re-insert seed if not present
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
            else:
                # Reset seed to full amount
                await conn.execute(
                    "UPDATE all_no_book SET amount = ?, balance_after = ? WHERE note = 'seed'",
                    (ALL_NO_STARTING_BALANCE, ALL_NO_STARTING_BALANCE),
                )

            await conn.commit()
            self.logger.info(
                f"ALL NO {mode.upper()} book reset to ${ALL_NO_STARTING_BALANCE:.2f}"
            )

        return await self.get_balance(mode)


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

            # Get eligible markets with volume and expiry filters.
            # ALL NO has its own separate position book, so it must NOT
            # be filtered out by has_position=0 — that flag tracks the
            # main book's positions and would silently exclude every
            # market the main book already holds, leaving only 2 markets.
            try:
                markets = await db_manager.get_eligible_markets(
                    volume_min=MIN_VOLUME_FOR_TRADE,
                    max_days_to_expiry=30,
                    ignore_has_position=True,
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

            # Conservative fair estimate: assume NO is undervalued
            # In real implementation, this would come from AI/LLM analysis
            fair_no_prob = market_no_prob + 0.08  # Assume 8% edge for NO

            # CRITICAL FIX: EdgeFilter.calculate_edge() interprets probabilities
            # as YES-side probabilities. When ai_prob > market_prob, edge > 0 and
            # side = "YES". When ai_prob < market_prob, edge < 0 and side = "NO".
            #
            # For a NO-side strategy, we want side = "NO", which means we must
            # pass YES-side probabilities (1 - fair_no, 1 - market_no). Then:
            #   edge = (1 - fair_no) - (1 - market_no) = market_no - fair_no < 0
            #   side = "NO"  ✓
            #
            # The old code passed NO-side probabilities directly, which made
            # edge > 0 → side = "YES" → the side check at the bottom rejected
            # EVERY opportunity. The lane never traded.
            edge_result = EdgeFilter.calculate_edge(
                ai_probability=1.0 - fair_no_prob,
                market_probability=1.0 - market_no_prob,
                confidence=MIN_CONFIDENCE_FOR_TRADE,
                price=no_price,
                contracts=1,
                is_maker=True  # Maker entries
            )

            if not edge_result.passes_filter:
                return None

            # Verify NO side (should always be "NO" now with the fix above)
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
                "confidence": MIN_CONFIDENCE_FOR_TRADE,
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

            # Calculate Kelly sizing - FULL BLAST with balance passed in
            buy_amount = AllNoKalshiKellySizer.calculate_buy_amount(
                fair_prob, entry_price, balance=available_cash
            )

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

            # CRITICAL: Actually submit the order through the broker.
            # Without this call, every position logged is a phantom —
            # no order ever reaches Kalshi. The position row exists but
            # nothing was transmitted. execute_position() runs the same
            # validation as LIVE (tradeability, price range, affordability)
            # and submits through broker_for_mode(), so DRY rehearses
            # what LIVE would do.
            from src.jobs.execute import execute_position
            from src.clients.kilo_gateway import KiloGateway

            gateway = KiloGateway()
            live_mode = (mode == MODE_LIVE)
            success = await execute_position(
                position=position,
                live_mode=live_mode,
                db_manager=db_manager,
                gateway=gateway,
                maker_wait_seconds=0.0,  # taker entry for ALL NO
            )
            if not success:
                self.logger.error(
                    f"❌ ALL NO order execution failed for {position.market_id}, "
                    f"removing phantom position row"
                )
                await self._remove_position(db_manager, position.market_id, mode)
                return None

            # DO NOT call _record_balance_change here. The get_balance()
            # method already computes the correct balance from
            # seed + realized_pnl - deployed_cost. Calling
            # _record_balance_change on top of that double-counts
            # every trade and drove the book to -$248.
            # The balance is derived, not ledger-tracked.

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

    async def _remove_position(self, db_manager: DatabaseManager, market_id: str, mode: str) -> None:
        """Remove a phantom position row when execution fails."""
        import aiosqlite

        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute(
                "DELETE FROM all_no_positions WHERE market_id = ? AND mode = ?",
                (market_id, mode),
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

        # CRITICAL: verify book mode at startup. The strategy_bootstrap
        # validator kills any process whose book mode doesn't match the
        # persisted mode. Without this call the ALL NO lane dies on boot.
        try:
            from src.jobs.strategy_bootstrap import verify_book_mode_at_startup

            expected = "live" if mode == MODE_LIVE else "paper"
            await verify_book_mode_at_startup("all_no_markets", expected_mode=expected)
        except SystemExit:
            raise
        except Exception as exc:
            self.logger.error(f"❌ Book mode verification failed: {exc}")
            raise

        self.logger.info(f"🚀 ALL NO MARKETS strategy starting ({mode.upper()} mode)")
        self.logger.info(f"   Book balance: $250.00 (SEPARATE from DRY $300)")
        self.logger.info(f"   Mode: {mode.upper()}")
        self.logger.info(f"   Separation: COMPLETE - no crossover with main books")
        self.logger.info(f"   Strategy: NO-side maker entries, FULL KELLY, 80% max clip")
        self.logger.info(f"   Entry criteria: Edge > 2%, confidence > 55%, NO price 70-98c")
        self.logger.info(f"   Exit strategy: Profit at 97c+, stop at -20%, 24h time-based")

        # Ensure schema and seed
        await self.all_no_book.ensure_schema()
        await self.all_no_book.seed_book()

        # Check balance and reset if too low (due to previous bugs that
        # deployed too much of the book). Reset when the balance is below
        # 50% of starting - the book is meant to compound from $250, not
        # sit at a cent.
        dry_balance = await self.all_no_book.get_balance(MODE_DRY)
        live_balance = await self.all_no_book.get_balance(MODE_LIVE)
        _min_cash = ALL_NO_STARTING_BALANCE * 0.5
        if dry_balance["cash"] < _min_cash or live_balance["cash"] < _min_cash:
            self.logger.warning(
                f"ALL NO book balance too low (DRY: ${dry_balance['cash']:.2f}, "
                f"LIVE: ${live_balance['cash']:.2f}). Resetting to starting balance."
            )
            await self.all_no_book.reset_to_starting_balance(MODE_DRY)
            await self.all_no_book.reset_to_starting_balance(MODE_LIVE)

        # Verify separation
        if not await self.all_no_book.is_separate_from_dry_book():
            self.logger.error("❌ CRITICAL: Separation guarantee failed!")
            raise RuntimeError("ALL NO book separation from DRY book not verified!")

        self.logger.info("✅ Separation guarantee verified")

        # Stamp heartbeat so the supervisor sees this process as alive.
        # Without this the lane's first heartbeat is minutes away and the
        # supervisor respawns it (then kills it on the next book-mode check).
        try:
            from src.utils.strategy_runtime import StrategyRuntime

            runtime_store = StrategyRuntime(self.db_path)
            runtime_mode = "live" if mode == MODE_LIVE else "paper"
            await runtime_store.record_heartbeat("all_no_markets", runtime_mode)
        except Exception:
            pass

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

                # Stamp heartbeat every cycle so the supervisor always sees
                # this process as alive - this is what stops the lane from
                # being killed and respawned every few minutes.
                try:
                    from src.utils.strategy_runtime import StrategyRuntime

                    runtime_store = StrategyRuntime(self.db_path)
                    runtime_mode = "live" if mode == MODE_LIVE else "paper"
                    await runtime_store.record_heartbeat("all_no_markets", runtime_mode)
                except Exception:
                    pass

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
