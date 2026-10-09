"""
Database manager for the Kalshi trading system.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiosqlite

from src.utils.logging_setup import TradingLoggerMixin


@dataclass
class Market:
    """Represents a market in the database."""

    market_id: str
    title: str
    yes_price: float
    no_price: float
    volume: int
    expiration_ts: int
    category: str
    status: str
    last_updated: datetime
    has_position: bool = False


@dataclass
class Position:
    """Represents a trading position."""

    market_id: str
    side: str  # "YES" or "NO"
    entry_price: float
    quantity: int
    timestamp: datetime
    rationale: Optional[str] = None
    confidence: Optional[float] = None
    live: bool = False
    status: str = "open"  # open, closed, pending
    id: Optional[int] = None
    strategy: Optional[str] = None  # Strategy that created this position

    # Enhanced exit strategy fields
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    max_hold_hours: Optional[int] = None  # Maximum hours to hold position
    target_confidence_change: Optional[float] = None  # Exit if confidence drops by this amount

    # Which book this position belongs to: "dry" (simulated) or "live" (real).
    # Kept separate from `live`, which only records whether a real order was
    # ever sent. Overloading `live` meant live-only trackers could not see DRY
    # positions, so DRY exits never evaluated.
    mode: Optional[str] = None

    # CALIBRATION: the model's fair probability at entry, as computed from the
    # live stream. `confidence` on updown lanes stores the edge magnitude; the
    # fair is the number every win-rate-by-band report needs. Appended last so
    # positional construction stays compatible.
    entry_fair: Optional[float] = None


def _position_from_row(row: aiosqlite.Row) -> Position:
    """Build a Position from a `SELECT *` row by column name.

    Positional indexing is unsafe here: the positions table has 15 columns and
    previously the map was off by one from index 10, so stop_loss_price came
    back holding the strategy string.
    """
    data = dict(row)
    ts = data.get("timestamp")
    if isinstance(ts, str):
        data["timestamp"] = datetime.fromisoformat(ts)
    # Tolerate columns the dataclass does not declare, so a future migration
    # cannot turn a read into a TypeError.
    known = {f for f in Position.__dataclass_fields__}
    return Position(**{k: v for k, v in data.items() if k in known})


def _resolve_current_mode(db_path: str) -> str:
    """Read the persisted DRY/LIVE mode synchronously.

    Exit management has to know which book to manage before it can pick
    positions, and it cannot await inside a sync query builder. Defaults to DRY
    if the mode store is unreadable - the safe answer, since acting on the live
    book by accident is the expensive error.
    """
    import sqlite3

    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT value FROM runtime_config WHERE key = 'trading_mode'"
            ).fetchone()
        finally:
            conn.close()
        if row and row[0] in ("dry", "live"):
            return str(row[0])
    except Exception:  # noqa: BLE001 - fall through to the safe default
        pass
    return "dry"


@dataclass
class TradeLog:
    """Represents a closed trade for logging and analysis."""

    market_id: str
    side: str
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    entry_timestamp: datetime
    exit_timestamp: datetime
    rationale: str
    strategy: Optional[str] = None  # Strategy that created this trade
    # Why the position was closed (take_profit, stop_loss, market_resolution,
    # time_limit, manual). Distinguishes an intentional exit from a mystery.
    exit_reason: Optional[str] = None
    # Which book the close belongs to, so simulated P&L never inflates the
    # live record.
    mode: Optional[str] = None
    # LIVE-only: estimated Kalshi fees for the round trip (entry taker + exit).
    # DRY rows store 0/None and keep gross PnL; LIVE rows store the estimate
    # and net it out of pnl so the dashboard shows fee-net results.
    fee_paid: Optional[float] = None
    id: Optional[int] = None
    # CALIBRATION: the entry fair carried through from the position, so a
    # closed trade can always be bucketed by the model's own probability.
    entry_fair: Optional[float] = None


def _tradelog_from_row(row: aiosqlite.Row) -> TradeLog:
    """Build a TradeLog from a row, ignoring columns the dataclass lacks.

    Adding a migration column without a matching dataclass field would turn
    every read into a TypeError, which is exactly what happened to exit_reason.
    """
    data = dict(row)
    for key in ("entry_timestamp", "exit_timestamp"):
        if isinstance(data.get(key), str):
            data[key] = datetime.fromisoformat(data[key])
    known = set(TradeLog.__dataclass_fields__)
    return TradeLog(**{k: v for k, v in data.items() if k in known})


@dataclass
class LLMQuery:
    """Represents an LLM query and response for analysis."""

    timestamp: datetime
    strategy: str  # Which strategy made the query
    query_type: str  # Type of query (market_analysis, movement_prediction, etc.)
    market_id: Optional[str]  # Market being analyzed (if applicable)
    prompt: str  # The prompt sent to LLM
    response: str  # LLM response
    tokens_used: Optional[int] = None  # Tokens consumed
    cost_usd: Optional[float] = None  # Cost in USD
    confidence_extracted: Optional[float] = None  # Confidence if extracted
    decision_extracted: Optional[str] = None  # Decision if extracted
    id: Optional[int] = None


# Seconds a writer waits for a competing writer before giving up. SQLite's
# default is 5s, which six strategy processes plus the dashboard exhaust
# routinely: the crash was "database is locked" during startup, twice.
BUSY_TIMEOUT_SECONDS = 30
# A blocked *reader* must fail fast. Every dashboard poll opens connections, and
# with six strategies writing to one file a reader that waits the full write
# timeout turns the page into a 27-second hang - long enough that buttons look
# dead and the figures look blank. Writes still get the long timeout, because a
# write that gives up loses work.
READ_BUSY_TIMEOUT_SECONDS = 4


def connect_readonly(path: str, **kwargs):
    """A connection for reads: short busy timeout, so a page never hangs."""
    kwargs.setdefault("timeout", READ_BUSY_TIMEOUT_SECONDS)
    return aiosqlite.connect(path, **kwargs)


async def _apply_pragmas(conn):
    """Put the database into WAL, where concurrent access is safe.

    WAL lets readers run while a writer holds the lock. Under the default
    rollback journal a single write blocks every other process, which is how six
    strategies plus the dashboard turned into `database is locked` failures.
    The mode is persistent in the file, so this only has to succeed once.
    """
    try:
        cur = await conn.execute("PRAGMA journal_mode=WAL")
        await cur.fetchall()
        await conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:  # noqa: BLE001 - pragmas are best-effort
        pass


def connect(path: str, **kwargs):
    """Open a connection that waits for a competing writer instead of failing.

    SQLite gives up on a locked database after a few seconds by default, which is
    roughly how long a busy strategy cycle holds the write lock. `timeout` maps
    to `busy_timeout`, so a blocked writer waits rather than raising.

    Deliberately *not* a coroutine: callers use `async with connect(...)`, and
    awaiting the connection first would start aiosqlite's worker thread twice.
    """
    kwargs.setdefault("timeout", BUSY_TIMEOUT_SECONDS)
    return aiosqlite.connect(path, **kwargs)


def _default_db_path() -> str:
    """The database path, honouring DB_PATH.

    The literal default "trading_system.db" silently overrode the deployment's
    DB_PATH, so a strategy spawned by the dashboard wrote to the container
    filesystem while the dashboard read the volume-mounted database. The two then
    disagreed about everything: 644 markets ingested in one file, "0 markets" on
    the page, and a pipeline that looked like it had never run.
    """
    import os

    return os.environ.get("DB_PATH", "").strip() or "trading_system.db"


class DatabaseManager(TradingLoggerMixin):
    """Manages database operations for the trading system."""

    def __init__(self, db_path: Optional[str] = None):
        """Initialize database connection."""
        self.db_path = db_path or _default_db_path()
        self.logger.info("Initializing database manager", db_path=self.db_path)

    async def initialize(self) -> None:
        """Initialize database schema and run migrations."""
        # Ensure the parent directory exists (e.g. data/ on a fresh clone)
        import os

        db_dir = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(db_dir, exist_ok=True)

        async with connect(self.db_path) as db:
            # WAL before anything else: under the default rollback journal a
            # single writer blocks every reader, and six strategy processes plus
            # the dashboard share this one file on a Railway volume.
            await _apply_pragmas(db)
            await self._create_tables(db)
            await self._run_migrations(db)
            await db.commit()
        self.logger.info("Database initialized successfully")

    async def _migrate_existing_strategy_data(self, db: aiosqlite.Connection) -> None:
        """Migrate existing position data to include strategy information."""
        try:
            # Update positions based on rationale patterns
            await db.execute(
                """
                UPDATE positions 
                SET strategy = 'quick_flip_scalping' 
                WHERE strategy IS NULL AND rationale LIKE 'QUICK FLIP:%'
            """
            )

            await db.execute(
                """
                UPDATE positions 
                SET strategy = 'portfolio_optimization' 
                WHERE strategy IS NULL AND rationale LIKE 'Portfolio optimization allocation:%'
            """
            )

            await db.execute(
                """
                UPDATE positions 
                SET strategy = 'market_making' 
                WHERE strategy IS NULL AND (
                    rationale LIKE '%market making%' OR 
                    rationale LIKE '%spread profit%'
                )
            """
            )

            await db.execute(
                """
                UPDATE positions 
                SET strategy = 'directional_trading' 
                WHERE strategy IS NULL AND (
                    rationale LIKE 'High-confidence%' OR
                    rationale LIKE '%near-expiry%' OR
                    rationale LIKE '%decision%'
                )
            """
            )

            # Update trade_logs similarly
            await db.execute(
                """
                UPDATE trade_logs 
                SET strategy = 'quick_flip_scalping' 
                WHERE strategy IS NULL AND rationale LIKE 'QUICK FLIP:%'
            """
            )

            await db.execute(
                """
                UPDATE trade_logs 
                SET strategy = 'portfolio_optimization' 
                WHERE strategy IS NULL AND rationale LIKE 'Portfolio optimization allocation:%'
            """
            )

            await db.execute(
                """
                UPDATE trade_logs 
                SET strategy = 'market_making' 
                WHERE strategy IS NULL AND (
                    rationale LIKE '%market making%' OR 
                    rationale LIKE '%spread profit%'
                )
            """
            )

            await db.execute(
                """
                UPDATE trade_logs 
                SET strategy = 'directional_trading' 
                WHERE strategy IS NULL AND (
                    rationale LIKE 'High-confidence%' OR
                    rationale LIKE '%near-expiry%' OR
                    rationale LIKE '%decision%'
                )
            """
            )

            self.logger.info("Migrated existing position/trade data with strategy information")

        except Exception as e:
            self.logger.error(f"Error migrating existing strategy data: {e}")

    async def _create_tables(self, db: aiosqlite.Connection) -> None:
        """Create all database tables."""

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS markets (
                market_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                yes_price REAL NOT NULL,
                no_price REAL NOT NULL,
                volume INTEGER NOT NULL,
                expiration_ts INTEGER NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL,
                last_updated TEXT NOT NULL,
                has_position BOOLEAN NOT NULL DEFAULT 0
            )
        """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                rationale TEXT,
                confidence REAL,
                live BOOLEAN NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                strategy TEXT,
                stop_loss_price REAL,
                take_profit_price REAL,
                max_hold_hours INTEGER,
                target_confidence_change REAL,
                UNIQUE(market_id, side)
            )
        """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                pnl REAL NOT NULL,
                entry_timestamp TEXT NOT NULL,
                exit_timestamp TEXT NOT NULL,
                rationale TEXT,
                strategy TEXT,
                fee_paid REAL DEFAULT 0
            )
        """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS market_analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                analysis_timestamp TEXT NOT NULL,
                decision_action TEXT NOT NULL,
                confidence REAL,
                cost_usd REAL NOT NULL,
                analysis_type TEXT NOT NULL DEFAULT 'standard'
            )
        """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_cost_tracking (
                date TEXT PRIMARY KEY,
                total_ai_cost REAL NOT NULL DEFAULT 0.0,
                analysis_count INTEGER NOT NULL DEFAULT 0,
                decision_count INTEGER NOT NULL DEFAULT 0
            )
        """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                strategy TEXT NOT NULL,
                query_type TEXT NOT NULL,
                market_id TEXT,
                prompt TEXT NOT NULL,
                response TEXT NOT NULL,
                tokens_used INTEGER,
                cost_usd REAL,
                confidence_extracted REAL,
                decision_extracted TEXT
            )
        """
        )

        # Add analysis_reports table for performance tracking
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                health_score REAL NOT NULL,
                critical_issues INTEGER DEFAULT 0,
                warnings INTEGER DEFAULT 0,
                action_items INTEGER DEFAULT 0,
                report_file TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # Create indices for performance
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_analyses_market_id ON market_analyses(market_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_analyses_timestamp ON market_analyses(analysis_timestamp)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_cost_date ON daily_cost_tracking(date)"
        )

        # Run migrations to ensure schema is up to date
        await self._run_migrations(db)

        self.logger.info("Tables created or already exist.")

    async def _run_migrations(self, db: aiosqlite.Connection) -> None:
        """Bring an existing database up to the current schema.

        Each step is independent: one failing migration must not skip the rest.
        Previously everything lived in a single try block behind a duplicate
        method definition, so a failure part-way through silently left the rest
        unapplied - which is how `mode`, `strategy` and `blocked_trades` all
        ended up missing from a freshly initialized database.
        """

        async def columns_of(table: str) -> List[str]:
            cur = await db.execute(f"PRAGMA table_info({table})")
            return [c[1] for c in await cur.fetchall()]

        async def add_column(table: str, column: str, decl: str) -> None:
            if column not in await columns_of(table):
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                # Commit DDL immediately: later migration steps (and any step
                # that opens its own connection) must SEE the new column.
                # Uncommitted ALTERs were invisible across connections, which
                # made the strategy-backfill step die with "no column named
                # entry_fair" on the first init pass.
                await db.commit()
                self.logger.info(f"Added {column} to {table}")

        async def ensure_table(name: str, ddl: str) -> None:
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
            )
            if not await cur.fetchone():
                await db.execute(ddl)
                await db.commit()
                self.logger.info(f"Created {name} table")

        steps: List[Any] = [
            lambda: add_column("positions", "strategy", "TEXT"),
            lambda: add_column("positions", "stop_loss_price", "REAL"),
            lambda: add_column("positions", "take_profit_price", "REAL"),
            lambda: add_column("positions", "max_hold_hours", "INTEGER"),
            lambda: add_column("positions", "target_confidence_change", "REAL"),
            lambda: add_column("positions", "mode", "TEXT"),
            # CALIBRATION: the entry fair per position and per closed trade.
            lambda: add_column("positions", "entry_fair", "REAL"),
            lambda: add_column("trade_logs", "entry_fair", "REAL"),
            lambda: add_column("trade_logs", "strategy", "TEXT"),
            # cli.py queries blocked_trades unguarded, but it was only ever
            # created by PortfolioEnforcer - any database that had not run the
            # enforcer raised "no such table".
            lambda: ensure_table(
                "blocked_trades",
                """
                CREATE TABLE IF NOT EXISTS blocked_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    details TEXT,
                    market_data TEXT
                )
            """,
            ),
            lambda: add_column("trade_logs", "exit_reason", "TEXT"),
            lambda: add_column("trade_logs", "mode", "TEXT"),
            # LIVE-only fee accounting. Additive column, default 0: existing
            # DRY rows read back as 0/None and their gross PnL is untouched.
            lambda: add_column("trade_logs", "fee_paid", "REAL DEFAULT 0"),
            # Backfill the book for pre-existing rows. `live` is the best signal
            # available for them: live=1 meant a real order was sent.
            lambda: self._backfill_position_mode(db),
            lambda: self._migrate_existing_strategy_data(db),
            # Drop the UNIQUE(market_id, side) constraint on positions. It was the
            # deepest layer of the "one clip per market" guard: even with every
            # caller-side check removed, a second row for the same market and side
            # was impossible because the schema rejected it. Strategies that need
            # to pyramid - btc_updown buying more of the same contract while the
            # model still favours it - were structurally unable to. Their own
            # in-code guards still prevent duplicate entry everywhere else.
            lambda: self._drop_positions_market_side_unique(db),
        ]

        for step in steps:
            try:
                await step()
            except Exception as e:  # noqa: BLE001 - keep going
                self.logger.error(f"Migration step failed: {type(e).__name__}: {e}")

        await db.commit()

    async def _backfill_position_mode(self, db: aiosqlite.Connection) -> None:
        """Give pre-mode positions a book, derived from `live`."""
        await db.execute("UPDATE positions SET mode = 'live' WHERE mode IS NULL AND live = 1")
        await db.execute("UPDATE positions SET mode = 'dry' WHERE mode IS NULL AND live = 0")

    async def _drop_positions_market_side_unique(self, db: aiosqlite.Connection) -> None:
        """Rebuild `positions` without the UNIQUE(market_id, side) constraint.

        SQLite cannot drop a UNIQUE constraint in place, so the table is rebuilt:
        rename the old one, create the new shape, copy the rows, drop the old.
        The column list is taken from PRAGMA so a database from any point in the
        migration history copies correctly; the new table then has exactly the
        current schema, minus the constraint.
        """
        cur = await db.execute("PRAGMA index_list(positions)")
        indexes = await cur.fetchall()
        # rows: seq, name, unique, origin, partial
        has_unique_constraint = any(
            int(r[2]) == 1 and r[3] == "u" for r in indexes
        )
        if not has_unique_constraint:
            return

        cur = await db.execute("PRAGMA table_info(positions)")
        columns = [c[1] for c in await cur.fetchall()]
        col_list = ", ".join(columns)

        await db.execute("ALTER TABLE positions RENAME TO positions_legacy")
        await db.execute(
            """
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                rationale TEXT,
                confidence REAL,
                live BOOLEAN NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                strategy TEXT,
                stop_loss_price REAL,
                take_profit_price REAL,
                max_hold_hours INTEGER,
                target_confidence_change REAL,
                mode TEXT
            )
            """
        )
        await db.execute(
            f"INSERT INTO positions ({col_list}) SELECT {col_list} FROM positions_legacy"
        )
        await db.execute("DROP TABLE positions_legacy")
        self.logger.info("Rebuilt positions without UNIQUE(market_id, side)")

    async def upsert_markets(self, markets: List[Market]):
        """
        Upsert a list of markets into the database.

        Args:
            markets: A list of Market dataclass objects.
        """
        async with connect(self.db_path) as db:
            # SQLite STRFTIME arguments needs to be a string
            # and asdict converts datetime to datetime object
            # so we need to convert it to string manually
            market_dicts = []
            for m in markets:
                market_dict = asdict(m)
                market_dict["last_updated"] = m.last_updated.isoformat()
                market_dicts.append(market_dict)

            await db.executemany(
                """
                INSERT INTO markets (market_id, title, yes_price, no_price, volume, expiration_ts, category, status, last_updated, has_position)
                VALUES (:market_id, :title, :yes_price, :no_price, :volume, :expiration_ts, :category, :status, :last_updated, :has_position)
                ON CONFLICT(market_id) DO UPDATE SET
                    title=excluded.title,
                    yes_price=excluded.yes_price,
                    no_price=excluded.no_price,
                    volume=excluded.volume,
                    expiration_ts=excluded.expiration_ts,
                    category=excluded.category,
                    status=excluded.status,
                    last_updated=excluded.last_updated,
                    has_position=excluded.has_position
            """,
                market_dicts,
            )
            await db.commit()
            self.logger.info(f"Upserted {len(markets)} markets.")

    async def get_eligible_markets(self, volume_min: int, max_days_to_expiry: int) -> List[Market]:
        """
        Get markets that are eligible for trading.

        Args:
            volume_min: Minimum trading volume.
            max_days_to_expiry: Maximum days to expiration.

        Returns:
            A list of eligible markets.
        """
        now_ts = int(datetime.now().timestamp())
        max_expiry_ts = now_ts + (max_days_to_expiry * 24 * 60 * 60)

        async with connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM markets
                WHERE
                    volume >= ? AND
                    expiration_ts > ? AND
                    expiration_ts <= ? AND
                    status = 'active' AND
                    has_position = 0
            """,
                (volume_min, now_ts, max_expiry_ts),
            )
            rows = await cursor.fetchall()

            markets = []
            for row in rows:
                market_dict = dict(row)
                market_dict["last_updated"] = datetime.fromisoformat(market_dict["last_updated"])
                markets.append(Market(**market_dict))
            return markets

    async def get_markets_with_positions(self) -> set[str]:
        """
        Returns a set of market IDs that have associated open positions.
        """
        async with connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT DISTINCT market_id FROM positions WHERE status IN ('open', 'pending')
            """
            )
            rows = await cursor.fetchall()
            return {row[0] for row in rows}

    async def is_position_opening_for_market(self, market_id: str) -> bool:
        """
        Checks if a position is currently being opened for a given market.
        This is to prevent race conditions where multiple workers try to open a position for the same market.
        """
        async with connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT market_id FROM positions WHERE market_id = ? AND status = 'pending' LIMIT 1
            """,
                (market_id,),
            )
            row = await cursor.fetchone()
            return row is not None

    async def get_open_non_live_positions(self) -> List[Position]:
        """
        Get all positions that are open and not live (i.e. the simulated book).

        Returns:
            A list of Position objects.
        """
        async with connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM positions WHERE status = 'open' AND live = 0")
            rows = await cursor.fetchall()
            return [_position_from_row(r) for r in rows]

    async def get_open_live_positions(self, mode: Optional[str] = None) -> List[Position]:
        """
        Get positions eligible for exit management.

        Defaults to the book currently being traded, resolved from the `mode`
        column rather than `live`. Previously this filtered `live = 1`, which
        made it structurally blind to DRY positions - DRY trades opened and
        were never exited. Pass `mode=` to target one book explicitly.

        Returns:
            A list of Position objects.
        """
        import os

        # The manager's OWN path is authoritative. This used to resolve the
        # book from the global DB_PATH env var first, even when the manager
        # had been constructed with an explicit path - which is how test
        # databases, temp books and tools silently read the developer's
        # repo-root trading_system.db instead (the "local-DB pollution"
        # pattern). In production the manager's path comes from that same
        # env var, so this is byte-identical there.
        resolved = mode or _resolve_current_mode(self.db_path)
        # An unlabelled row (mode IS NULL / '') is read as DRY, which is what the
        # dashboard's COALESCE(NULLIF(mode,''),'dry') does with it. Letting the
        # LIVE query match those rows too meant a position the books could not
        # attribute was eligible for real sell orders in both books at once. DRY
        # still inherits them (they are simulated by definition); LIVE no longer
        # touches a row it cannot prove is its own.
        #
        # The LIVE query keys off `mode` alone, not the legacy `live` flag:
        # rows the ladder books as mode='live' with live=0 (a local clip the
        # canonical order path recorded without flipping the flag) were
        # invisible to every exit path while still pinning deployed capital.
        # That is how 29 dead YES clips sat unclosable for a day.
        book_clause = (
            "(mode = ? OR mode IS NULL OR mode = '')" if resolved == "dry" else "(mode = ?)"
        )
        async with connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM positions WHERE status = 'open'"
                f" AND {book_clause}",
                (resolved,),
            )
            rows = await cursor.fetchall()
            return [_position_from_row(r) for r in rows]

    async def get_open_positions_for_mode(self, mode: str) -> List[Position]:
        """Open positions in one specific book."""
        return await self.get_open_positions(mode=mode)

    async def update_position_status(self, position_id: int, status: str):
        """
        Updates the status of a position.

        Args:
            position_id: The id of the position to update.
            status: The new status ('closed', 'voided').
        """
        async with connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE positions SET status = ? WHERE id = ?
            """,
                (status, position_id),
            )
            await db.commit()
            self.logger.info(f"Updated position {position_id} status to {status}.")

    async def get_position_by_market_id(self, market_id: str) -> Optional[Position]:
        """
        Get a position by market ID.

        Args:
            market_id: The ID of the market.

        Returns:
            A Position object if found, otherwise None.
        """
        async with connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM positions WHERE market_id = ? AND status = 'open' LIMIT 1",
                (market_id,),
            )
            row = await cursor.fetchone()
            if row:
                return _position_from_row(row)
            return None

    async def get_position_by_market_and_side(
        self, market_id: str, side: str
    ) -> Optional[Position]:
        """
        Get a position by market ID and side.

        Args:
            market_id: The ID of the market.
            side: The side of the position ('YES' or 'NO').

        Returns:
            A Position object if found, otherwise None.
        """
        async with connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM positions WHERE market_id = ? AND side = ? AND status = 'open'",
                (market_id, side),
            )
            row = await cursor.fetchone()
            if row:
                return _position_from_row(row)
            return None

    async def add_trade_log(self, trade_log: TradeLog) -> None:
        """
        Add a trade log entry.

        Args:
            trade_log: The trade log to add.
        """
        trade_dict = asdict(trade_log)
        trade_dict["entry_timestamp"] = trade_log.entry_timestamp.isoformat()
        trade_dict["exit_timestamp"] = trade_log.exit_timestamp.isoformat()

        async with connect(self.db_path) as db:
            try:
                await db.execute(
                    """
                    INSERT INTO trade_logs (market_id, side, entry_price, exit_price, quantity, pnl, entry_timestamp, exit_timestamp, rationale, strategy, exit_reason, mode, fee_paid, entry_fair)
                    VALUES (:market_id, :side, :entry_price, :exit_price, :quantity, :pnl, :entry_timestamp, :exit_timestamp, :rationale, :strategy, :exit_reason, :mode, :fee_paid, :entry_fair)
                """,
                    {
                        **trade_dict,
                        "fee_paid": trade_dict.get("fee_paid") or 0.0,
                        "entry_fair": trade_dict.get("entry_fair"),
                    },
                )
            except Exception:  # noqa: BLE001 - pre-migration DB without fee_paid
                await db.execute(
                    """
                    INSERT INTO trade_logs (market_id, side, entry_price, exit_price, quantity, pnl, entry_timestamp, exit_timestamp, rationale, strategy, exit_reason, mode)
                    VALUES (:market_id, :side, :entry_price, :exit_price, :quantity, :pnl, :entry_timestamp, :exit_timestamp, :rationale, :strategy, :exit_reason, :mode)
                """,
                    trade_dict,
                )
            await db.commit()
            self.logger.info(f"Added trade log for market {trade_log.market_id}.")

    async def get_performance_by_strategy(self) -> Dict[str, Dict]:
        """
        Get performance metrics broken down by strategy.

        Returns:
            Dictionary with strategy names as keys and performance metrics as values.
        """
        async with connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Check if strategy column exists in trade_logs
            cursor = await db.execute("PRAGMA table_info(trade_logs)")
            columns = await cursor.fetchall()
            column_names = [col[1] for col in columns]
            has_strategy_in_trades = "strategy" in column_names

            completed_stats = []

            if has_strategy_in_trades:
                # Get stats from completed trades (trade_logs)
                cursor = await db.execute(
                    """
                    SELECT 
                        strategy,
                        COUNT(*) as trade_count,
                        SUM(pnl) as total_pnl,
                        AVG(pnl) as avg_pnl,
                        SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                        SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losing_trades,
                        MAX(pnl) as best_trade,
                        MIN(pnl) as worst_trade
                    FROM trade_logs 
                    WHERE strategy IS NOT NULL
                    GROUP BY strategy
                """
                )
                completed_stats = await cursor.fetchall()
            else:
                # If no strategy column, create a generic entry
                cursor = await db.execute(
                    """
                    SELECT 
                        'legacy_trades' as strategy,
                        COUNT(*) as trade_count,
                        SUM(pnl) as total_pnl,
                        AVG(pnl) as avg_pnl,
                        SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                        SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losing_trades,
                        MAX(pnl) as best_trade,
                        MIN(pnl) as worst_trade
                    FROM trade_logs
                """
                )
                result = await cursor.fetchone()
                if result and result["trade_count"] > 0:
                    completed_stats = [result]

            # Check if strategy column exists in positions
            cursor = await db.execute("PRAGMA table_info(positions)")
            columns = await cursor.fetchall()
            column_names = [col[1] for col in columns]
            has_strategy_in_positions = "strategy" in column_names

            open_stats = []

            if has_strategy_in_positions:
                # Get current open positions by strategy
                cursor = await db.execute(
                    """
                    SELECT 
                        strategy,
                        COUNT(*) as open_positions,
                        SUM(quantity * entry_price) as capital_deployed
                    FROM positions 
                    WHERE status = 'open' AND strategy IS NOT NULL
                    GROUP BY strategy
                """
                )
                open_stats = await cursor.fetchall()
            else:
                # If no strategy column, create a generic entry
                cursor = await db.execute(
                    """
                    SELECT 
                        'legacy_positions' as strategy,
                        COUNT(*) as open_positions,
                        SUM(quantity * entry_price) as capital_deployed
                    FROM positions 
                    WHERE status = 'open'
                """
                )
                result = await cursor.fetchone()
                if result and result["open_positions"] > 0:
                    open_stats = [result]

            # Combine the results
            performance = {}

            # Add completed trade stats
            for row in completed_stats:
                strategy = row["strategy"] or "unknown"
                win_rate = (
                    (row["winning_trades"] / row["trade_count"]) * 100
                    if row["trade_count"] > 0
                    else 0
                )

                performance[strategy] = {
                    "completed_trades": row["trade_count"],
                    "total_pnl": row["total_pnl"],
                    "avg_pnl_per_trade": row["avg_pnl"],
                    "win_rate_pct": win_rate,
                    "winning_trades": row["winning_trades"],
                    "losing_trades": row["losing_trades"],
                    "best_trade": row["best_trade"],
                    "worst_trade": row["worst_trade"],
                    "open_positions": 0,
                    "capital_deployed": 0.0,
                }

            # Add open position stats
            for row in open_stats:
                strategy = row["strategy"] or "unknown"
                if strategy not in performance:
                    performance[strategy] = {
                        "completed_trades": 0,
                        "total_pnl": 0.0,
                        "avg_pnl_per_trade": 0.0,
                        "win_rate_pct": 0.0,
                        "winning_trades": 0,
                        "losing_trades": 0,
                        "best_trade": 0.0,
                        "worst_trade": 0.0,
                        "open_positions": 0,
                        "capital_deployed": 0.0,
                    }

                performance[strategy]["open_positions"] = row["open_positions"]
                performance[strategy]["capital_deployed"] = row["capital_deployed"]

            return performance

    async def log_llm_query(self, llm_query: LLMQuery) -> None:
        """Log an LLM query and response for analysis."""
        try:
            query_dict = asdict(llm_query)
            query_dict["timestamp"] = llm_query.timestamp.isoformat()

            async with connect(self.db_path) as db:
                await db.execute(
                    """
                    INSERT INTO llm_queries (
                        timestamp, strategy, query_type, market_id, prompt, response,
                        tokens_used, cost_usd, confidence_extracted, decision_extracted
                    ) VALUES (
                        :timestamp, :strategy, :query_type, :market_id, :prompt, :response,
                        :tokens_used, :cost_usd, :confidence_extracted, :decision_extracted
                    )
                """,
                    query_dict,
                )
                await db.commit()

        except Exception as e:
            self.logger.error(f"Error logging LLM query: {e}")

    async def get_llm_queries(
        self, strategy: Optional[str] = None, hours_back: int = 24, limit: int = 100
    ) -> List[LLMQuery]:
        """Get recent LLM queries, optionally filtered by strategy."""
        try:
            cutoff_time = datetime.now() - timedelta(hours=hours_back)

            async with connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row

                # Check if llm_queries table exists
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_queries'"
                )
                table_exists = await cursor.fetchone()

                if not table_exists:
                    self.logger.info(
                        "LLM queries table doesn't exist yet - will be created on first query"
                    )
                    return []

                if strategy:
                    cursor = await db.execute(
                        """
                        SELECT * FROM llm_queries 
                        WHERE strategy = ? AND timestamp >= ?
                        ORDER BY timestamp DESC LIMIT ?
                    """,
                        (strategy, cutoff_time.isoformat(), limit),
                    )
                else:
                    cursor = await db.execute(
                        """
                        SELECT * FROM llm_queries 
                        WHERE timestamp >= ?
                        ORDER BY timestamp DESC LIMIT ?
                    """,
                        (cutoff_time.isoformat(), limit),
                    )

                rows = await cursor.fetchall()

                queries = []
                for row in rows:
                    query_dict = dict(row)
                    query_dict["timestamp"] = datetime.fromisoformat(query_dict["timestamp"])
                    queries.append(LLMQuery(**query_dict))

                return queries

        except Exception as e:
            self.logger.error(f"Error getting LLM queries: {e}")
            return []

    async def get_llm_stats_by_strategy(self) -> Dict[str, Dict]:
        """Get LLM usage statistics by strategy."""
        try:
            async with connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row

                # Check if llm_queries table exists
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_queries'"
                )
                table_exists = await cursor.fetchone()

                if not table_exists:
                    self.logger.info(
                        "LLM queries table doesn't exist yet - will be created on first query"
                    )
                    return {}

                cursor = await db.execute(
                    """
                    SELECT 
                        strategy,
                        COUNT(*) as query_count,
                        SUM(tokens_used) as total_tokens,
                        SUM(cost_usd) as total_cost,
                        AVG(confidence_extracted) as avg_confidence,
                        MIN(timestamp) as first_query,
                        MAX(timestamp) as last_query
                    FROM llm_queries 
                    WHERE timestamp >= datetime('now', '-7 days')
                    GROUP BY strategy
                """
                )

                rows = await cursor.fetchall()

                stats = {}
                for row in rows:
                    stats[row["strategy"]] = {
                        "query_count": row["query_count"],
                        "total_tokens": row["total_tokens"] or 0,
                        "total_cost": row["total_cost"] or 0.0,
                        "avg_confidence": row["avg_confidence"] or 0.0,
                        "first_query": row["first_query"],
                        "last_query": row["last_query"],
                    }

                return stats

        except Exception as e:
            self.logger.error(f"Error getting LLM stats: {e}")
            return {}

    async def close(self):
        """Close database connections (no-op for aiosqlite)."""
        # aiosqlite doesn't require explicit closing of connections
        # since we use context managers, but we provide this method
        # for compatibility with other code that expects it
        pass

    async def record_market_analysis(
        self,
        market_id: str,
        decision_action: str,
        confidence: float,
        cost_usd: float,
        analysis_type: str = "standard",
    ) -> None:
        """Record that a market was analyzed to prevent duplicate analysis."""
        now = datetime.now().isoformat()
        today = datetime.now().strftime("%Y-%m-%d")

        async with connect(self.db_path) as db:
            # Record the analysis
            await db.execute(
                """
                INSERT INTO market_analyses (market_id, analysis_timestamp, decision_action, confidence, cost_usd, analysis_type)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (market_id, now, decision_action, confidence, cost_usd, analysis_type),
            )

            # Update daily cost tracking
            await db.execute(
                """
                INSERT INTO daily_cost_tracking (date, total_ai_cost, analysis_count, decision_count)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(date) DO UPDATE SET
                    total_ai_cost = total_ai_cost + excluded.total_ai_cost,
                    analysis_count = analysis_count + 1,
                    decision_count = decision_count + excluded.decision_count
            """,
                (today, cost_usd, 1 if decision_action != "SKIP" else 0),
            )

            await db.commit()

    async def was_recently_analyzed(self, market_id: str, hours: int = 6) -> bool:
        """Check if market was analyzed within the specified hours."""
        cutoff_time = datetime.now() - timedelta(hours=hours)
        cutoff_str = cutoff_time.isoformat()

        async with connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM market_analyses 
                WHERE market_id = ? AND analysis_timestamp > ?
            """,
                (market_id, cutoff_str),
            )
            count = (await cursor.fetchone())[0]
            return count > 0

    async def get_daily_ai_cost(self, date: str = None) -> float:
        """Get total AI cost for a specific date (defaults to today)."""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        async with connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT total_ai_cost FROM daily_cost_tracking WHERE date = ?
            """,
                (date,),
            )
            row = await cursor.fetchone()
            return row[0] if row else 0.0

    async def upsert_daily_cost(self, cost: float, date: str = None) -> None:
        """
        Increment the daily AI cost total in the database.

        Called by xAI/OpenRouter clients after every API request so that the
        dashboard and evaluate job always reflect real spending — not just the
        in-memory pickle tracker.

        Args:
            cost: Cost in USD to add to today's total.
            date:  Date string (YYYY-MM-DD). Defaults to today.
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        try:
            async with connect(self.db_path) as db:
                await db.execute(
                    """
                    INSERT INTO daily_cost_tracking (date, total_ai_cost, analysis_count, decision_count)
                    VALUES (?, ?, 1, 0)
                    ON CONFLICT(date) DO UPDATE SET
                        total_ai_cost = total_ai_cost + excluded.total_ai_cost,
                        analysis_count = analysis_count + 1
                """,
                    (date, cost),
                )
                await db.commit()
        except Exception as e:
            self.logger.error(f"Failed to upsert daily cost: {e}")

    async def get_market_analysis_count_today(self, market_id: str) -> int:
        """Get number of times market was analyzed today."""
        today = datetime.now().strftime("%Y-%m-%d")

        async with connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM market_analyses 
                WHERE market_id = ? AND DATE(analysis_timestamp) = ?
            """,
                (market_id, today),
            )
            count = (await cursor.fetchone())[0]
            return count

    async def get_all_trade_logs(self) -> List[TradeLog]:
        """
        Get all trade logs from the database.

        Returns:
            A list of TradeLog objects.
        """
        async with connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM trade_logs")
            rows = await cursor.fetchall()

            logs = []
            for row in rows:
                _dict = dict(row)

                logs.append(_tradelog_from_row(row))
            return logs

    async def update_position_to_live(self, position_id: int, entry_price: float):
        """
        Updates the status and entry price of a position after it has been executed.

        Args:
            position_id: The ID of the position to update.
            entry_price: The actual entry price from the exchange.
        """
        async with connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE positions 
                SET live = 1, entry_price = ?
                WHERE id = ?
            """,
                (entry_price, position_id),
            )
            await db.commit()
        self.logger.info(f"Updated position {position_id} to live.")

    async def update_position_fill(self, position_id: int, entry_price: float, quantity: int):
        """Record the ACTUAL simulated fill for a DRY position.

        The execution simulator prices DRY entries off the live book at
        execution time - slippage, partials and all. The row must carry that
        price and quantity, or closes compute P&L off the decision-time price
        while the ledger debited the real one: the two books diverge by
        exactly the slippage, and the audit reads it as theft.
        """
        async with connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE positions
                SET entry_price = ?, quantity = ?
                WHERE id = ?
            """,
                (float(entry_price), int(quantity), position_id),
            )
            await db.commit()

    async def add_position(
        self, position: Position, allow_duplicate: bool = False
    ) -> Optional[int]:
        """
        Adds a new position to the database, if one doesn't already exist for the same market and side.

        Args:
            position: The position to add.
            allow_duplicate: Permit a second open row for the same market and
                side. This is for strategies that deliberately pyramid (e.g.
                btc_updown buying more of the same contract while the model
                still favours it). Every other caller keeps the guard.

        Returns:
            The ID of the newly inserted position, or None if a position already exists.
        """
        if not allow_duplicate:
            existing_position = await self.get_position_by_market_and_side(
                position.market_id, position.side
            )
            if existing_position:
                self.logger.warning(
                    f"Position already exists for market {position.market_id} and side {position.side}."
                )
                return None

        async with connect(self.db_path) as db:
            position_dict = asdict(position)
            # aiosqlite does not support dataclasses with datetime objects
            position_dict["timestamp"] = position.timestamp.isoformat()
            # Stamp the book at insert time. Falls back to the persisted mode so a
            # caller that forgets is not silently filed under the wrong book.
            if not position_dict.get("mode"):
                position_dict["mode"] = _resolve_current_mode(self.db_path)

            cursor = await db.execute(
                """
                INSERT OR REPLACE INTO positions (market_id, side, entry_price, quantity, timestamp, rationale, confidence, live, status, strategy, stop_loss_price, take_profit_price, max_hold_hours, target_confidence_change, mode, entry_fair)
                VALUES (:market_id, :side, :entry_price, :quantity, :timestamp, :rationale, :confidence, :live, :status, :strategy, :stop_loss_price, :take_profit_price, :max_hold_hours, :target_confidence_change, :mode, :entry_fair)
            """,
                position_dict,
            )
            await db.commit()

            # Set has_position to True for the market
            await db.execute(
                "UPDATE markets SET has_position = 1 WHERE market_id = ?", (position.market_id,)
            )
            await db.commit()

            self.logger.info(
                f"Added position for market {position.market_id}", position_id=cursor.lastrowid
            )
            return cursor.lastrowid

    async def release_position_claim(self, position_id: int) -> bool:
        """Hand a claimed close back, so the position can be retried.

        Without this, any exit that fails after the claim - a refused $0 price,
        a rejected sell order - leaves the row stuck in 'closing'. It would drop
        out of every `status='open'` query while still being a live obligation:
        capital frozen and no exit ever retried.
        """
        async with connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE positions SET status = 'open' WHERE id = ? AND status = 'closing'",
                (int(position_id),),
            )
            await db.commit()
            return cur.rowcount > 0

    async def claim_position_for_close(self, position_id: int) -> bool:
        """Take exclusive ownership of closing a position. True for exactly one caller.

        Several strategy processes each run book-wide position tracking, so two of
        them can read the same `status='open'` row, both decide to exit, both place
        a sell, and both write a trade log. On the live DRY book that produced 17
        closed rows for 11 actual closes - every result counted twice - plus a
        duplicated ledger credit for each, which is how a $300 simulated account
        ended up reporting three different realized figures at once.

        The conditional UPDATE is the arbiter: whoever flips 'open' -> 'closing'
        owns the close, and everyone else is told to leave it alone.
        """
        async with connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE positions SET status = 'closing' WHERE id = ? AND status = 'open'",
                (int(position_id),),
            )
            await db.commit()
            return cur.rowcount > 0

    async def delete_position(self, position_id: int) -> bool:
        """Remove a position row that never became a fill.

        A position is persisted *before* its order is submitted, so that
        `execute_position` has a row to record the fill against. If the order is
        then refused - insufficient funds, an untradable price, a contract that
        rolled - the row has no fill behind it, yet it still reads as `open`:
        it counts toward open-position totals and its cost basis counts as
        deployed capital, with no matching debit in the ledger.

        That is how 23 positions came to exist against 12 ledger entries, and it
        is why the book's own arithmetic stopped reconciling. Unwinding the insert
        keeps the position table a record of fills rather than of attempts.
        """
        async with connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM positions WHERE id = ? AND status = 'open'", (int(position_id),)
            )
            await db.execute(
                "UPDATE markets SET has_position = 0 WHERE market_id ="
                " (SELECT market_id FROM positions WHERE id = ?)",
                (int(position_id),),
            )
            await db.commit()
            return cur.rowcount > 0

    async def get_open_positions(self, mode: Optional[str] = None) -> List[Position]:
        """Get all open positions, optionally scoped to one DRY/LIVE book.

        This used to build Position from positional indexes (`row[10]`,
        `row[11]`, ...) against a `SELECT *`. The positions table has 15
        columns, so every index from 10 on was shifted by one:
        `stop_loss_price` was reading the `strategy` column (TEXT), so exit
        prices came back as strategy names. Named access is used instead, which
        cannot drift when the schema changes.
        """
        query = "SELECT * FROM positions WHERE status = 'open'"
        params: tuple = ()
        if mode is not None:
            query += " AND (mode = ? OR mode IS NULL)"
            params = (mode,)

        async with connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
            return [_position_from_row(r) for r in rows]


if __name__ == "__main__":
    import asyncio
    import os

    async def _init():
        db_path = os.getenv("DB_PATH", "trading_system.db")
        manager = DatabaseManager(db_path=db_path)
        await manager.initialize()
        print(f"✅ Database initialized at {os.path.abspath(db_path)}")
        print(
            "   Tables: markets, positions, trade_logs, market_analyses, daily_cost_tracking, llm_queries, analysis_reports"
        )

    asyncio.run(_init())
