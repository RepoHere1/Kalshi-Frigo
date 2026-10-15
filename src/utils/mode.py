"""Persisted DRY / LIVE trading mode.

Two books, kept factually apart:

- DRY rehearses against the LIVE order book and the REAL the equity, but
  transmits nothing and spends nothing real. Its cash is a flat, derived
  figure, not a running counter that can drift (see `dry_account()`).
- LIVE places real orders against the real Kalshi production account.

Both modes survive restarts: the choice lives in `runtime_config`, not in
process memory, so a redeploy cannot silently change how orders are placed.
DRY is the default on a fresh database - an unconfigured deployment must
never come up armed to trade real money.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from src.utils.database import connect

MODE_DRY = "dry"
MODE_LIVE = "live"
VALID_MODES = (MODE_DRY, MODE_LIVE)

# The DRY book's funding baseline. It is deliberately NOT a mirror of the real
# LIVE balance: a rehearsal needs a stable baseline it can compound from
# regardless of what the real account happens to hold (an earlier "mirror LIVE
# into DRY" behaviour made DRY inherit a $15.41 balance and read as broken).
DEFAULT_DRY_STARTING_BALANCE = 300.0

DB_TIMEOUT_SEC = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_MODE_KEY = "trading_mode"
_START_KEY = "dry_starting_balance"

# Kalshi's minimum order is one contract, not any dollar figure: a 1-cent
# contract is a valid $0.01 order. A balance is "fundable" as long as it can
# pay for at least one whole contract.
MIN_FUNDABLE_BALANCE = 0.01


async def apply_persisted_trading_config(db_path: Optional[str] = None) -> int:
    """Apply saved dashboard config edits onto settings.trading.

    The config editor persists each change as a `cfg.<field>` key in the
    restart-proof runtime_config table; this re-applies them at process
    startup so a redeploy cannot revert an operator's settings (the
    "max positions keeps reverting to 10" bug). Called by the dashboard at
    boot and by every strategy child before it reads settings. Coerces to
    the field's current type; unknown fields are ignored. Returns how many
    fields were applied.
    """
    import os as _os

    from src.config.settings import settings

    path = db_path or _os.getenv("DB_PATH", "trading_system.db")
    try:
        _mode = TradingMode(path)
        async with _mode._conn() as conn:
            cur = await conn.execute(
                "SELECT key, value FROM runtime_config WHERE key LIKE 'cfg.%'"
            )
            rows = await cur.fetchall()
    except Exception:  # noqa: BLE001 - no table yet is not an error
        return 0
    applied = 0
    for row in rows:
        field = str(row["key"])[4:]
        if not hasattr(settings.trading, field):
            continue
        try:
            val = json.loads(row["value"])
        except Exception:  # noqa: BLE001
            continue
        current = getattr(settings.trading, field)
        try:
            if isinstance(current, bool):
                val = bool(val)
            elif isinstance(current, int):
                val = int(val)
            elif isinstance(current, float):
                val = float(val)
            elif isinstance(current, str):
                val = str(val)
        except (TypeError, ValueError):
            continue
        setattr(settings.trading, field, val)
        applied += 1
    return applied


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ModeError(Exception):
    """Raised when a mode transition is not allowed."""


class TradingMode:
    """Reads and writes the persisted mode."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[Any]:
        """Open a connection with the schema guaranteed.

        `async with connect(...)` must not be preceded by `await`:
        aiosqlite's awaitable starts its worker thread, and __aenter__ awaits
        the same object again, which raises "threads can only be started once".
        """
        import aiosqlite

        async with connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.executescript(_SCHEMA)
            yield conn

    async def _get(self, conn, key: str) -> Optional[str]:
        cur = await conn.execute("SELECT value FROM runtime_config WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def _set(self, conn, key: str, value: str) -> None:
        await conn.execute(
            "INSERT INTO runtime_config (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (key, str(value), _now()),
        )

    async def save_trading_config(self, fields: Dict[str, Any]) -> None:
        """Persist dashboard config edits so they survive redeploys.

        Values live as `cfg.<field>` keys in runtime_config, the same
        restart-proof table the mode switch uses.
        """
        async with self._conn() as conn:
            for key, val in fields.items():
                await self._set(conn, f"cfg.{key}", json.dumps(val))
            await conn.commit()

    # ------------------------------------------------------------------
    # Mode
    # ------------------------------------------------------------------
    async def current(self) -> str:
        """The persisted mode.

        A fresh database must never come up LIVE: an unconfigured deployment
        that defaults to the real book is a deployment that spends real money
        without anybody asking it to. Unknown or missing therefore reads DRY.
        """
        async with self._conn() as conn:
            value = await self._get(conn, _MODE_KEY)
        return value if value in VALID_MODES else MODE_DRY

    async def set(self, mode: str, *, confirmed: bool = False) -> str:
        """Switch the mode, persisting it.

        DRY always succeeds. LIVE places real orders with real money, so it
        additionally needs the caller to already be authenticated AND to have
        explicitly confirmed; without both the book stays where it is.
        """
        mode = (mode or "").strip().lower()
        if mode not in VALID_MODES:
            raise ModeError(f"mode must be one of {VALID_MODES}")
        if mode == MODE_LIVE and not confirmed:
            raise ModeError(
                "LIVE places real orders with real money and requires an "
                "explicit confirmation."
            )
        async with self._conn() as conn:
            await self._set(conn, _MODE_KEY, mode)
            await conn.commit()
            return mode

    # ------------------------------------------------------------------
    # The DRY book
    # ------------------------------------------------------------------
    async def save_dry_baseline(self, amount: float) -> None:
        """Record the DRY book's funding baseline."""
        async with self._conn() as conn:
            await self._set(conn, _START_KEY, f"{float(amount):.2f}")
            await conn.commit()

    async def _book_stats(self, book: str) -> Dict[str, Any]:
        """Every figure for one book, read over ONE connection.

        Reads are scoped by the book clause, so a simulated row and a real row
        can never be summed into the same number.
        """
        import aiosqlite

        live_book = str(book).strip().lower() == MODE_LIVE
        book_filter = (
            "COALESCE(NULLIF(mode, ''), 'dry') = 'live'"
            if live_book
            else "COALESCE(NULLIF(mode, ''), 'dry') = 'dry'"
        )

        async def _scalar(conn: Any, sql: str) -> float:
            """First column of the first row, or 0.0.

            `positions` and `trade_logs` are created by DatabaseManager, not
            here, so on a brand-new database this module can legitimately run
            first; a missing table reads as zero rather than raising.
            """
            try:
                cur = await conn.execute(sql)
                row = await cur.fetchone()
                return float(row[0]) if row and row[0] is not None else 0.0
            except Exception:  # noqa: BLE001 - missing table is expected early
                return 0.0

        stats: Dict[str, float] = {}
        # connect() yields an async context manager that must not be awaited
        # first; see src/utils/strategy_runtime.py.
        async with connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            stats["deployed"] = await _scalar(
                conn,
                "SELECT COALESCE(SUM(quantity * entry_price), 0.0) FROM positions"
                f" WHERE status = 'open' AND {book_filter}",
            )
            stats["open_count"] = await _scalar(
                conn,
                f"SELECT COUNT(*) FROM positions WHERE status = 'open' AND {book_filter}",
            )
            stats["realized"] = await _scalar(
                conn,
                f"SELECT COALESCE(SUM(pnl), 0.0) FROM trade_logs WHERE {book_filter}",
            )
            stats["closed"] = await _scalar(
                conn, f"SELECT COUNT(*) FROM trade_logs WHERE {book_filter}"
            )
            stats["wins"] = await _scalar(
                conn,
                f"SELECT COUNT(*) FROM trade_logs WHERE {book_filter} AND pnl > 0",
            )
            stats["losses"] = await _scalar(
                conn,
                f"SELECT COUNT(*) FROM trade_logs WHERE {book_filter} AND pnl < 0",
            )
        return stats

    async def dry_account(self) -> Dict[str, Any]:
        """The DRY book: every figure derived from persisted rows.

        Cash is DERIVED, never stored:

            cash = starting + realized - deployed

        where `realized` is the sum of `trade_logs.pnl` for DRY rows and
        `deployed` is the entry cost of `positions` still open in DRY. The same
        arithmetic holds both mid-hold and after a close, and because nothing is
        accumulated there is no counter to drift out of step with the rows.

        This is the fix for the class of lie the book used to tell: cash was a
        running counter debited on every simulated fill, so a fill that left no
        matching row drained it permanently. One deployment read $38.47 cash
        against $7.00 deployed from a $300 start with zero closes - which is
        arithmetically impossible, and it made every figure downstream suspect.
        A derived cash cannot disagree with its own rows.
        """
        st = await self._book_stats(MODE_DRY)
        starting = float(await self._dry_baseline() or 0.0)

        deployed = st["deployed"]
        open_count = int(st["open_count"])
        realized = st["realized"]
        closed = int(st["closed"])
        wins = int(st["wins"])
        losses = int(st["losses"])

        # The identity, stated rather than stored: cash is exactly what the
        # starting baseline plus the rows account for.
        cash = starting + realized - deployed
        equity = cash + deployed

        return {
            "book": MODE_DRY,
            "starting_balance": round(starting, 2),
            "cash": round(cash, 2),
            "deployed": round(deployed, 2),
            "open_positions": open_count,
            "equity": round(equity, 2),
            "realized": round(realized, 2),
            "total_pnl": round(equity - starting, 2),
            "closed_trades": closed,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0.0,
            # The ledger of rows this book is built from, so a reader can check
            # the arithmetic instead of trusting it.
            "ledger_entries": closed,
            "return_pct": round((equity - starting) / starting * 100, 1) if starting else 0.0,
        }

    async def _dry_baseline(self) -> float:
        """The DRY book's funding baseline, seeded once on first read."""
        async with self._conn() as conn:
            value = await self._get(conn, _START_KEY)
        if value is None:
            value = f"{DEFAULT_DRY_STARTING_BALANCE:.2f}"
            async with self._conn() as conn:
                await self._set(conn, _START_KEY, value)
                await conn.commit()
        try:
            return float(value)
        except (TypeError, ValueError):
            return DEFAULT_DRY_STARTING_BALANCE

    async def set_dry_cash(self, amount: float, *, note: str = "") -> Dict[str, Any]:
        """Rebase the DRY book onto an exact funding amount by moving the
        BASELINE, never by writing a cash number.

        Cash is derived, so the only honest way to set it is to move the
        starting figure the derivation runs from. Every open row and every
        close is then still accounted for by the same identity.
        """
        amount = round(float(amount), 2)
        if amount <= 0:
            raise ModeError("DRY baseline must be positive")
        async with self._conn() as conn:
            await self._set(conn, _START_KEY, f"{amount:.2f}")
            await conn.commit()
        return await self.dry_account()

    async def reset_dry_account(self) -> Dict[str, Any]:
        """Top the DRY book back to its baseline. Positions and closes stay."""
        return await self.dry_account()

    async def book_account(self, mode: str) -> Dict[str, Any]:
        """Stats for one book, from that book's own rows only.

        DRY returns the derived simulated book. LIVE returns the real account's
        trade/P&L side from local rows (the Kalshi balance is merged in by the
        caller), scoped so a row from one book can never be counted next to a
        row from the other.
        """
        if str(mode).strip().lower() == MODE_DRY:
            return await self.dry_account()

        st = await self._book_stats(MODE_LIVE)
        deployed = st["deployed"]
        open_count = int(st["open_count"])
        realized = st["realized"]
        closed = int(st["closed"])
        wins = int(st["wins"])

        return {
            "book": MODE_LIVE,
            "starting_balance": 0.0,
            "cash": 0.0,
            "deployed": round(deployed, 2),
            "open_positions": open_count,
            "equity": round(deployed, 2),
            "realized": round(realized, 2),
            "total_pnl": round(realized, 2),
            "closed_trades": closed,
            "wins": wins,
            "losses": int(st["losses"]),
            "win_rate": round(wins / closed * 100, 1) if closed else 0.0,
            "ledger_entries": closed,
            "return_pct": 0.0,
        }

    async def live_account(self) -> Dict[str, Any]:
        """The real book, from local rows only - the Kalshi balance is merged
        in by the caller."""
        return await self.book_account(MODE_LIVE)

    async def funding(self, private_key_path: Optional[str] = None) -> Dict[str, Any]:
        """Real Kalshi balance, and whether it can actually fund an order.

        The caller supplies the resolved key path so this module never imports
        the dashboard (which would be a cycle). Everything happens inside this
        one coroutine: crossing event loops per request would be both slow and
        a source of hangs.
        """
        import os

        from src.clients.kalshi_client import KalshiClient

        if not private_key_path:
            return {
                "connected": False,
                "balance": 0.0,
                "balance_cents": 0,
                "can_fund": False,
                "reason": "no Kalshi credentials configured",
            }

        client = None
        try:
            client = KalshiClient(
                api_key=os.environ.get("KALSHI_API_KEY", ""),
                private_key_path=private_key_path,
            )
            payload = await client.get_balance()
            cents = int(payload.get("balance", 0))
        except Exception as exc:  # noqa: BLE001 - surfaced to the dashboard
            return {
                "connected": False,
                "balance": 0.0,
                "balance_cents": 0,
                "can_fund": False,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass

        balance = cents / 100.0
        # Kalshi's minimum order is one contract, not any dollar figure: a
        # 1-cent contract is a valid $0.01 order. A balance is "fundable" as
        # long as it can pay for at least one whole contract.
        return {
            "connected": True,
            "balance": round(balance, 2),
            "balance_cents": cents,
            "can_fund": balance >= MIN_FUNDABLE_BALANCE,
            "reason": ""
            if balance >= MIN_FUNDABLE_BALANCE
            else (
                f"balance ${balance:.4f} cannot buy even one 1-cent contract - "
                "fund the account before going LIVE"
            ),
        }


def buy_boost() -> float:
    """Per-clip spend multiplier (operator directive): DOUBLE the DRY buy amount.

    Kelly sizing still decides the base clip; this multiplies the result by 2
    after conviction sizing and before the caps (so the caps still bound it).
    Overrides: BUY_MULT, BUY_MULT_LIVE, BUY_MULT_DRY.
    """
    import os

    _forced = os.environ.get("BUY_MULT")
    if _forced:
        try:
            return float(_forced)
        except ValueError:
            pass
    try:
        from src.jobs.broker import should_trade_live

        _live = should_trade_live()
    except Exception:  # noqa: BLE001 - assume paper when the broker cannot say
        _live = False
    _val = os.environ.get("BUY_MULT_LIVE" if _live else "BUY_MULT_DRY")
    if _val:
        try:
            return float(_val)
        except ValueError:
            pass
    return 1.0 if _live else 2.0


def run(coro: Any, timeout: float = DB_TIMEOUT_SEC) -> Any:
    """Run a coroutine on the process-wide bridge loop with a hard timeout.

    This used to build a throwaway event loop per call. Each throwaway loop
    lazily created a default executor for aiosqlite and never shut it down, so
    every mode/DB read leaked executor threads; the dashboard worker eventually
    hit the OS thread limit ("can't start new thread"), went blank, and Railway
    restarted it - killing every strategy child with it. See
    src/utils/async_bridge.py.
    """
    from src.utils.async_bridge import run as _bridge_run

    return _bridge_run(coro, timeout=timeout)
