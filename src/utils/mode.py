"""Persisted DRY / LIVE trading mode, plus the simulated DRY account.

DRY  - real market data, simulated fills. Orders are never sent to Kalshi; a
       local cash ledger is debited/credited at the real quoted price and every
       simulated trade is written to `trade_logs` so the charts fill in.
LIVE - real orders against the Kalshi production account. Guarded by
       `arm_live()`, which refuses unless the caller has already authenticated
       and explicitly confirmed.

The mode survives restarts: it lives in the `runtime_config` table rather than
in process memory, so a redeploy cannot silently revert LIVE trading to DRY or
the other way around.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from src.utils.database import connect

MODE_DRY = "dry"
MODE_LIVE = "live"
VALID_MODES = (MODE_DRY, MODE_LIVE)

DEFAULT_DRY_STARTING_BALANCE = 300.0

# Every DB call is bounded. A hung read must surface as an error the dashboard
# can render, not as a request that never returns.
DB_TIMEOUT_SEC = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dry_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    amount REAL NOT NULL,
    cash_after REAL NOT NULL,
    note TEXT
);
"""

_MODE_KEY = "trading_mode"
_START_KEY = "dry_starting_balance"
_CASH_KEY = "dry_cash"


class ModeError(Exception):
    """Raised when a mode transition is not allowed."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class TradingMode:
    """Reads and writes the persisted mode and DRY cash ledger."""

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

    async def _exec_optional(self, conn, sql: str, params: Optional[List[Any]] = None) -> bool:
        """Run a statement, tolerating a table that does not exist yet.

        `trade_logs`, `positions` and `dry_ledger` are not all owned by this
        module's `_SCHEMA` - the first two are created by DatabaseManager. On a
        fresh database (a new Railway volume, or any container where the trading
        loop has not run yet) those tables are simply absent, and an unguarded
        DELETE raises `no such table`, which surfaced as a 500 from /api/mode and
        left the DRY/LIVE switch permanently stuck.

        Returns True if the statement ran, False if the table was missing.
        """
        try:
            await conn.execute(sql, tuple(params or ()))
            return True
        except Exception:  # noqa: BLE001 - a missing table is an expected state
            return False

    # ------------------------------------------------------------------
    # Mode
    # ------------------------------------------------------------------
    async def current(self) -> str:
        async with self._conn() as conn:
            value = await self._get(conn, _MODE_KEY)
        # Default to DRY. A fresh database must never come up LIVE.
        return value if value in VALID_MODES else MODE_DRY

    async def set(self, mode: str, *, confirmed: bool = False) -> str:
        mode = (mode or "").strip().lower()
        if mode not in VALID_MODES:
            raise ModeError(f"mode must be one of {VALID_MODES}")

        async with self._conn() as conn:
            previous = await self._get(conn, _MODE_KEY) or MODE_DRY
            if mode == previous:
                return mode

            # Going LIVE places real orders with real money. It cannot happen by
            # a single unconfirmed call, and it cannot happen from a stale session.
            if mode == MODE_LIVE and not confirmed:
                raise ModeError(
                    "Switching to LIVE places real orders with real money and "
                    "requires an explicit confirmation."
                )

            # Isolation between the books is enforced by the `mode` column on every
            # query, by `should_trade_live()` inside each strategy process, and
            # by the broker being chosen per book. It is NOT enforced by
            # deleting the other book.
            #
            # It used to be. Switching to LIVE wiped every DRY position, close
            # and ledger row and reset the cash to $300, on the reasoning that
            # the other book's rows "must not exist when we switch". So every
            # glance at LIVE destroyed the simulated book - which is why a
            # profitable DRY account kept reappearing as $300 with no history,
            # and why it looked like the books were leaking into each other when
            # the books were in fact being destroyed by the switch.
            #
            # Two books that coexist, distinguished by a column, cannot leak:
            # nothing reads across it.
            if mode == MODE_DRY:
                # Ensure a funded DRY book exists without touching an existing
                # one. `ensure_dry_account` creates only when absent.
                await self.ensure_dry_account()

            await self._set(conn, _MODE_KEY, mode)
            await conn.commit()
            return mode

    # ------------------------------------------------------------------
    # DRY account
    # ------------------------------------------------------------------
    @staticmethod
    async def _scalar(conn: Any, sql: str, params: Optional[List[Any]] = None) -> Any:
        """First column of the first row, or None if the table does not exist.

        `positions` and `trade_logs` are created by DatabaseManager, not here,
        so on a brand-new database this module can legitimately run first.
        """
        try:
            cur = await conn.execute(sql, tuple(params or ()))
            row = await cur.fetchone()
            return row[0] if row else None
        except Exception:  # noqa: BLE001 - missing table is an expected state
            return None

    async def reconcile_dry(self) -> Dict[str, Any]:
        """Rebuild DRY cash from the persisted book and report the drift.

        The DRY cash figure used to be a running counter debited on every
        simulated fill. Anything that filled without leaving a matching row - a
        rejected insert, a duplicate market, a position written without going
        through the broker - debited the counter and never credited it back. The
        DRY book then read $38.47 of an original $300 with zero closed trades,
        which is arithmetically impossible and exactly the kind of number that
        makes the whole panel untrustworthy.

        So cash is *derived* from the same rows everything else reads:

            cash = starting + realized - cost basis of open DRY positions

        which makes equity == starting + realized by construction. The ledger is
        still appended to for the fill-by-fill history; it is just no longer
        trusted as the source of truth. The drift is returned so the discrepancy
        stays visible instead of being quietly overwritten.

        An empty book is left alone: with no positions and no closes there is
        nothing to derive from, and a manually set balance must survive.
        """
        async with self._conn() as conn:
            starting = await self._get(conn, _START_KEY)
            starting_f = float(starting) if starting else DEFAULT_DRY_STARTING_BALANCE
            ledger_cash = await self._get(conn, _CASH_KEY)
            ledger_f = float(ledger_cash) if ledger_cash is not None else starting_f

            book_rows = int(
                await self._scalar(
                    conn,
                    "SELECT (SELECT COUNT(*) FROM positions"
                    " WHERE COALESCE(NULLIF(mode, ''), 'dry') = 'dry') +"
                    " (SELECT COUNT(*) FROM trade_logs"
                    " WHERE COALESCE(NULLIF(mode, ''), 'dry') = 'dry')",
                )
                or 0
            )
            if book_rows == 0:
                return {
                    "ledger_cash": round(ledger_f, 2),
                    "derived_cash": round(ledger_f, 2),
                    "drift": 0.0,
                    "note": "no DRY positions or closes yet; the stored balance stands",
                }

            deployed = float(
                await self._scalar(
                    conn,
                    "SELECT COALESCE(SUM(quantity * entry_price), 0.0) FROM positions"
                    " WHERE status = 'open' AND COALESCE(NULLIF(mode, ''), 'dry') = 'dry'",
                )
                or 0.0
            )
            realized = float(
                await self._scalar(
                    conn,
                    "SELECT COALESCE(SUM(pnl), 0.0) FROM trade_logs"
                    " WHERE COALESCE(NULLIF(mode, ''), 'dry') = 'dry'",
                )
                or 0.0
            )
            derived = round(starting_f + realized - deployed, 2)
            # Reported, NOT written.

            # This used to assign the derived figure straight back to the
            # balance, and `reconcile_dry()` runs on every GET /api/mode - so
            # simply loading the page overwrote the cash balance with a number
            # computed from `starting + realized - deployed`. That derivation
            # cannot know about a fill whose close never happened, so every
            # refresh nudged the balance and the book drifted further from its
            # own ledger: it is why a $300 account read -$73.36, and why cash and
            # the ledger could never agree no matter what else was fixed.

            # A GET must not mutate state. The balance is the ledger's truth;
            # this only says how far the two have parted, and
            # `repair_dry_book()` reports the same thing in more detail.
            await conn.commit()

        return {
            "ledger_cash": round(ledger_f, 2),
            "derived_cash": derived,
            "drift": round(derived - ledger_f, 2),
        }

    async def book_account(self, mode: str) -> Dict[str, Any]:
        """Cash, equity and the realized/unrealized split for one book.

        Every query is scoped to the book's own rows. This used to sum *every*
        open position and *every* closed trade regardless of which book they
        belonged to, so the DRY panel reported the real Kalshi account's
        exposure and realized P&L under a DRY heading - the exact confusion the
        mode switch exists to prevent. `live = 0` is the historical marker for a
        simulated row, so both the new `mode` column and the old flag are
        honoured for rows written before the migration.

        The LIVE book has no simulated cash ledger - its starting balance is 0
        and its cash comes from the real Kalshi API (supplied by the caller).
        """
        mode = MODE_LIVE if str(mode).strip().lower() == MODE_LIVE else MODE_DRY
        book_filter = (
            "COALESCE(NULLIF(mode, ''), 'dry') = ?"
            if mode == MODE_DRY
            else "COALESCE(NULLIF(mode, ''), 'dry') = 'live'"
        )
        params: List[Any] = [MODE_DRY] if mode == MODE_DRY else []

        async with self._conn() as conn:
            if mode == MODE_DRY:
                starting = await self._get(conn, _START_KEY)
                cash = await self._get(conn, _CASH_KEY)
                starting_f = float(starting) if starting else DEFAULT_DRY_STARTING_BALANCE
                cash_f = float(cash) if cash is not None else starting_f
            else:
                starting_f = 0.0
                cash_f = 0.0

            deployed = float(
                await self._scalar(
                    conn,
                    "SELECT COALESCE(SUM(quantity * entry_price), 0.0) FROM positions"
                    f" WHERE status = 'open' AND {book_filter}",
                    params,
                )
                or 0.0
            )
            open_count = int(
                await self._scalar(
                    conn,
                    f"SELECT COUNT(*) FROM positions WHERE status = 'open' AND {book_filter}",
                    params,
                )
                or 0
            )
            realized = float(
                await self._scalar(
                    conn,
                    f"SELECT COALESCE(SUM(pnl), 0.0) FROM trade_logs WHERE {book_filter}",
                    params,
                )
                or 0.0
            )
            closed = int(
                await self._scalar(
                    conn, f"SELECT COUNT(*) FROM trade_logs WHERE {book_filter}", params
                )
                or 0
            )
            ledger_rows = (
                int(await self._scalar(conn, "SELECT COUNT(*) FROM dry_ledger") or 0)
                if mode == MODE_DRY
                else 0
            )

        return {
            "book": mode,
            "starting_balance": round(starting_f, 2),
            "cash": round(cash_f, 2),
            "deployed": round(deployed, 2),
            "open_positions": open_count,
            "equity": round(cash_f + deployed, 2),
            "realized": round(realized, 2),
            "total_pnl": round(cash_f - starting_f + realized, 2),
            "closed_trades": closed,
            "ledger_entries": ledger_rows,
            "return_pct": round((cash_f - starting_f + realized) / starting_f * 100, 2)
            if starting_f
            else 0.0,
        }

    async def dry_account(self) -> Dict[str, Any]:
        """Cash, equity and realized/unrealized split for the simulated book."""
        return await self.book_account(MODE_DRY)

    async def live_account(self) -> Dict[str, Any]:
        """The real book, from local rows only - no Kalshi balance here."""
        return await self.book_account(MODE_LIVE)

    async def set_dry_cash(self, amount: float) -> None:
        """Reset the simulated cash balance (top-up / reset to $300)."""
        async with self._conn() as conn:
            await self._set(conn, _CASH_KEY, str(round(float(amount), 2)))
            await conn.commit()

    async def ensure_dry_account(self) -> Dict[str, Any]:
        """Make sure a funded DRY book exists - without destroying an existing one.

        This is what "switch to DRY" needs. `reset_dry_account` is a deliberate
        wipe: it clears the ledger, the positions and the closes, and puts the
        balance back to the starting figure. Calling that on every mode-set meant
        any trip through the DRY switch - or a retried request - silently threw
        away the whole simulated book, which is what kept resetting a profitable
        account back to $300.

        So this only ever *creates*: if there is no ledger and no balance, it
        seeds the starting figure. An existing book is returned untouched.
        """
        async with self._conn() as conn:
            starting = await self._get(conn, _START_KEY)
            starting_f = float(starting) if starting else DEFAULT_DRY_STARTING_BALANCE
            cash = await self._get(conn, _CASH_KEY)
            rows = int(await self._scalar(conn, "SELECT COUNT(*) FROM dry_ledger") or 0)
            if cash is None and rows == 0:
                await self._set(conn, _START_KEY, str(round(starting_f, 2)))
                await self._set(conn, _CASH_KEY, str(round(starting_f, 2)))
                await conn.commit()
        return await self.dry_account()

    async def reset_dry_account(self) -> Dict[str, Any]:
        """Restore the DRY book to a clean starting state.

        This has to mean the *whole* simulated book, not just the cash figure.
        It used to reset cash and empty the ledger while leaving DRY positions
        and DRY trade_logs untouched, so a "reset" produced a book claiming
        $300 cash *and* $250.64 of deployed capital - equity of $550.64 on a
        $300 account, with 23 positions that no fill had ever paid for. A reset
        that leaves the history behind is not a reset.

        Only the DRY book is touched; LIVE rows are never in scope here.
        """
        async with self._conn() as conn:
            starting = await self._get(conn, _START_KEY)
            starting_f = float(starting) if starting else DEFAULT_DRY_STARTING_BALANCE
            await self._set(conn, _CASH_KEY, str(round(starting_f, 2)))
            await conn.execute("DELETE FROM dry_ledger")
            # Positions and closes are the rest of the simulated book.
            await self._exec_optional(
                conn,
                "DELETE FROM positions WHERE COALESCE(NULLIF(mode, ''), 'dry') = 'dry'",
            )
            await self._exec_optional(
                conn,
                "DELETE FROM trade_logs WHERE COALESCE(NULLIF(mode, ''), 'dry') = 'dry'",
            )
            await self._exec_optional(
                conn,
                "DELETE FROM blocked_trades WHERE COALESCE(NULLIF(mode, ''), 'dry') = 'dry'",
            )
            await conn.commit()
        return await self.dry_account()

    async def record_fill(
        self,
        *,
        market_id: str,
        side: str,
        action: str,
        quantity: float,
        price: float,
        note: str = "",
    ) -> Dict[str, Any]:
        """Debit/credit simulated cash for a simulated fill and log it.

        `action` is 'buy' or 'sell'. Buying spends cash, selling returns it.
        """
        quantity = float(quantity)
        price = float(price)
        amount = round(quantity * price, 2)

        async with self._conn() as conn:
            # Take the write lock BEFORE reading the balance. Under WAL two
            # writers can both start a deferred transaction, both read the same
            # cash figure, both compute a new one, and both insert their ledger
            # row - but only the last cash write survives. That is how the DRY
            # book ended up with a ledger whose newest `cash_after` disagreed
            # with the stored balance, and a running total that did not add up.
            # BEGIN IMMEDIATE serialises read-modify-write; nothing else here
            # can, because the ledger row and the balance must move together.
            try:
                await conn.execute("BEGIN IMMEDIATE")
            except Exception:  # noqa: BLE001 - already in a transaction
                pass
            starting = await self._get(conn, _START_KEY)
            cash = await self._get(conn, _CASH_KEY)
            starting_f = float(starting) if starting else DEFAULT_DRY_STARTING_BALANCE
            cash_f = float(cash) if cash is not None else starting_f

            if action == "buy":
                if amount > cash_f:
                    raise ModeError(
                        f"Insufficient simulated funds: ${amount:.2f} needed, "
                        f"${cash_f:.2f} available."
                    )
                cash_f -= amount
            elif action == "sell":
                cash_f += amount
            else:
                raise ModeError("action must be 'buy' or 'sell'")

            await self._set(conn, _CASH_KEY, str(round(cash_f, 2)))
            await conn.execute(
                "INSERT INTO dry_ledger (ts, market_id, side, action, quantity,"
                " price, amount, cash_after, note) VALUES (?,?,?,?,?,?,?,?,?)",
                (_now(), market_id, side, action, quantity, price, amount, round(cash_f, 2), note),
            )
            await conn.commit()
            return {"cash": round(cash_f, 2), "amount": amount}

    async def repair_dry_book(self) -> Dict[str, Any]:
        """Reconcile the DRY book against its own ledger and report the repairs.

        Read-only with respect to history: nothing is deleted or rewritten here.
        This exists because three separate faults could leave the book asserting
        things that cannot all be true - positions with no matching fill, closes
        recorded twice, and a cash balance that no longer follows from either.
        Rather than quietly "fixing" the numbers, it reports exactly which
        invariant is broken and by how much, so the operator decides.

        The three checks:
          1. open positions whose cost basis has no corresponding ledger debit
          2. duplicate closes in trade_logs (same market, exit time and P&L)
          3. cash that cannot be derived from debits minus credits
        """
        import os

        from src.utils.database import DatabaseManager

        report: Dict[str, Any] = {
            "orphan_positions": 0,
            "orphan_notional": 0.0,
            "duplicate_closes": 0,
            "duplicate_pnl": 0.0,
            "ledger_debits": 0.0,
            "ledger_credits": 0.0,
            "cash": 0.0,
            "derived_cash": None,
            "ledger_cash_after": None,
            "ledger_self_consistent": True,
            "ok": True,
        }

        db = DatabaseManager(db_path=os.getenv("DB_PATH", "trading_system.db"))
        await db.initialize()
        book = MODE_DRY

        debits: Dict[str, float] = {}
        credits = 0.0
        try:
            for row in await self.ledger(limit=100000):
                amount = float(row.get("amount") or 0.0)
                if row.get("action") == "buy":
                    debits[str(row.get("market_id"))] = (
                        debits.get(str(row.get("market_id")), 0.0) + amount
                    )
                    report["ledger_debits"] += amount
                else:
                    credits += amount
                    report["ledger_credits"] += amount
        except Exception:  # noqa: BLE001
            pass
        report["cash"] = round(float((await self.dry_account()).get("cash") or 0.0), 2)
        report["derived_cash"] = round(
            float((await self.dry_account()).get("starting_balance") or 0.0)
            - report["ledger_debits"]
            + report["ledger_credits"],
            2,
        )

        # 1. Open positions with no fill behind them.
        try:
            for p in await db.get_open_positions(mode=book):
                if p.strategy and debits.get(p.market_id, 0.0) <= 0.0:
                    report["orphan_positions"] += 1
                    report["orphan_notional"] += (p.entry_price or 0.0) * (p.quantity or 0)
        except Exception:  # noqa: BLE001
            pass

        # 2. Duplicate closes.
        try:
            import aiosqlite

            async with connect(db.db_path) as conn:
                conn.row_factory = aiosqlite.Row
                cur = await conn.execute(
                    "SELECT market_id, exit_timestamp, pnl, COUNT(*) AS n FROM trade_logs"
                    " WHERE COALESCE(NULLIF(mode,''),'dry') = 'dry'"
                    " AND exit_timestamp IS NOT NULL"
                    " GROUP BY market_id, exit_timestamp, pnl HAVING n > 1"
                )
                for row in await cur.fetchall():
                    report["duplicate_closes"] += int(row["n"]) - 1
                    report["duplicate_pnl"] += float(row["pnl"] or 0.0) * (int(row["n"]) - 1)
        except Exception:  # noqa: BLE001
            pass

        report["orphan_notional"] = round(report["orphan_notional"], 2)
        report["duplicate_pnl"] = round(report["duplicate_pnl"], 2)

        # 4. Does the ledger agree with itself?
        #
        # `cash_after` on the newest row is written inside the same transaction
        # as the balance, so if the two disagree, a read-modify-write lost a race
        # between processes. Comparing them is the cheapest way to catch a lost
        # update: the derived total can look plausible while the running balance
        # that produced each row says something else entirely.
        try:
            newest = await self.ledger(limit=1)
            if newest:
                recorded = float(newest[0].get("cash_after") or 0.0)
                report["ledger_cash_after"] = round(recorded, 2)
                report["ledger_self_consistent"] = abs(recorded - report["cash"]) < 0.01
        except Exception:  # noqa: BLE001
            report["ledger_self_consistent"] = True

        report["ok"] = (
            report["orphan_positions"] == 0
            and report["duplicate_closes"] == 0
            and abs(report["cash"] - (report["derived_cash"] or 0.0)) < 0.01
            and report["cash"] >= 0.0
            and bool(report.get("ledger_self_consistent", True))
        )
        return report

    async def ledger(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Most recent simulated fills, newest first."""
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT ts, market_id, side, action, quantity, price, amount,"
                " cash_after, note FROM dry_ledger ORDER BY id DESC LIMIT ?",
                (int(limit),),
            )
            return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------------
    # LIVE funding
    # ------------------------------------------------------------------
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
        # Kalshi's minimum order is $1, so anything below that cannot be traded.
        return {
            "connected": True,
            "balance": round(balance, 2),
            "balance_cents": cents,
            "can_fund": balance >= 1.0,
            "reason": ""
            if balance >= 1.0
            else (
                f"balance ${balance:.2f} is below Kalshi's $1.00 minimum order size - "
                "fund the account before going LIVE"
            ),
        }


def run(coro: Any, timeout: float = DB_TIMEOUT_SEC) -> Any:
    """Run a coroutine on a fresh loop with a hard timeout.

    Flask handlers are synchronous, so each needs its own loop. The timeout
    keeps a wedged DB or socket from holding a worker thread forever.
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
    finally:
        try:
            asyncio.set_event_loop(None)
        finally:
            loop.close()
