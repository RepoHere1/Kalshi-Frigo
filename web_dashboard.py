#!/usr/bin/env python3
"""
Kalshi-Frigo Web Dashboard — enhanced multi-feature dashboard.

Features:
 1. Live position table
 2. Real-time trade feed (SSE)
 3. Strategy control panel (start/stop)
 4. P&L charting (Chart.js)
 5. Alerting (Telegram / Discord webhooks)
 6. Live config editor
 7. Log viewer
 8. Multi-bot management

Railway-ready: listens on $PORT, healthcheck on /health.
"""
import asyncio
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from collections import deque
from typing import Any, Deque, Dict, List, Optional, cast

from flask import Flask, jsonify, render_template_string, request
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
# ProxyFix rebinds the WSGI callable; mypy flags the method assignment, but this
# is the documented way to make Flask trust Railway's proxy headers.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)  # type: ignore[method-assign]
app.config["PREFERRED_URL_SCHEME"] = "https"

# Resolve paths relative to the repo root so the app works regardless of cwd.
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "trading_system.db"))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(BASE_DIR / "logs")))
MAX_ERRORS = 50
MAX_EVENTS = 100

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_STARTED_AT = time.time()

dashboard_state = {
    "status": "starting",
    "has_kalshi_creds": False,
    "has_openrouter_creds": False,
    "balance": None,
    "positions": [],
    "trades": [],
    "strategies": {},
    "last_update": None,
    "errors": [],
    "sse_listeners": [],
    "events": [],
    "db_positions_count": 0,
    "kalshi_position_count": 0,
}

# Strategy control state
strategy_state = {
    "ai_directional": {"running": False, "pid": None, "mode": "paper"},
    "safe_compounder": {"running": False, "pid": None, "mode": "paper"},
    "beast_mode": {"running": False, "pid": None, "mode": "paper"},
    "market_making": {"running": False, "pid": None, "mode": "paper"},
    "quick_flip": {"running": False, "pid": None, "mode": "paper"},
}

# Alert webhooks
alert_state = {
    "telegram_url": os.environ.get("TELEGRAM_WEBHOOK", ""),
    "discord_url": os.environ.get("DISCORD_WEBHOOK", ""),
    "enabled": False,
    "last_sent": None,
}

# Log tail
log_buffer: Deque[str] = deque(maxlen=500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _push_error(message):
    """Record an error, keeping the buffer bounded so long-lived
    containers don't accumulate memory."""
    dashboard_state["errors"].append({"time": _now(), "error": message})
    if len(dashboard_state["errors"]) > MAX_ERRORS:
        del dashboard_state["errors"][: len(dashboard_state["errors"]) - MAX_ERRORS]


def _log_files():
    """Return log files newest-first.

    setup_logging() writes logs/trading_system_<timestamp>.log plus
    logs/latest.log, so there is no single stable 'trading_system.log'.
    """
    if not LOG_DIR.is_dir():
        return []
    try:
        return sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


def _audit(message):
    """Record a privileged action in a bounded audit trail and push it to clients.

    Kept separate from _push_error: switching to LIVE is not a fault, but it is
    the single most consequential thing this process can do, so it needs a
    timestamped record that survives long enough to be noticed.
    """
    entry = {"time": _now(), "action": message}
    dashboard_state.setdefault("events", []).append(entry)
    if len(dashboard_state["events"]) > MAX_EVENTS:
        del dashboard_state["events"][: len(dashboard_state["events"]) - MAX_EVENTS]
    _broadcast("audit", entry)


def _read_log_tail(n):
    """Read the last n lines from the newest log file, or None if there are none."""
    for path in _log_files():
        try:
            with open(path, "r", errors="replace") as f:
                return [line.rstrip() for line in f.readlines()[-n:]]
        except OSError:
            continue
    return None


def _broadcast(event, data):
    """Push an event to all SSE listeners.

    The listeners are plain deques, not queue.Queue objects: they are
    append-only and the consumer pops from them, so a bounded deque avoids
    unbounded growth if a client stops reading.
    """
    payload = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
    dead = []
    for q in list(dashboard_state["sse_listeners"]):
        try:
            q.append(payload)
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            dashboard_state["sse_listeners"].remove(q)
        except ValueError:
            pass


def materialize_private_key():
    """Write a KALSHI_PRIVATE_KEY env var (PEM text) to a file KalshiClient can read.

    KalshiClient loads its key from a *path* (KALSHI_PRIVATE_KEY_PATH, default
    "kalshi_private_key.pem"). Railway can only inject env vars, so a deployment
    that has the PEM as KALSHI_PRIVATE_KEY text would otherwise fail with
    "Private key file not found". Writing it to a 0600 temp file bridges the two.

    Returns the path, or None if no key material is configured.
    """
    pem = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    if not pem:
        return None
    if "BEGIN" not in pem:
        # Not PEM text - assume it is already a path.
        return pem
    key_path = Path(tempfile.gettempdir()) / "kalshi_private_key.pem"
    if not key_path.exists() or key_path.read_text(errors="replace").strip() != pem:
        key_path.write_text(pem if pem.endswith("\n") else pem + "\n")
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass  # best effort; not all filesystems support chmod
    return str(key_path)


def kalshi_configured():
    """True when both the API key and RSA key material are present."""
    if not os.environ.get("KALSHI_API_KEY", "").strip():
        return False
    if materialize_private_key():
        return True
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
    if path and Path(path).exists():
        return True
    return (BASE_DIR / "kalshi_private_key.pem").exists()


async def _fetch_kalshi_data():
    """Fetch balance and positions from the Kalshi API."""
    from src.clients.kalshi_client import KalshiClient

    if not kalshi_configured():
        return None, None
    try:
        key_path = materialize_private_key()
    except OSError as e:
        _push_error(f"Kalshi key material: {e}")
        return None, None

    # KalshiClient's constructor is eager: it loads the PEM and raises if it is
    # missing, so it must never be built when the key is unavailable. There is no
    # initialize() step - signing happens per request in _make_authenticated_request.
    try:
        client = KalshiClient(private_key_path=key_path) if key_path else KalshiClient()
    except Exception as e:
        _push_error(f"Kalshi client init: {e}")
        return None, None

    try:
        balance = await client.get_balance()
        positions = await client.get_positions()
        return balance, positions
    except Exception as e:
        _push_error(f"Kalshi fetch: {e}")
        return None, None
    finally:
        await client.close()


def _run_async(coro):
    """Run a coroutine on a throwaway event loop.

    Flask's request handlers are sync, so each DB call needs its own loop.
    The loop is always closed, otherwise aiosqlite threads accumulate until
    the worker runs out of file descriptors.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        coro.close()
        raise RuntimeError(
            "_run_async() cannot be called from inside a running event loop; "
            "await the coroutine directly instead."
        )
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            asyncio.set_event_loop(None)
        finally:
            loop.close()


def _db():
    """Build an initialized DatabaseManager (schema created if missing)."""
    from src.utils.database import DatabaseManager

    mgr = DatabaseManager(db_path=DB_PATH)
    _run_async(mgr.initialize())
    return mgr


async def _fetch_db_data():
    """Fetch open positions and per-strategy performance from SQLite."""
    from src.utils.database import DatabaseManager

    db = DatabaseManager(db_path=DB_PATH)
    await db.initialize()
    open_positions = await db.get_open_live_positions()
    perf = await db.get_performance_by_strategy()
    return open_positions, perf


# ---------------------------------------------------------------------------
# Read-only SQL helpers
#
# DatabaseManager only exposes per-strategy aggregates that filter on
# `strategy IS NOT NULL`, but the production writer of trade_logs
# (src/jobs/track.py) never sets that column, so every trade it records is
# invisible to get_performance_by_strategy(). These queries aggregate the raw
# tables instead, folding NULL strategies into an explicit "unattributed" bucket
# so the numbers on the page are the real totals rather than a filtered subset.
# ---------------------------------------------------------------------------
def _db_many(queries: List[tuple]) -> List[List[Dict[str, Any]]]:
    """Run several read-only SELECTs over one connection and one event loop.

    build_snapshot() needs a dozen aggregates and the client polls it every few
    seconds. Opening an aiosqlite connection plus a throwaway event loop per
    query turned a page render into ~16 sequential round trips, so they are
    batched here instead. A failing query yields an empty list rather than
    taking the whole snapshot down with it.
    """
    if not queries:
        return []
    import aiosqlite

    async def _run():
        out: List[List[Dict[str, Any]]] = []
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            for sql, params in queries:
                try:
                    cur = await conn.execute(sql, params)
                    out.append([dict(r) for r in await cur.fetchall()])
                except Exception:
                    out.append([])
        return out

    try:
        results: List[List[Dict[str, Any]]] = _run_async(_run())
        return results
    except Exception as e:
        _push_error(f"DB query failed: {e}")
        return [[] for _ in queries]


def _db_rows(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Run a read-only SELECT and return a list of dicts (empty on any error)."""
    rows = _db_many([(sql, params)])
    return rows[0] if rows else []


def _db_one(sql: str, params: tuple = ()) -> Dict[str, Any]:
    rows = _db_rows(sql, params)
    return rows[0] if rows else {}


def _trading_setting(name: str, default: Any = None) -> Any:
    """Read one settings.trading field without importing settings at module load."""
    try:
        from src.config.settings import settings

        return getattr(settings.trading, name, default)
    except Exception:
        return default


# Descriptions of what each runnable strategy actually does. Shown on the page so
# the dashboard documents the system it is monitoring.
STRATEGY_DOCS = {
    "ai_directional": (
        "LLM directional",
        "Ingests Kalshi markets, scores each one with an LLM decision pass, then "
        "sizes a Kelly-criterion position and places paper or live orders.",
    ),
    "safe_compounder": (
        "Safe compounder",
        "No LLM. Pure edge math: only takes trades whose model-implied probability "
        "beats the market price by more than the configured threshold.",
    ),
    "beast_mode": (
        "Beast mode",
        "Aggressive multi-strategy runner (market making + directional + arbitrage) "
        "with risk-parity allocation. Not the default.",
    ),
    "market_making": (
        "Market making",
        "Quotes both sides of the order book and earns the spread, capped by "
        "inventory-risk and correlation limits.",
    ),
    "quick_flip": (
        "Quick flip scalping",
        "Short-horizon strategy that enters on momentum and exits on a small " "favourable move.",
    ),
}


def _refresh_kalshi():
    """One balance/positions sync from the Kalshi API into dashboard_state."""
    balance, positions = _run_async(_fetch_kalshi_data())
    if balance:
        # Kalshi reports cents; the dashboard shows dollars.
        dashboard_state["balance"] = balance.get("balance", 0) / 100.0
    if positions:
        # Kalshi uses different keys for event markets vs market tickers.
        merged = list(positions.get("market_positions") or [])
        merged += list(positions.get("event_positions") or [])
        dashboard_state["positions"] = merged
    dashboard_state["last_update"] = _now()
    dashboard_state["kalshi_position_count"] = len(dashboard_state["positions"])


def _monitor_loop():
    """Background thread: keep credentials, DB schema and Kalshi state fresh.

    This used to run once and return, which left `last_update` and
    `positions` frozen at whatever the first fetch returned (or permanently
    empty when credentials were absent at boot). It now loops for the life of
    the process, so enabling credentials via a redeploy is picked up without a
    restart and live positions stay current.
    """
    interval = max(15, int(os.environ.get("KALSHI_REFRESH_SECONDS", "60")))
    warned_missing = False
    while True:
        dashboard_state["status"] = "online"
        dashboard_state["has_openrouter_creds"] = bool(
            os.environ.get("OPENROUTER_API_KEY", "").strip()
        )
        dashboard_state["has_kalshi_creds"] = kalshi_configured()

        # Make sure the schema exists so the dashboard shows real numbers even
        # on a fresh container where the trading loop has never run.
        try:
            _db()
        except Exception as e:
            _push_error(f"Database init: {e}")

        if not dashboard_state["has_kalshi_creds"]:
            if not warned_missing:
                _push_error("Kalshi credentials not configured — info mode only")
                warned_missing = True
            dashboard_state["last_update"] = _now()
        else:
            try:
                _refresh_kalshi()
            except Exception as e:
                _push_error(f"Kalshi connect: {e}")

        time.sleep(interval)


def _log_tail_loop():
    """Tail the newest log file and buffer lines for the log viewer."""
    seen = set()
    while True:
        files = _log_files()
        if not files:
            time.sleep(2)
            continue
        for path in files:
            if path in seen:
                continue
            seen.add(path)
            try:
                with open(path, "r", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            log_buffer.append(line)
            except OSError:
                continue
        if len(seen) > 8:
            seen = set(files[:1])
        time.sleep(1)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------
def _bots_payload() -> List[Dict[str, Any]]:
    return [
        {
            "name": name,
            "running": st["running"],
            "pid": st["pid"],
            "mode": st.get("mode", "paper"),
            "label": STRATEGY_DOCS.get(name, ("", ""))[0],
            "description": STRATEGY_DOCS.get(name, ("", ""))[1],
        }
        for name, st in strategy_state.items()
    ]


def _config_payload() -> Dict[str, Any]:
    return {f: _trading_setting(f) for f in CONFIG_FIELDS if _trading_setting(f) is not None}


DATA_TABLES = (
    "markets",
    "positions",
    "trade_logs",
    "market_analyses",
    "daily_cost_tracking",
    "llm_queries",
    "analysis_reports",
)

_SQL_TRADES = (
    "SELECT COUNT(*) AS trades,"
    " COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,"
    " COALESCE(SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END), 0) AS losses,"
    " COALESCE(SUM(pnl), 0.0) AS realized_pnl,"
    " COALESCE(AVG(pnl), 0.0) AS avg_pnl,"
    " COALESCE(MAX(pnl), 0.0) AS best_trade,"
    " COALESCE(MIN(pnl), 0.0) AS worst_trade"
    " FROM trade_logs"
)
_SQL_OPEN = (
    "SELECT COUNT(*) AS positions,"
    " COALESCE(SUM(quantity * entry_price), 0.0) AS capital,"
    " COALESCE(SUM(CASE WHEN live = 1 THEN 1 ELSE 0 END), 0) AS live"
    " FROM positions WHERE status = 'open'"
)
_SQL_AI = (
    "SELECT COALESCE(SUM(cost_usd), 0.0) AS cost, COUNT(*) AS analyses"
    " FROM market_analyses WHERE date(analysis_timestamp) = date('now')"
)
_SQL_LLM = (
    "SELECT COUNT(*) AS queries, COALESCE(SUM(tokens_used), 0) AS tokens,"
    " COALESCE(SUM(cost_usd), 0.0) AS cost FROM llm_queries"
)
_SQL_STRATEGY = (
    "SELECT COALESCE(NULLIF(strategy, ''), 'unattributed') AS strategy,"
    " COUNT(*) AS trades,"
    " COALESCE(SUM(pnl), 0.0) AS pnl,"
    " COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,"
    " COALESCE(MAX(pnl), 0.0) AS best,"
    " COALESCE(MIN(pnl), 0.0) AS worst"
    " FROM trade_logs GROUP BY strategy ORDER BY pnl DESC"
)
_SQL_RECENT = (
    "SELECT market_id, side, entry_price, exit_price, quantity, pnl,"
    " exit_timestamp, rationale, strategy FROM trade_logs"
    " ORDER BY exit_timestamp DESC LIMIT 25"
)
_SQL_EQUITY = "SELECT exit_timestamp, pnl FROM trade_logs ORDER BY exit_timestamp DESC LIMIT 200"
_SQL_POSITIONS = (
    "SELECT market_id, side, entry_price, quantity, strategy, stop_loss_price,"
    " take_profit_price, status, timestamp FROM positions"
    " WHERE status = 'open' ORDER BY timestamp DESC"
)


def _row_trades(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    row = rows[0] if rows else {}
    trades = int(row.get("trades") or 0)
    wins = int(row.get("wins") or 0)
    return {
        "trades": trades,
        "wins": wins,
        "losses": int(row.get("losses") or 0),
        "realized_pnl": round(float(row.get("realized_pnl") or 0.0), 2),
        "avg_pnl": round(float(row.get("avg_pnl") or 0.0), 2),
        "best_trade": round(float(row.get("best_trade") or 0.0), 2),
        "worst_trade": round(float(row.get("worst_trade") or 0.0), 2),
        "win_rate": round(wins / trades * 100, 1) if trades else 0.0,
    }


def _row_open(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    row = rows[0] if rows else {}
    positions = int(row.get("positions") or 0)
    live = int(row.get("live") or 0)
    return {
        "positions": positions,
        "live": live,
        "paper": positions - live,
        "capital": round(float(row.get("capital") or 0.0), 2),
    }


def _row_data(
    counts: List[List[Dict[str, Any]]], ai: List[Dict[str, Any]], llm: List[Dict[str, Any]]
) -> Dict[str, Any]:
    ai_row = ai[0] if ai else {}
    llm_row = llm[0] if llm else {}
    return {
        "tables": {t: (c[0].get("n", 0) if c else 0) or 0 for t, c in zip(DATA_TABLES, counts)},
        "ai_cost_today": round(float(ai_row.get("cost") or 0.0), 4),
        "ai_analyses_today": int(ai_row.get("analyses") or 0),
        "ai_budget": _trading_setting("daily_ai_budget", 10.0),
        "llm_queries": int(llm_row.get("queries") or 0),
        "llm_tokens": int(llm_row.get("tokens") or 0),
        "llm_cost": round(float(llm_row.get("cost") or 0.0), 4),
    }


def _row_breakdown(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        trades = int(r.get("trades") or 0)
        wins = int(r.get("wins") or 0)
        out.append(
            {
                "strategy": r.get("strategy") or "unattributed",
                "trades": trades,
                "wins": wins,
                "losses": trades - wins,
                "pnl": round(float(r.get("pnl") or 0.0), 2),
                "best": round(float(r.get("best") or 0.0), 2),
                "worst": round(float(r.get("worst") or 0.0), 2),
                "win_rate": round(wins / trades * 100, 1) if trades else 0.0,
            }
        )
    return out


def _row_positions(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        out.append(
            {
                "market_id": r.get("market_id"),
                "side": r.get("side"),
                "entry_price": r.get("entry_price"),
                "quantity": r.get("quantity"),
                "strategy": r.get("strategy") or "unknown",
                "stop_loss": r.get("stop_loss_price"),
                "take_profit": r.get("take_profit_price"),
                "status": r.get("status"),
                "timestamp": str(r.get("timestamp") or ""),
                "source": "db",
            }
        )
    return out


def _as_float(value: Any, default: float = 0.0) -> float:
    """Kalshi returns money as decimal *strings* (e.g. "-0.012000")."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _kalshi_account() -> Dict[str, Any]:
    """Normalise the raw Kalshi portfolio payload into something displayable.

    The portfolio endpoint returns two different shapes and neither matches what
    the old fallback assumed:
      market_positions -> ticker, position_fp, market_exposure_dollars,
                          total_traded_dollars, realized_pnl_dollars, fees_paid_dollars
      event_positions  -> event_ticker, total_cost_dollars, total_cost_shares_fp,
                          event_exposure_dollars, realized_pnl_dollars, fees_paid_dollars
    There is no `side`, `position`, `last_entry_price` or `status` field on
    either, so those lookups previously rendered "?" and 0 for every row.
    All money fields are decimal strings, not numbers.
    """
    raw: List[Dict[str, Any]] = cast(List[Dict[str, Any]], dashboard_state["positions"])
    markets: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []

    for p in raw:
        if p.get("ticker"):
            markets.append(
                {
                    "ticker": p.get("ticker"),
                    "shares": _as_float(p.get("position_fp")),
                    "exposure": round(_as_float(p.get("market_exposure_dollars")), 2),
                    "traded": round(_as_float(p.get("total_traded_dollars")), 2),
                    "realized": round(_as_float(p.get("realized_pnl_dollars")), 2),
                    "fees": round(_as_float(p.get("fees_paid_dollars")), 2),
                    "updated": str(p.get("last_updated_ts") or ""),
                }
            )
        elif p.get("event_ticker"):
            events.append(
                {
                    "ticker": p.get("event_ticker"),
                    "shares": _as_float(p.get("total_cost_shares_fp")),
                    "cost": round(_as_float(p.get("total_cost_dollars")), 2),
                    "exposure": round(_as_float(p.get("event_exposure_dollars")), 2),
                    "realized": round(_as_float(p.get("realized_pnl_dollars")), 2),
                    "fees": round(_as_float(p.get("fees_paid_dollars")), 2),
                }
            )

    def total(rows: List[Dict[str, Any]], key: str) -> float:
        return round(sum(float(r.get(key) or 0.0) for r in rows), 2)

    return {
        "connected": bool(markets or events),
        "market_count": len(markets),
        "event_count": len(events),
        "markets": sorted(markets, key=lambda r: -abs(r["exposure"]))[:25],
        "events": sorted(events, key=lambda r: -abs(r["cost"]))[:25],
        "exposure": round(total(markets, "exposure") + total(events, "exposure"), 2),
        "cost_basis": total(events, "cost"),
        "realized": round(total(markets, "realized") + total(events, "realized"), 2),
        "fees": round(total(markets, "fees") + total(events, "fees"), 2),
        "traded": total(markets, "traded"),
    }


def build_snapshot() -> Dict[str, Any]:
    """Everything the dashboard shows, in one dict.

    Used both to server-render the page (so the HTML is populated on first byte)
    and by /api/snapshot, so the client-side refresh and the server-rendered
    markup can never disagree. Every SQL statement is issued in one batch so the
    whole snapshot costs a single connection and event loop.
    """
    batch = _db_many(
        [
            (_SQL_TRADES, ()),
            (_SQL_OPEN, ()),
            (_SQL_AI, ()),
            (_SQL_LLM, ()),
            (_SQL_STRATEGY, ()),
            (_SQL_RECENT, ()),
            (_SQL_EQUITY, ()),
            (_SQL_POSITIONS, ()),
        ]
        + [(f"SELECT COUNT(*) AS n FROM {t}", ()) for t in DATA_TABLES]
    )
    trades_r, open_r, ai_r, llm_r, strat_r, recent_r, equity_r, pos_r = batch[:8]
    counts = batch[8:]

    # Equity curve is fetched newest-first; flip it so the chart reads left to right.
    equity_rows = list(reversed(equity_r))
    labels: List[str] = []
    values: List[float] = []
    cumulative = 0.0
    for r in equity_rows:
        cumulative += float(r.get("pnl") or 0.0)
        labels.append(str(r.get("exit_timestamp") or "")[:19])
        values.append(round(cumulative, 2))

    recent = []
    for r in recent_r:
        r["pnl"] = round(float(r.get("pnl") or 0.0), 2)
        r["strategy"] = r.get("strategy") or "unattributed"
        recent.append(r)

    positions = _row_positions(pos_r)
    account = _kalshi_account()
    if not positions and account["connected"]:
        # Kalshi is the source of truth for anything the local DB missed.
        positions = [
            {
                "market_id": r["ticker"],
                "side": "event" if not r["shares"] else ("long" if r["shares"] > 0 else "short"),
                "entry_price": None,
                "quantity": r["shares"],
                "strategy": "kalshi_api",
                "stop_loss": None,
                "take_profit": None,
                "status": "open",
                "timestamp": r.get("updated", ""),
                "source": "kalshi",
            }
            for r in (account["markets"] + account["events"])
        ]
    dashboard_state["db_positions_count"] = len(positions)

    running = _running_strategies()
    errors: List[Dict[str, Any]] = cast(List[Dict[str, Any]], dashboard_state["errors"])
    events: List[Dict[str, Any]] = cast(List[Dict[str, Any]], dashboard_state["events"])

    return {
        "generated_at": _now(),
        "uptime_sec": int(time.time() - _STARTED_AT),
        "status": dashboard_state["status"],
        "last_update": dashboard_state["last_update"],
        "has_kalshi_creds": kalshi_configured(),
        "has_openrouter_creds": bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
        "balance": dashboard_state["balance"],
        "kalshi": account,
        "db_path": str(DB_PATH),
        "db_exists": Path(DB_PATH).exists(),
        "db_persistent": not _is_ephemeral_db(),
        "public_domain": os.environ.get("RAILWAY_PUBLIC_DOMAIN", ""),
        "trades": _row_trades(trades_r),
        "open": _row_open(open_r),
        "data": _row_data(counts, ai_r, llm_r),
        "positions": positions,
        "recent_trades": recent,
        "by_strategy": _row_breakdown(strat_r),
        "equity": {"labels": labels, "pnl": values},
        "bots": _bots_payload(),
        "running_count": len(running),
        "config": _config_payload(),
        "strategy_docs": [
            {
                "name": n,
                "label": STRATEGY_DOCS.get(n, (n, ""))[0],
                "description": STRATEGY_DOCS.get(n, (n, ""))[1],
            }
            for n in strategy_state
        ],
        "alerts": dict(alert_state),
        "mode": _safe_mode_payload(),
        "logs": _read_log_tail(100) or list(log_buffer)[-100:],
        "errors": errors[-10:],
        "events": events[-8:],
    }


def _safe_mode_payload() -> Dict[str, Any]:
    """Mode payload for rendering, degraded rather than fatal.

    A failure here must not take the whole page down: the dashboard is still
    useful for read-only Kalshi data even if the mode store is unavailable.
    """
    try:
        return _mode_payload()
    except Exception as e:
        _push_error(f"Mode payload: {e}")
        return {
            "mode": "dry",
            "token_set": token_required(),
            "dry": {
                "starting_balance": 300.0,
                "cash": 300.0,
                "deployed": 0.0,
                "equity": 300.0,
                "realized": 0.0,
                "total_pnl": 0.0,
                "closed_trades": 0,
                "ledger_entries": 0,
                "return_pct": 0.0,
            },
            "funding": {"connected": False, "balance": 0.0, "can_fund": False, "reason": str(e)},
        }


def _is_ephemeral_db() -> bool:
    """True when the DB lives in the container filesystem and dies on redeploy.

    Railway only persists paths covered by a mounted volume; anything else is
    wiped on every deploy, which is why trades/positions vanish between
    deployments. The dashboard reports this so the cause is visible on the page.
    """
    override = os.environ.get("DB_PATH")
    if override and Path(override).exists():
        return False
    return not bool(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"))


# ---------------------------------------------------------------------------
# Token gate
#
# The dashboard is public. Reading it is harmless, but every state-changing
# route used to be an open POST on the open internet: /api/strategy/<name>/
# toggle would spawn a real trading process for anyone who found the URL, and
# once the Kalshi credentials are present that burns real money and real LLM
# budget. Mutating routes now require a shared token.
# ---------------------------------------------------------------------------
def _configured_token() -> str:
    return os.environ.get("DASHBOARD_TOKEN", "").strip()


def token_required() -> bool:
    """If no token is configured the gate fails closed and locks writes.

    Failing open would defeat the point; failing closed means an operator who
    forgot to set DASHBOARD_TOKEN gets a locked dashboard and a clear error,
    not an unprotected one.
    """
    return bool(_configured_token())


def _presented_token() -> str:
    header = request.headers.get("X-Auth-Token", "")
    if header:
        return header.strip()
    data = request.get_json(silent=True) or {}
    if isinstance(data, dict):
        return str(data.get("token", "")).strip()
    return request.args.get("token", "").strip()


def require_token():
    """Flask guard for mutating routes. Returns None when allowed, else a response."""
    if not token_required():
        return (
            jsonify(
                {
                    "error": "Dashboard is locked: DASHBOARD_TOKEN is not set. "
                    "Set it as a Railway service variable to enable write actions."
                }
            ),
            503,
        )
    if not secrets.compare_digest(_presented_token(), _configured_token()):
        _audit(f"rejected unauthorized {request.method} {request.path} from {request.remote_addr}")
        return jsonify({"error": "Unauthorized: bad or missing X-Auth-Token"}), 401
    return None


# ---------------------------------------------------------------------------
# DRY / LIVE trading mode
# ---------------------------------------------------------------------------
def _mode_manager():
    from src.utils.mode import TradingMode

    return TradingMode(db_path=DB_PATH)


def _mode_payload() -> Dict[str, Any]:
    from src.utils.mode import MODE_DRY, MODE_LIVE, run

    mgr = _mode_manager()
    payload: Dict[str, Any] = {
        "mode": run(mgr.current()),
        "token_set": token_required(),
        "dry": run(mgr.dry_account()),
        "funding": {},
    }
    # Only the live path needs the network, so only probe it in LIVE mode.
    if payload["mode"] == MODE_LIVE:
        payload["funding"] = run(mgr.funding(materialize_private_key()))
    else:
        assert MODE_DRY
        payload["funding"] = {
            "connected": dashboard_state["has_kalshi_creds"],
            "balance": dashboard_state["balance"],
            "can_fund": bool(dashboard_state["balance"]),
            "reason": "",
        }
    assert MODE_LIVE  # keeps the import meaningful for readers
    return payload


@app.route("/api/mode", methods=["GET"])
def api_mode():
    """Current DRY/LIVE mode, the DRY account, and real funding status."""
    try:
        return jsonify(_mode_payload())
    except Exception as e:
        _push_error(f"Mode read: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/mode", methods=["POST"])
def api_mode_set():
    """Switch DRY <-> LIVE.

    Going LIVE places real orders, so it needs the token *and* an explicit
    confirmation *and* a balance that can actually fund an order.
    """
    denied = require_token()
    if denied is not None:
        return denied

    from src.utils.mode import MODE_DRY, MODE_LIVE, ModeError, run

    data = request.get_json(silent=True) or {}
    target = str(data.get("mode", "")).strip().lower()
    confirmed = bool(data.get("confirm"))

    mgr = _mode_manager()
    try:
        if target == MODE_LIVE and not confirmed:
            return (
                jsonify(
                    {
                        "error": "LIVE places real orders with real money. "
                        "Resend with confirm=true to proceed."
                    }
                ),
                400,
            )
        if target == MODE_LIVE:
            funding = run(mgr.funding(materialize_private_key()))
            if not funding.get("connected"):
                return (
                    jsonify(
                        {
                            "error": "Cannot go LIVE: Kalshi API unreachable - "
                            + str(funding.get("reason", ""))
                        }
                    ),
                    400,
                )
            if not funding.get("can_fund"):
                return (
                    jsonify(
                        {
                            "error": "Cannot go LIVE: "
                            + str(funding.get("reason", "insufficient funds"))
                        }
                    ),
                    400,
                )

        mode = run(mgr.set(target, confirmed=confirmed))
    except ModeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        _push_error(f"Mode switch: {e}")
        return jsonify({"error": str(e)}), 500

    _broadcast("mode", {"mode": mode})
    if mode == MODE_DRY:
        # Back to simulated: make sure a DRY book exists and is funded. This must
        # be awaited - calling the coroutine bare leaves the DRY account at $0
        # and emits a RuntimeWarning.
        run(mgr.reset_dry_account())
    _audit(f"trading mode set to {mode.upper()}")
    return jsonify({"ok": True, **_mode_payload()})


@app.route("/api/dry/reset", methods=["POST"])
def api_dry_reset():
    """Top the DRY account back up to its starting balance and clear the ledger."""
    denied = require_token()
    if denied is not None:
        return denied
    from src.utils.mode import run

    mgr = _mode_manager()
    try:
        account = run(mgr.reset_dry_account())
    except Exception as e:
        _push_error(f"DRY reset: {e}")
        return jsonify({"error": str(e)}), 500
    _broadcast("mode", {"mode": run(mgr.current()), "action": "dry_reset"})
    return jsonify({"ok": True, "dry": account})


@app.route("/api/dry/ledger")
def api_dry_ledger():
    """Recent simulated fills."""
    try:
        from src.utils.mode import run

        return jsonify({"ledger": run(_mode_manager().ledger(100))})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Routes — HTML
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    """Server-render the dashboard so the HTML ships populated, not as a shell.

    The client-side poller then refreshes the same numbers in place. Rendering
    server-side matters: view-source (and anything that does not run JS) shows
    the real position, trade and P&L data instead of "--" placeholders.
    """
    try:
        snap = build_snapshot()
    except Exception as e:
        _push_error(f"Snapshot build: {e}")
        snap = {
            "generated_at": _now(),
            "uptime_sec": int(time.time() - _STARTED_AT),
            "status": dashboard_state["status"],
            "errors": [{"time": _now(), "error": f"snapshot failed: {e}"}],
        }
    return render_template_string(_TEMPLATE, s=snap)


@app.route("/api/snapshot")
def api_snapshot():
    """The same payload the page was rendered from, for client-side refreshes."""
    return jsonify(build_snapshot())


@app.route("/health")
def health():
    """Railway healthcheck. Must stay fast and never depend on the network."""
    return jsonify(
        {
            "status": "ok",
            "app": dashboard_state["status"],
            "uptime_sec": int(time.time() - _STARTED_AT),
            "db": str(DB_PATH),
            "db_exists": Path(DB_PATH).exists(),
        }
    )


@app.route("/api/status")
def api_status():
    return jsonify(
        {
            "status": dashboard_state["status"],
            "uptime_sec": int(time.time() - _STARTED_AT),
            "has_kalshi_creds": dashboard_state["has_kalshi_creds"],
            "has_openrouter_creds": dashboard_state["has_openrouter_creds"],
            "balance": dashboard_state["balance"],
            "positions_count": len(dashboard_state["positions"]),
            "db_positions_count": dashboard_state["db_positions_count"],
            "running_strategies": len(_running_strategies()),
            "last_update": dashboard_state["last_update"],
            "db_path": str(DB_PATH),
            "errors": dashboard_state["errors"][-10:],
            "alert_state": alert_state,
        }
    )


# 1. Live position table
@app.route("/api/positions")
def api_positions():
    """Return open positions from the local DB, falling back to Kalshi."""
    try:
        db = _db()
        positions = _run_async(db.get_open_live_positions())
        result = []
        for p in positions:
            result.append(
                {
                    "id": p.id,
                    "market_id": p.market_id,
                    "side": p.side,
                    "entry_price": p.entry_price,
                    "quantity": p.quantity,
                    "strategy": p.strategy or "unknown",
                    "stop_loss": p.stop_loss_price,
                    "take_profit": p.take_profit_price,
                    "status": p.status,
                    "timestamp": str(p.timestamp),
                }
            )
        # Kalshi is the source of truth for anything the local DB missed.
        if not result and dashboard_state["positions"]:
            for p in dashboard_state["positions"]:
                result.append(
                    {
                        "id": None,
                        "market_id": p.get("ticker") or p.get("market_ticker") or "?",
                        "side": p.get("side", "?"),
                        "entry_price": (p.get("last_entry_price") or 0) / 100.0,
                        "quantity": p.get("position", 0),
                        "strategy": "kalshi_api",
                        "stop_loss": None,
                        "take_profit": None,
                        "status": p.get("status", "open"),
                        "timestamp": str(p.get("last_update_time", "")),
                    }
                )
        return jsonify({"positions": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# 2. Real-time trade feed (SSE)
@app.route("/api/stream")
def api_stream():
    """SSE endpoint for real-time events."""
    from flask import Response

    def generate():
        q = deque(maxlen=50)
        dashboard_state["sse_listeners"].append(q)
        try:
            # Unnamed event so the browser's default onmessage handler fires.
            yield f"data: {json.dumps({'event': 'connected', 'msg': 'connected'})}\n\n"
            while True:
                try:
                    msg = q.popleft()
                    yield msg
                except IndexError:
                    time.sleep(0.5)
        finally:
            try:
                dashboard_state["sse_listeners"].remove(q)
            except ValueError:
                pass

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# 3. Strategy control panel
@app.route("/api/strategies", methods=["GET"])
def api_strategies():
    return jsonify(strategy_state)


# Keep strong references to spawned processes so Python's GC never reaps them,
# and always route child output to a file. Pipes would fill after ~64KB and
# deadlock the trading loop, since nothing drains them.
_child_procs: Dict[int, "subprocess.Popen[Any]"] = {}


def _stop_child(st):
    """Stop a running strategy process: SIGTERM, then SIGKILL if it lingers."""
    pid = st.get("pid")
    if not pid:
        return
    proc = _child_procs.get(pid)
    if proc is not None:
        # Preferred path: the Popen object works identically on every platform.
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except (OSError, ValueError):
            pass
        _child_procs.pop(pid, None)
    else:
        # No Popen reference (e.g. state restored after a restart); fall back to
        # signals. signal.SIGKILL does not exist on Windows.
        import signal

        for sig in (getattr(signal, "SIGTERM", 15), getattr(signal, "SIGKILL", 9)):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                break
            for _ in range(10):
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, OSError):
                    break
                time.sleep(0.2)
            else:
                continue
            break
    st["running"] = False
    st["pid"] = None


def _pid_alive(pid):
    """True if a strategy process is still running.

    Popen.poll() is authoritative and portable when we own the handle, so it is
    checked first. os.kill(pid, 0) is only a safe existence probe on POSIX: on
    Windows os.kill() maps to TerminateProcess for any signal other than the
    CTRL_* events, so probing with 0 would kill the very process being checked.
    """
    if not pid:
        return False
    proc = _child_procs.get(pid)
    if proc is not None:
        return proc.poll() is None
    if os.name == "nt":
        return False  # no safe probe without the handle
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # exists, owned by another user
    except (ProcessLookupError, OSError):
        return False
    return True


def _running_strategies():
    """Strategies whose process is actually alive.

    Reconciles the recorded flag against the OS, so a subprocess that crashed
    stops being reported as running instead of lingering in the UI forever.
    """
    names = []
    for name, st in strategy_state.items():
        if not st.get("pid"):
            st["running"] = False
            continue
        if _pid_alive(st["pid"]):
            names.append(name)
        else:
            _push_error(f"Strategy '{name}' exited unexpectedly (pid {st['pid']})")
            st["running"] = False
            st["pid"] = None
            _child_procs.pop(st.get("pid", 0), None)
    return names


@app.route("/api/strategy/<name>/toggle", methods=["POST"])
def api_strategy_toggle(name):
    """Start/stop a strategy subprocess."""
    denied = require_token()
    if denied is not None:
        return denied
    if name not in strategy_state:
        return jsonify({"error": f"Unknown strategy: {name}"}), 404

    st = strategy_state[name]
    mode = (request.json or {}).get("mode", "paper")
    if mode not in ("paper", "live"):
        return jsonify({"error": "mode must be 'paper' or 'live'"}), 400
    if mode == "live" and not os.environ.get("KALSHI_API_KEY"):
        return jsonify({"error": "Live mode needs KALSHI_API_KEY configured"}), 400

    if st["running"]:
        _stop_child(st)
        _broadcast("strategy", {"name": name, "action": "stopped"})
        return jsonify({"name": name, "running": False})

    if mode == "live":
        return (
            jsonify(
                {
                    "error": "Live trading is disabled on this deployment. "
                    "Set KALSHI_PRIVATE_KEY via the Railway dashboard and redeploy to enable."
                }
            ),
            403,
        )

    # Even paper mode boots a KalshiClient, which dies immediately without
    # credentials ("Private key file not found"). Refuse up front so the button
    # does not report success for a process that is about to exit.
    if not os.environ.get("KALSHI_API_KEY") or not (
        os.environ.get("KALSHI_PRIVATE_KEY") or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    ):
        return (
            jsonify(
                {
                    "error": "Kalshi credentials are not configured. Set KALSHI_API_KEY "
                    "and KALSHI_PRIVATE_KEY as Railway service variables, then "
                    "restart the service. The trading loop cannot start without them."
                }
            ),
            400,
        )

    # sys.executable guarantees the child uses the same interpreter (and venv)
    # as the dashboard, rather than whatever 'python' resolves to on PATH.
    py = sys.executable or "python"
    cmd = [py, "cli.py", "run", "--paper"]
    if name == "safe_compounder":
        cmd = [py, "cli.py", "run", "--safe-compounder", "--paper"]
    elif name == "beast_mode":
        cmd = [py, "cli.py", "run", "--beast", "--paper"]
    elif name in ("market_making", "quick_flip"):
        # No dedicated CLI entry point for these strategies yet; fall back to
        # the AI directional loop so the button still does something real.
        cmd = [py, "cli.py", "run", "--paper"]

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        out = open(LOG_DIR / f"strategy_{name}.log", "ab", buffering=0)
        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        out.close()
    except Exception as e:
        _push_error(f"Strategy start ({name}): {e}")
        return jsonify({"error": f"Failed to start: {e}"}), 500

    _child_procs[proc.pid] = proc
    st["running"] = True
    st["pid"] = proc.pid
    st["mode"] = mode
    _broadcast("strategy", {"name": name, "action": "started", "pid": proc.pid})
    return jsonify({"name": name, "running": True, "pid": proc.pid})


# 4. Charting data (P&L)
@app.route("/api/chart/pnl")
def api_chart_pnl():
    """Return P&L data for Chart.js."""
    try:
        db = _db()
        perf = _run_async(db.get_performance_by_strategy())

        labels = []
        pnl_data = []
        cumulative = 0.0
        try:
            import aiosqlite

            async def _read_trades():
                async with aiosqlite.connect(db.db_path) as conn:
                    cur = await conn.execute(
                        "SELECT exit_timestamp, pnl FROM trade_logs "
                        "ORDER BY exit_timestamp ASC LIMIT 200"
                    )
                    return await cur.fetchall()

            for row in _run_async(_read_trades()):
                labels.append(str(row[0] or "")[:19])
                cumulative += row[1] or 0.0
                pnl_data.append(round(cumulative, 2))
        except Exception as e:
            _push_error(f"P&L history: {e}")

        if not labels:
            labels = ["no closed trades yet"]
            pnl_data = [0]

        return jsonify(
            {
                "labels": labels,
                "pnl": pnl_data,
                "by_strategy": {k: (v.get("total_pnl") or 0) for k, v in perf.items()},
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chart/performance")
def api_chart_performance():
    """Strategy performance breakdown."""
    try:
        db = _db()
        return jsonify(_run_async(db.get_performance_by_strategy()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# 5. Alerting
@app.route("/api/alerts", methods=["GET", "POST"])
def api_alerts():
    """Configure alert webhooks."""
    if request.method == "POST":
        denied = require_token()
        if denied is not None:
            return denied
        data = request.json or {}
        alert_state["telegram_url"] = data.get("telegram_url", alert_state["telegram_url"])
        alert_state["discord_url"] = data.get("discord_url", alert_state["discord_url"])
        alert_state["enabled"] = data.get("enabled", alert_state["enabled"])
        _broadcast("alert", {"action": "configured", "enabled": alert_state["enabled"]})
        return jsonify({"ok": True, "alert_state": alert_state})
    return jsonify(alert_state)


def _send_alert(msg):
    """Send alert via configured webhooks."""
    if not alert_state["enabled"]:
        return
    import requests

    payload = {"text": msg}
    if alert_state["telegram_url"]:
        try:
            requests.post(alert_state["telegram_url"], json=payload, timeout=5)
        except Exception:
            pass
    if alert_state["discord_url"]:
        try:
            requests.post(alert_state["discord_url"], json=payload, timeout=5)
        except Exception:
            pass
    alert_state["last_sent"] = _now()


# 6. Config editor
CONFIG_FIELDS = [
    "max_position_size_pct",
    "max_daily_loss_pct",
    "max_positions",
    "min_confidence_to_trade",
    "kelly_fraction",
    "max_single_position",
    "daily_ai_budget",
    "max_ai_cost_per_decision",
    "scan_interval_seconds",
    "market_scan_interval",
    "use_kelly_criterion",
    "live_trading_enabled",
    "paper_trading_mode",
]


def _coerce_like(current, raw):
    """Coerce a form value to the type of the value it replaces.

    The browser sends every field as a string; without this, booleans would
    become non-empty strings and ints would silently become floats.
    """
    if isinstance(current, bool):
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(float(str(raw).strip()))
        except (TypeError, ValueError):
            return current
    if isinstance(current, float):
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            return current
    return raw


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    """Read/write trading config."""
    from src.config.settings import settings

    if request.method == "POST":
        denied = require_token()
        if denied is not None:
            return denied
        data = request.json or {}
        updated, skipped = [], []
        for key, val in data.items():
            if key not in CONFIG_FIELDS or not hasattr(settings.trading, key):
                skipped.append(key)
                continue
            setattr(settings.trading, key, _coerce_like(getattr(settings.trading, key), val))
            updated.append(key)
        if skipped:
            _push_error(f"Ignored unknown config keys: {', '.join(skipped)}")
        _broadcast("config", {"action": "updated", "keys": updated})
        return jsonify({"ok": True, "updated": updated, "skipped": skipped})

    readable = {}
    for field in CONFIG_FIELDS:
        if hasattr(settings.trading, field):
            readable[field] = getattr(settings.trading, field)
    return jsonify(readable)


# 7. Log viewer
@app.route("/api/logs")
def api_logs():
    """Return recent log lines."""
    try:
        n = max(1, min(2000, int(request.args.get("lines", "100"))))
    except ValueError:
        n = 100
    lines = _read_log_tail(n)
    if lines is not None:
        return jsonify({"lines": lines, "source": "file"})
    return jsonify({"lines": list(log_buffer)[-n:], "source": "buffer"})


# 8. Multi-bot management
@app.route("/api/bots", methods=["GET"])
def api_bots():
    """List tracked bot processes, reconciling stale state against the OS."""
    # Mutates strategy_state as a side effect, so /api/status and /api/bots
    # can never disagree about whether a strategy is running.
    _running_strategies()
    return jsonify(
        [
            {
                "name": name,
                "running": st["running"],
                "pid": st["pid"],
                "mode": st.get("mode", "paper"),
            }
            for name, st in strategy_state.items()
        ]
    )


@app.route("/api/bot/<name>/kill", methods=["POST"])
def api_bot_kill(name):
    """Force-stop a bot process."""
    denied = require_token()
    if denied is not None:
        return denied
    if name not in strategy_state:
        return jsonify({"error": "unknown"}), 404
    st = strategy_state[name]
    if not st.get("pid"):
        return jsonify({"name": name, "killed": False, "reason": "not running"})
    _stop_child(st)
    return jsonify({"name": name, "killed": True})


# ---------------------------------------------------------------------------
# HTML Template (all features)
# ---------------------------------------------------------------------------
_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalshi-Frigo — Live Trading Dashboard</title>
<style>
:root{
  --bg:#080b12; --panel:#0f1420; --panel2:#141b2a; --line:#1f2937; --line2:#2b3648;
  --fg:#e6edf6; --dim:#8494ab; --faint:#5b6a80;
  --up:#2ee6a8; --down:#ff5c7a; --blue:#4d9fff; --amber:#ffb454; --violet:#a78bfa;
  --r:14px; --shadow:0 1px 0 rgba(255,255,255,.03) inset, 0 8px 30px rgba(0,0,0,.45);
}
*{box-sizing:border-box;margin:0;padding:0}
html{scrollbar-color:#2b3648 transparent}
body{
  font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Inter,Roboto,sans-serif;
  background:
    radial-gradient(1100px 520px at 12% -10%, #16233c 0%, transparent 60%),
    radial-gradient(900px 480px at 92% 0%, #1a1533 0%, transparent 55%),
    var(--bg);
  color:var(--fg); min-height:100vh; padding:22px 18px 48px;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1400px;margin:0 auto}

/* ---------- header ---------- */
header{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end;justify-content:space-between;margin-bottom:22px}
.brand{display:flex;align-items:center;gap:12px}
.logo{
  width:42px;height:42px;border-radius:12px;display:grid;place-items:center;font-size:20px;
  background:linear-gradient(145deg,#2b6cff,#8b5cf6);box-shadow:0 6px 20px rgba(43,108,255,.35);
}
h1{font-size:20px;font-weight:650;letter-spacing:-.2px}
h1 span{color:var(--dim);font-weight:400}
.sub{color:var(--faint);font-size:12px;margin-top:2px}
.headright{display:flex;flex-direction:column;align-items:flex-end;gap:6px}

/* DRY / LIVE switch - the highest-stakes control on the page, so it is the
   most prominent thing in the header and colour-coded rather than subtle. */
.modeswitch{display:flex;gap:0;border-radius:10px;overflow:hidden;border:1px solid var(--line2)}
.modebtn{
  background:var(--panel);color:var(--faint);border:0;border-radius:0;
  padding:7px 18px;font-size:12px;font-weight:700;letter-spacing:.09em;cursor:pointer;
}
.modebtn:hover{background:rgba(255,255,255,.07)}
.modebtn.on{background:rgba(46,230,168,.16);color:var(--up);box-shadow:inset 0 -2px 0 var(--up)}
.modebtn.live.on{background:rgba(255,92,122,.18);color:var(--down);box-shadow:inset 0 -2px 0 var(--down)}
.modebtn:disabled{opacity:.5;cursor:not-allowed}
.note-box.live{background:rgba(255,92,122,.08);border-color:rgba(255,92,122,.28)}
.note-box.live b{color:var(--down)}
.url{
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:var(--blue);
  background:rgba(77,159,255,.09);border:1px solid rgba(77,159,255,.25);
  padding:3px 9px;border-radius:7px;cursor:pointer;transition:.15s;
}
.url:hover{background:rgba(77,159,255,.18)}
.stamp{font-size:11px;color:var(--faint)}

/* ---------- tiles ---------- */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:10px;margin-bottom:18px}
.tile{
  background:linear-gradient(180deg,var(--panel) 0%,var(--panel2) 100%);
  border:1px solid var(--line);border-radius:var(--r);padding:14px 15px;box-shadow:var(--shadow);
  position:relative;overflow:hidden;
}
.tile::after{content:"";position:absolute;inset:0 0 auto 0;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.09),transparent)}
.tile .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--faint);margin-bottom:7px}
.tile .v{font-size:25px;font-weight:660;letter-spacing:-.6px;font-variant-numeric:tabular-nums;line-height:1.05}
.tile .s{font-size:11px;color:var(--dim);margin-top:4px}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--fg)}

/* ---------- layout ---------- */
.row{display:grid;gap:12px;margin-bottom:12px}
.row.two{grid-template-columns:1.15fr .85fr}
.row.half{grid-template-columns:1fr 1fr}
@media(max-width:1000px){.row.two,.row.half{grid-template-columns:1fr}}
.panel{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
  box-shadow:var(--shadow);overflow:hidden;display:flex;flex-direction:column;
}
.ph{
  display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:13px 16px;border-bottom:1px solid var(--line);background:rgba(255,255,255,.012);
}
.ph h2{font-size:12.5px;font-weight:620;letter-spacing:.04em;text-transform:uppercase;color:var(--fg)}
.ph .note{font-size:11px;color:var(--faint);font-weight:400;text-transform:none;letter-spacing:0}
.pb{padding:14px 16px;flex:1}

/* ---------- bits ---------- */
.pill{display:inline-flex;align-items:center;gap:5px;padding:2.5px 9px;border-radius:999px;font-size:10.5px;font-weight:620;letter-spacing:.02em}
.pill.ok{background:rgba(46,230,168,.13);color:var(--up)}
.pill.no{background:rgba(255,92,122,.13);color:var(--down)}
.pill.warn{background:rgba(255,180,84,.14);color:var(--amber)}
.pill.info{background:rgba(77,159,255,.13);color:var(--blue)}
.dot{width:6px;height:6px;border-radius:50%;background:currentColor;box-shadow:0 0 0 3px rgba(255,255,255,.05)}
.pulse{animation:pl 2s ease-in-out infinite}
@keyframes pl{0%,100%{opacity:1}50%{opacity:.35}}

.kv{display:grid;grid-template-columns:1fr auto;gap:7px 14px;font-size:12.5px;align-items:center}
.kv dt{color:var(--dim)}
.kv dd{text-align:right;font-variant-numeric:tabular-nums}

table{width:100%;border-collapse:collapse;font-size:12.5px}
th{
  text-align:left;padding:8px 10px;color:var(--faint);font-size:10px;font-weight:640;
  text-transform:uppercase;letter-spacing:.07em;border-bottom:1px solid var(--line);white-space:nowrap;
}
td{padding:8px 10px;border-bottom:1px solid rgba(31,41,55,.55);vertical-align:middle}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:rgba(255,255,255,.022)}
.num{text-align:right;font-variant-numeric:tabular-nums}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px}
.tag{
  display:inline-block;background:rgba(77,159,255,.12);color:var(--blue);
  padding:1.5px 7px;border-radius:6px;font-size:10px;font-weight:600;white-space:nowrap;
}
.empty{padding:26px 14px;text-align:center;color:var(--faint);font-size:12.5px}

.scroll{max-height:290px;overflow:auto}
.scroll::-webkit-scrollbar{width:8px;height:8px}
.scroll::-webkit-scrollbar-thumb{background:var(--line2);border-radius:8px}

canvas{max-height:250px}

/* ---------- controls ---------- */
button{
  background:rgba(77,159,255,.14);color:var(--blue);border:1px solid rgba(77,159,255,.28);
  padding:5px 12px;border-radius:8px;cursor:pointer;font-size:11.5px;font-weight:600;
  font-family:inherit;transition:.14s;white-space:nowrap;
}
button:hover{background:rgba(77,159,255,.26)}
button.danger{background:rgba(255,92,122,.12);color:var(--down);border-color:rgba(255,92,122,.28)}
button.danger:hover{background:rgba(255,92,122,.24)}
button:disabled{opacity:.45;cursor:not-allowed}
input[type=text],input[type=number]{
  background:var(--bg);color:var(--fg);border:1px solid var(--line2);border-radius:8px;
  padding:6px 10px;font-size:12px;width:100%;font-family:inherit;
}
input:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px rgba(77,159,255,.13)}
input[type=checkbox]{width:auto;margin:0;accent-color:var(--blue);vertical-align:middle;cursor:pointer}
.field{margin-bottom:7px}
.field label{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--dim);margin-bottom:4px}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}

pre{
  background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:11px;
  font-size:11px;line-height:1.55;max-height:240px;overflow:auto;white-space:pre-wrap;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--dim);margin:0;
}
.steps{margin:0;padding-left:18px;font-size:12.5px;line-height:1.85;color:var(--dim)}
.steps li{margin-bottom:3px}
.steps b{color:var(--fg);font-weight:620}
.note-box{
  background:rgba(255,180,84,.07);border:1px solid rgba(255,180,84,.22);
  border-radius:10px;padding:10px 12px;font-size:11.5px;color:var(--dim);line-height:1.6;
}
.note-box b{color:var(--amber)}
code{background:var(--bg);border:1px solid var(--line2);border-radius:5px;padding:1px 5px;font-size:11px;font-family:ui-monospace,monospace}
footer{margin-top:22px;text-align:center;color:var(--faint);font-size:11px}
</style>
</head>
<body>
<div class="wrap">

<header>
  <div class="brand">
    <div class="logo">&#129504;</div>
    <div>
      <h1>Kalshi-Frigo <span>&middot; live trading dashboard</span></h1>
      <div class="sub">LLM-driven Kalshi automation &middot; paper &amp; live &middot; multi-strategy</div>
    </div>
  </div>
  <div class="headright">
    <!-- DRY / LIVE switch: the one control that decides whether orders are real -->
    <div class="modeswitch">
      <button id="modeDry"  class="modebtn {{ 'on' if s.mode.mode != 'live' else '' }}" onclick="setMode('dry')">DRY</button>
      <button id="modeLive" class="modebtn live {{ 'on' if s.mode.mode == 'live' else '' }}" onclick="setMode('live')">LIVE</button>
    </div>
    <div class="url" onclick="navigator.clipboard.writeText(location.href)" title="Click to copy this URL">{{ s.public_domain or 'localhost' }}</div>
    <div class="stamp">Rendered {{ s.generated_at }}</div>
  </div>
</header>

{% if s.mode.mode == 'live' %}
<div class="note-box live" style="margin-bottom:14px">
  <b>LIVE MODE &mdash; real orders, real money.</b>
  Every fill below is a real Kalshi trade against the production account
  (funding source: <b>{{ '$%.2f'|format(s.mode.funding.get('balance', 0)) if s.mode.funding.get('balance') is not none else 'unavailable' }}</b>).
  Realized P&amp;L of <b>{{ '$%.2f'|format(s.mode.funding.get('balance', 0) or 0) }}</b> is the account balance, not a simulation.
</div>
{% endif %}

{% if not s.mode.token_set %}
<div class="note-box" style="margin-bottom:14px">
  <b>Write actions are locked.</b>
  <code>DASHBOARD_TOKEN</code> is not set, so every mutating endpoint (mode switch,
  strategy start/stop, config, alerts) returns <code>503</code>. This is deliberate:
  the dashboard is public, and with Kalshi credentials present an open
  <code>/api/strategy/&lt;name&gt;/toggle</code> would let anyone spawn a trading process.
</div>
{% endif %}

<!-- DRY account: the only simulated thing in the system -->
<div class="row two">
  <div class="panel">
    <div class="ph">
      <h2>{{ 'DRY account' if s.mode.mode != 'live' else 'DRY account (paused while LIVE)' }}</h2>
      <span class="note">
        <span id="tDryCash">{{ '$%.2f'|format(s.mode.dry.cash) }}</span> cash
      </span>
    </div>
    <div class="pb">
      <dl class="kv">
        <dt>Starting balance</dt><dd>{{ '$%.2f'|format(s.mode.dry.starting_balance) }}</dd>
        <dt>Cash</dt><dd id="dCash">{{ '$%.2f'|format(s.mode.dry.cash) }}</dd>
        <dt>Deployed in open positions</dt><dd id="dDeployed">{{ '$%.2f'|format(s.mode.dry.deployed) }}</dd>
        <dt>Equity</dt><dd id="dEquity">{{ '$%.2f'|format(s.mode.dry.equity) }}</dd>
        <dt>Realized</dt><dd id="dRealized">{{ '$%.2f'|format(s.mode.dry.realized) }}</dd>
        <dt>Total P&amp;L vs start</dt>
        <dd id="dPnl" class="{{ 'up' if s.mode.dry.total_pnl > 0 else ('down' if s.mode.dry.total_pnl < 0 else 'flat') }}">{{ '$%.2f'|format(s.mode.dry.total_pnl) }}</dd>
        <dt>Return</dt><dd id="dRet">{{ s.mode.dry.return_pct }}%</dd>
        <dt>Simulated fills</dt><dd id="dLedger">{{ s.mode.dry.ledger_entries }}</dd>
      </dl>
      <div class="bar" style="margin-top:12px">
        <button onclick="resetDry()">Reset DRY account to {{ '$%.0f'|format(s.mode.dry.starting_balance) }}</button>
        <span id="dryStatus" style="font-size:11px;color:var(--faint)"></span>
      </div>
      <p class="note" style="margin-top:10px;color:var(--faint);font-size:11px">
        Prices, markets and P&amp;L inputs are the real production API. Only the
        order fill and the cash ledger are simulated, so the DRY book exercises
        the same code path as LIVE without spending money.
      </p>
    </div>
  </div>

  <div class="panel">
    <div class="ph"><h2>Funding source</h2><span class="note">live Kalshi account</span></div>
    <div class="pb">
      <dl class="kv">
        <dt>API connection</dt>
        <dd>{% if s.mode.funding.get('connected') %}<span class="pill ok">connected</span>{% else %}<span class="pill no">offline</span>{% endif %}</dd>
        <dt>Available balance</dt><dd id="fBalance">{{ '$%.2f'|format(s.mode.funding.get('balance', 0) or 0) }}</dd>
        <dt>Can fund an order</dt>
        <dd>{% if s.mode.funding.get('can_fund') %}<span class="pill ok">yes</span>{% else %}<span class="pill no">no</span>{% endif %}</dd>
      </dl>
      {% if s.mode.funding.get('reason') %}
      <div class="note-box" style="margin-top:12px"><b>Cannot go LIVE:</b> {{ s.mode.funding.get('reason') }}</div>
      {% endif %}
      <p class="note" style="margin-top:10px;color:var(--faint);font-size:11px">
        Kalshi rejects orders below $1.00. Until the production account is funded,
        the LIVE switch stays locked by design &mdash; this is the guard that stops a
        mis-click from becoming a failed or partial order.
      </p>
    </div>
  </div>
</div>

{% if not s.public_domain %}
<div class="note-box" style="margin-bottom:14px">
  <b>Heads up:</b> you are viewing this over a hostname other than the Railway public domain.
  The canonical address is <code>kalshi-frigo-production.up.railway.app</code> &mdash;
  <code>kalshi-frigo.up.railway.app</code> does not exist, because Railway always names
  service domains <code>&lt;service&gt;-&lt;environment&gt;.up.railway.app</code>.
</div>
{% endif %}

<!-- ============ headline tiles ============ -->
<div class="tiles">
  <div class="tile">
    <div class="k">Engine</div>
    <div class="v">{% if s.status == 'online' %}<span class="pill ok"><span class="dot pulse"></span>ONLINE</span>{% else %}<span class="pill no">OFF</span>{% endif %}</div>
    <div class="s" id="tUptime">up {{ (s.uptime_sec // 60) if s.uptime_sec is defined else 0 }}m</div>
  </div>
  <div class="tile">
    <div class="k">Kalshi balance</div>
    <div class="v" id="tBalance">{{ '$%.2f'|format(s.balance) if s.balance is not none else '-' }}</div>
    <div class="s">{% if s.has_kalshi_creds %}API connected{% else %}no credentials{% endif %}</div>
  </div>
  <div class="tile">
    <div class="k">Live positions</div>
    <div class="v" id="tLivePos">{{ (s.kalshi.market_count + s.kalshi.event_count) if s.kalshi else 0 }}</div>
    <div class="s">{{ s.kalshi.market_count if s.kalshi else 0 }} market &middot; {{ s.kalshi.event_count if s.kalshi else 0 }} event</div>
  </div>
  <div class="tile">
    <div class="k">Exposure</div>
    <div class="v" id="tExposure">{{ '$%.2f'|format(s.kalshi.exposure) if s.kalshi else '$0.00' }}</div>
    <div class="s">cost basis {{ '$%.2f'|format(s.kalshi.cost_basis) if s.kalshi else '$0.00' }}</div>
  </div>
  <div class="tile">
    <div class="k">Kalshi realized P&amp;L</div>
    <div class="v {{ 'up' if s.kalshi and s.kalshi.realized > 0 else ('down' if s.kalshi and s.kalshi.realized < 0 else 'flat') }}" id="tKalshiPnl">{{ '$%.2f'|format(s.kalshi.realized) if s.kalshi else '$0.00' }}</div>
    <div class="s">fees {{ '$%.2f'|format(s.kalshi.fees) if s.kalshi else '$0.00' }}</div>
  </div>
  <div class="tile">
    <div class="k">Bot realized P&amp;L</div>
    <div class="v {{ 'up' if s.trades and s.trades.realized_pnl > 0 else ('down' if s.trades and s.trades.realized_pnl < 0 else 'flat') }}" id="tBotPnl">{{ '$%.2f'|format(s.trades.realized_pnl) if s.trades else '$0.00' }}</div>
    <div class="s">{{ s.trades.trades if s.trades else 0 }} closed trades</div>
  </div>
  <div class="tile">
    <div class="k">Win rate</div>
    <div class="v" id="tWinRate">{{ s.trades.win_rate if s.trades else 0 }}%</div>
    <div class="s">{{ s.trades.wins if s.trades else 0 }}W / {{ s.trades.losses if s.trades else 0 }}L</div>
  </div>
  <div class="tile">
    <div class="k">AI spend today</div>
    <div class="v" id="tAiSpend">{{ '$%.2f'|format(s.data.ai_cost_today) if s.data else '$0.00' }}</div>
    <div class="s">of {{ '$%.2f'|format(s.data.ai_budget) if s.data else '$0.00' }} budget</div>
  </div>
  <div class="tile">
    <div class="k">Strategies live</div>
    <div class="v" id="tRunning">{{ s.running_count if s.running_count is defined else 0 }}</div>
    <div class="s">of {{ s.bots|length }} available</div>
  </div>
</div>

<!-- ============ readiness + inventory ============ -->
<div class="row two">
  <div class="panel">
    <div class="ph"><h2>System readiness</h2><span class="note">what is and isn't wired up</span></div>
    <div class="pb">
      <dl class="kv">
        <dt>Kalshi API credentials</dt>
        <dd>{% if s.has_kalshi_creds %}<span class="pill ok">configured</span>{% else %}<span class="pill no">missing</span>{% endif %}</dd>
        <dt>OpenRouter API key</dt>
        <dd>{% if s.has_openrouter_creds %}<span class="pill ok">configured</span>{% else %}<span class="pill no">missing</span>{% endif %}</dd>
        <dt>Database</dt>
        <dd>{% if s.db_exists %}<span class="pill ok">present</span>{% else %}<span class="pill warn">missing</span>{% endif %}</dd>
        <dt>Database persistence</dt>
        <dd>{% if s.db_persistent %}<span class="pill ok">volume</span>{% else %}<span class="pill warn">ephemeral</span>{% endif %}</dd>
        <dt>Last Kalshi sync</dt><dd>{{ s.last_update or 'never' }}</dd>
        <dt>Database path</dt><dd class="mono" style="color:var(--faint)">{{ s.db_path }}</dd>
      </dl>
      {% if not s.db_persistent %}
      <div class="note-box" style="margin-top:12px">
        <b>Bot trade history is wiped on every deploy.</b>
        <code>trading_system.db</code> lives in the container filesystem rather than a mounted
        volume, so each redeploy recreates it empty. Mount a Railway volume and point
        <code>DB_PATH</code> inside it to make bot positions and trade logs persist.
        The Kalshi figures above are unaffected &mdash; they come straight from the API.
      </div>
      {% endif %}
    </div>
  </div>

  <div class="panel">
    <div class="ph"><h2>Data inventory</h2><span class="note">row counts per table</span></div>
    <div class="pb">
      <dl class="kv">
        {%- for name, count in (s.data.tables if s.data else {}).items() %}
        <dt class="mono">{{ name }}</dt><dd>{{ count }}</dd>
        {%- endfor %}
        <dt>LLM queries logged</dt><dd>{{ s.data.llm_queries if s.data else 0 }}</dd>
        <dt>LLM tokens used</dt><dd>{{ s.data.llm_tokens if s.data else 0 }}</dd>
        <dt>LLM spend (all time)</dt><dd>${{ s.data.llm_cost if s.data else 0 }}</dd>
      </dl>
    </div>
  </div>
</div>

{% if s.errors %}
<div class="panel" style="margin-bottom:12px">
  <div class="ph"><h2>Recent errors</h2><span class="note">{{ s.errors|length }} most recent</span></div>
  <div class="pb"><div class="bar">
    {%- for e in s.errors[-6:]|reverse %}<span class="pill warn"><span class="mono">{{ e.time }}</span> {{ e.error }}</span>{% endfor %}
  </div></div>
</div>
{% endif %}

<!-- ============ Kalshi account ============ -->
<div class="panel" style="margin-bottom:12px">
  <div class="ph">
    <h2>Kalshi account &mdash; live from the API</h2>
    <span class="note">{% if s.last_update %}synced {{ s.last_update }}{% else %}not synced yet{% endif %}</span>
  </div>
  {%- if s.kalshi and s.kalshi.connected %}
  <div class="row two" style="padding:14px 16px 0;margin:0">
    <div class="pb" style="padding:0">
      <table><thead><tr><th>Market</th><th class="num">Shares</th><th class="num">Exposure</th><th class="num">Traded</th><th class="num">Realized</th><th class="num">Fees</th></tr></thead><tbody>
      <tbody id="kalshiMarkets">
      {%- for r in s.kalshi.markets %}
        <tr>
          <td class="mono">{{ r.ticker }}</td>
          <td class="num">{{ r.shares }}</td>
          <td class="num">{{ r.exposure }}</td>
          <td class="num">{{ r.traded }}</td>
          <td class="num {{ 'up' if r.realized > 0 else ('down' if r.realized < 0 else 'flat') }}">{{ r.realized }}</td>
          <td class="num" style="color:var(--faint)">{{ r.fees }}</td>
        </tr>
      {%- endfor %}
      </tbody></table>
    </div>
    <div class="pb" style="padding:0">
      <table><thead><tr><th>Event</th><th class="num">Shares</th><th class="num">Cost</th><th class="num">Exposure</th><th class="num">Realized</th><th class="num">Fees</th></tr></thead><tbody>
      <tbody id="kalshiEvents">
      {%- for r in s.kalshi.events %}
        <tr>
          <td class="mono">{{ r.ticker }}</td>
          <td class="num">{{ r.shares }}</td>
          <td class="num">{{ r.cost }}</td>
          <td class="num">{{ r.exposure }}</td>
          <td class="num {{ 'up' if r.realized > 0 else ('down' if r.realized < 0 else 'flat') }}">{{ r.realized }}</td>
          <td class="num" style="color:var(--faint)">{{ r.fees }}</td>
        </tr>
      {%- endfor %}
      </tbody></table>
    </div>
  </div>
  {%- else %}
  <div class="empty">
    {% if s.has_kalshi_creds %}
      Waiting for the first Kalshi sync &mdash; the poller runs every 60s.
    {% else %}
      No Kalshi credentials configured, so there is nothing to poll.
      Set <code>KALSHI_API_KEY</code> and <code>KALSHI_PRIVATE_KEY</code> as Railway service variables.
    {% endif %}
  </div>
  {%- endif %}
</div>

<!-- ============ positions + equity ============ -->
<div class="row two">
  <div class="panel">
    <div class="ph"><h2>Open positions</h2><span class="note">{{ s.positions|length }} row{{ '' if s.positions|length == 1 else 's' }}</span></div>
    <div class="scroll">
      {%- if s.positions %}
      <table><thead><tr><th>Market</th><th>Side</th><th class="num">Entry</th><th class="num">Qty</th><th>Strategy</th><th class="num">SL</th><th class="num">TP</th></tr></thead><tbody id="posBody">
      {%- for p in s.positions %}
        <tr>
          <td class="mono" title="{{ p.market_id }}">{{ p.market_id[:26] }}</td>
          <td><span class="tag">{{ p.side }}</span></td>
          <td class="num">{{ '%.3f'|format(p.entry_price) if p.entry_price is not none else '-' }}</td>
          <td class="num">{{ p.quantity }}</td>
          <td><span class="tag">{{ p.strategy }}</span></td>
          <td class="num">{{ p.stop_loss if p.stop_loss is not none else '-' }}</td>
          <td class="num">{{ p.take_profit if p.take_profit is not none else '-' }}</td>
        </tr>
      {%- endfor %}
      </tbody></table>
      {%- else %}
      <div class="empty">No open positions. The bot opens one after an LLM decision clears the confidence threshold.</div>
      {%- endif %}
    </div>
  </div>

  <div class="panel">
    <div class="ph"><h2>Cumulative realized P&amp;L</h2><span class="note" id="pnlSummary"></span></div>
    <div class="pb"><canvas id="pnlChart"></canvas></div>
  </div>
</div>

<!-- ============ performance + recent trades ============ -->
<div class="row half">
  <div class="panel">
    <div class="ph"><h2>Strategy performance</h2><span class="note">closed trades by strategy</span></div>
    {%- if s.by_strategy %}
    <table><thead><tr><th>Strategy</th><th class="num">Trades</th><th class="num">Win rate</th><th class="num">P&amp;L</th><th class="num">Best</th><th class="num">Worst</th></tr></thead><tbody>
    {%- for r in s.by_strategy %}
      <tr>
        <td><span class="tag">{{ r.strategy }}</span></td>
        <td class="num">{{ r.trades }}</td>
        <td class="num">{{ r.win_rate }}%</td>
        <td class="num {{ 'up' if r.pnl > 0 else ('down' if r.pnl < 0 else 'flat') }}">{{ r.pnl }}</td>
        <td class="num up">{{ r.best }}</td>
        <td class="num down">{{ r.worst }}</td>
      </tr>
    {%- endfor %}
    </tbody></table>
    {%- else %}
    <div class="empty">No closed trades to break down yet.</div>
    {%- endif %}
  </div>

  <div class="panel">
    <div class="ph"><h2>Recent closed trades</h2><span class="note">{{ s.recent_trades|length }} most recent</span></div>
    {%- if s.recent_trades %}
    <div class="scroll">
    <table><thead><tr><th>Market</th><th>Side</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">Qty</th><th class="num">P&amp;L</th><th>Exited</th></tr></thead><tbody>
    {%- for t in s.recent_trades %}
      <tr>
        <td class="mono" title="{{ t.market_id }}">{{ t.market_id[:24] }}</td>
        <td>{{ t.side }}</td>
        <td class="num">{{ '%.3f'|format(t.entry_price) }}</td>
        <td class="num">{{ '%.3f'|format(t.exit_price) }}</td>
        <td class="num">{{ t.quantity }}</td>
        <td class="num {{ 'up' if t.pnl > 0 else ('down' if t.pnl < 0 else 'flat') }}">{{ t.pnl }}</td>
        <td class="mono" style="color:var(--faint)">{{ (t.exit_timestamp or '')[:16] }}</td>
      </tr>
    {%- endfor %}
    </tbody></table>
    </div>
    {%- else %}
    <div class="empty">No closed trades recorded yet.</div>
    {%- endif %}
  </div>
</div>

<!-- ============ strategies ============ -->
<div class="panel" style="margin-bottom:12px">
  <div class="ph"><h2>Strategies</h2><span class="note">what each one actually does</span></div>
  <div class="pb" style="padding:0">
    <table><thead><tr><th style="width:170px">Strategy</th><th>Approach</th><th style="width:80px">Mode</th><th style="width:100px">State</th><th style="width:150px">Actions</th></tr></thead><tbody>
    {%- for b in s.bots %}
      <tr>
        <td><strong>{{ b.label }}</strong><div class="mono" style="color:var(--faint)">{{ b.name }}</div></td>
        <td style="color:var(--dim)">{{ b.description }}</td>
        <td>{{ b.mode }}</td>
        <td>{% if b.running %}<span class="pill ok"><span class="dot pulse"></span>running</span>{% else %}<span class="pill no">stopped</span>{% endif %}</td>
        <td>
          <div class="bar">
            <button onclick="toggleStrategy('{{ b.name }}')">{% if b.running %}Stop{% else %}Start{% endif %}</button>
            <button class="danger" onclick="killBot('{{ b.name }}')">Kill</button>
          </div>
        </td>
      </tr>
    {%- endfor %}
    </tbody></table>
  </div>
</div>

<!-- ============ what it does ============ -->
<div class="panel" style="margin-bottom:12px">
  <div class="ph"><h2>What this system does</h2><span class="note">the trading pipeline</span></div>
  <div class="pb">
    <p style="color:var(--dim);font-size:12.5px;margin-bottom:10px">
      Kalshi-Frigo scans prediction markets, asks an LLM whether each contract is mispriced,
      sizes a position with fractional Kelly sizing, then manages exits with stop-loss,
      take-profit and time-based rules. Every scan is recorded in SQLite &mdash; that is what
      the numbers on this page are read from.
    </p>
    <ol class="steps">
      <li><b>Ingest</b> &mdash; pulls open markets from the Kalshi API and upserts prices into <code>markets</code>.</li>
      <li><b>Decide</b> &mdash; scores each eligible market with an LLM pass via OpenRouter, under a daily cost budget.</li>
      <li><b>Execute</b> &mdash; sizes with quarter-Kelly and places orders, with price-sanity and fail-closed balance checks.</li>
      <li><b>Track</b> &mdash; monitors open positions and closes them on stop-loss, take-profit, time or resolution, writing each close to <code>trade_logs</code>.</li>
      <li><b>Evaluate</b> &mdash; aggregates realized P&amp;L, win rate and per-strategy attribution.</li>
    </ol>
    <div class="bar" style="margin-top:12px">
      <span class="tag">python cli.py run --paper</span>
      <span class="tag">--safe-compounder</span>
      <span class="tag">--beast</span>
      <span class="tag">--live</span>
      <span style="font-size:11.5px;color:var(--faint)">Live trading additionally needs <code>LIVE_TRADING_ENABLED=true</code>.</span>
    </div>
  </div>
</div>

<!-- ============ config + alerts ============ -->
<div class="row half">
  <div class="panel">
    <div class="ph"><h2>Config editor</h2><span class="note" id="configStatus">in-memory only</span></div>
    <div class="pb">
      {%- for key, value in s.config.items() %}
        {%- if value is sameas true or value is sameas false %}
        <div class="field"><label><input type="checkbox" data-key="{{ key }}" data-type="boolean" {{ 'checked' if value else '' }}> {{ key }}</label></div>
        {%- else %}
        <div class="field"><label>{{ key }}</label>
          <input type="text" data-key="{{ key }}" data-type="{{ 'number' if value is number else 'string' }}" value="{{ value }}">
        </div>
        {%- endif %}
      {%- endfor %}
      <div class="bar" style="margin-top:10px"><button onclick="saveConfig()">Save config</button></div>
    </div>
  </div>

  <div class="panel">
    <div class="ph"><h2>Alerts</h2><span class="note" id="alertStatus"></span></div>
    <div class="pb">
      <div class="field"><label>Telegram webhook</label><input type="text" id="tgUrl" value="{{ s.alerts.telegram_url if s.alerts else '' }}" placeholder="https://api.telegram.org/..."></div>
      <div class="field"><label>Discord webhook</label><input type="text" id="dcUrl" value="{{ s.alerts.discord_url if s.alerts else '' }}" placeholder="https://discord.com/api/webhooks/..."></div>
      <div class="field"><label><input type="checkbox" id="alertEnabled" {{ 'checked' if s.alerts and s.alerts.enabled else '' }}> Enable alerts</label></div>
      <div class="bar" style="margin-top:10px"><button onclick="saveAlerts()">Save alerts</button></div>
    </div>
  </div>
</div>

<!-- ============ logs + feed ============ -->
<div class="row half">
  <div class="panel">
    <div class="ph"><h2>Log tail</h2><span class="bar"><input type="text" id="logLines" value="100" style="width:64px"><button onclick="loadLogs()">Load</button></span></div>
    <div class="pb"><pre id="logOutput">{% if s.logs %}{{ s.logs|join('\n')|e }}{% else %}No log files yet. Start a strategy to generate logs.{% endif %}</pre></div>
  </div>

  <div class="panel">
    <div class="ph"><h2>Live event feed</h2><span class="note">server-sent events</span></div>
    <div class="pb"><pre id="tradeFeed">Connected. Waiting for events&hellip;</pre></div>
  </div>
</div>

<footer>Kalshi-Frigo &middot; prediction-market automation &middot; not financial advice</footer>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
// Server-rendered snapshot: the page is already correct before any fetch runs,
// and the poller below refreshes the exact same payload in place.
const SNAPSHOT = {{ s | tojson }};
const EQUITY = { labels: [], pnl: [] };
Object.assign(EQUITY, SNAPSHOT.equity || {});
const $ = id => document.getElementById(id);
const esc = v => String(v == null ? '' : v);

// --- live event feed ---
const feed = [];
function note(msg) {
  feed.unshift('[' + new Date().toLocaleTimeString() + '] ' + msg);
  if (feed.length > 60) feed.length = 60;
  $('tradeFeed').textContent = feed.join('\n');
}
const es = new EventSource('/api/stream');
es.onmessage = e => {
  try { note(JSON.stringify(JSON.parse(e.data))); } catch (_) { note(e.data); }
};
['strategy', 'alert', 'config'].forEach(n =>
  es.addEventListener(n, e => note(n + ': ' + e.data)));
es.onerror = () => note('stream disconnected - retrying...');

// --- snapshot refresh ---
// Re-paints the live panels in place from the same payload the server rendered,
// so an open tab tracks Kalshi without a reload.
function rows(cells, emptyMsg, colspan) {
  if (!cells.length) return '<tr><td colspan="' + colspan + '" class="empty">' + emptyMsg + '</td></tr>';
  return cells.map(c => '<tr>' + c + '</tr>').join('');
}
function num(v) { return v == null ? '-' : v; }
function sgn(v) { return v > 0 ? 'up' : (v < 0 ? 'down' : 'flat'); }

function paint(s) {
  if (!s) return;
  const k = s.kalshi || {};

  const mk = k.markets || [], ev = k.events || [];
  const mkBody = document.getElementById('kalshiMarkets');
  const evBody = document.getElementById('kalshiEvents');
  if (mkBody) {
    mkBody.innerHTML = rows(mk.map(r =>
      '<td class="mono">' + esc(r.ticker) + '</td>' +
      '<td class="num">' + num(r.shares) + '</td>' +
      '<td class="num">' + num(r.exposure) + '</td>' +
      '<td class="num">' + num(r.traded) + '</td>' +
      '<td class="num ' + sgn(r.realized) + '">' + num(r.realized) + '</td>' +
      '<td class="num" style="color:var(--faint)">' + num(r.fees) + '</td>'
    ), 'No market positions.', 6);
  }
  if (evBody) {
    evBody.innerHTML = rows(ev.map(r =>
      '<td class="mono">' + esc(r.event || r.ticker) + '</td>' +
      '<td class="num">' + num(r.shares) + '</td>' +
      '<td class="num">' + num(r.cost) + '</td>' +
      '<td class="num">' + num(r.exposure) + '</td>' +
      '<td class="num ' + sgn(r.realized) + '">' + num(r.realized) + '</td>' +
      '<td class="num" style="color:var(--faint)">' + num(r.fees) + '</td>'
    ), 'No event positions.', 6);
  }

  const posBody = document.getElementById('posBody');
  if (posBody) {
    posBody.innerHTML = rows((s.positions || []).map(p =>
      '<td class="mono">' + esc(String(p.market_id || '?').slice(0, 26)) + '</td>' +
      '<td><span class="tag">' + esc(p.side) + '</span></td>' +
      '<td class="num">' + (p.entry_price == null ? '-' : Number(p.entry_price).toFixed(3)) + '</td>' +
      '<td class="num">' + num(p.quantity) + '</td>' +
      '<td><span class="tag">' + esc(p.strategy) + '</span></td>' +
      '<td class="num">' + num(p.stop_loss) + '</td>' +
      '<td class="num">' + num(p.take_profit) + '</td>'
    ), 'No open positions.', 7);
  }

  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set('tBalance', s.balance == null ? '-' : '$' + Number(s.balance).toFixed(2));
  set('tLivePos', (k.market_count || 0) + (k.event_count || 0));
  const money = v => '$' + Number(v || 0).toFixed(2);
  set('tExposure', money(k.exposure));
  set('tKalshiPnl', money(k.realized));
  const kr = document.getElementById('tKalshiPnl');
  if (kr) kr.className = 'v ' + sgn(k.realized || 0);

  const t = s.trades || {}, d = s.data || {};
  set('tBotPnl', money(t.realized_pnl));
  const br = document.getElementById('tBotPnl');
  if (br) br.className = 'v ' + sgn(t.realized_pnl || 0);
  set('tWinRate', (t.win_rate || 0) + '%');
  set('tAiSpend', money(d.ai_cost_today));
  set('tRunning', s.running_count || 0);
  set('tUptime', Math.floor((s.uptime_sec || 0) / 60) + 'm');

  SNAPSHOT.equity = s.equity || SNAPSHOT.equity;
  set('pnlSummary', SNAPSHOT.equity.pn.length
    ? SNAPSHOT.equity.pn.length + ' closed trades'
    : 'no closed trades yet');
}

async function refresh() {
  try {
    paint(await fetch('/api/snapshot').then(r => r.json()));
    drawChart();
    try { paintMode(await fetch('/api/mode').then(r => r.json())); } catch (_) {}
  } catch (e) {
    note('snapshot refresh failed: ' + e.message);
  }
}

// --- DRY / LIVE mode -----------------------------------------------------
// The token is held in sessionStorage, never in the URL, so it does not leak
// into shared links or Referer headers.
function authHeaders(extra) {
  return Object.assign({ 'Content-Type': 'application/json' },
    { 'X-Auth-Token': sessionStorage.getItem('frigoToken') || '' }, extra || {});
}

function setMode(mode) {
  if (mode === 'live') {
    const ok = confirm(
      'GO LIVE?\n\n' +
      'Every order from now on is a REAL order on the Kalshi PRODUCTION account ' +
      'with REAL money. Simulated fills stop.\n\n' +
      'Type LIVE to confirm:');
    if (ok === null) return;
    if (String(ok).trim().toUpperCase() !== 'LIVE') {
      note('LIVE cancelled - confirmation did not match');
      return;
    }
  }
  postMode(mode, mode === 'live');
}

async function postMode(mode, confirmLive) {
  const r = await fetch('/api/mode', {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({ mode: mode, confirm: !!confirmLive }),
  });
  const d = await r.json().catch(() => ({}));
  if (d.error) {
    note('mode switch blocked: ' + d.error);
    alert(d.error);
    return;
  }
  note('trading mode is now ' + String(d.mode).toUpperCase());
  paintMode(d);
}

async function resetDry() {
  const r = await fetch('/api/dry/reset', { method: 'POST', headers: authHeaders() });
  const d = await r.json().catch(() => ({}));
  if (d.error) { $('dryStatus').textContent = d.error; return; }
  $('dryStatus').textContent = 'reset to ' + money(d.dry.starting_balance);
  paintMode(d);
}

function money(v) { return '$' + Number(v || 0).toFixed(2); }

function paintMode(d) {
  if (!d) return;
  const live = d.mode === 'live';
  $('modeDry').classList.toggle('on', !live);
  $('modeLive').classList.toggle('on', live);
  const dry = d.dry || {};
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set('dCash', money(dry.cash));
  set('dDeployed', money(dry.deployed));
  set('dEquity', money(dry.equity));
  set('dRealized', money(dry.realized));
  set('tDryCash', money(dry.cash));
  set('dLedger', dry.ledger_entries);
  set('dRet', (dry.return_pct || 0) + '%');
  const pnl = dry.total_pnl || 0;
  set('dPnl', money(pnl));
  const p = $('dPnl');
  if (p) p.className = pnl > 0 ? 'up' : (pnl < 0 ? 'down' : 'flat');
  const f = d.funding || {};
  set('fBalance', money(f.balance));
}

// Token prompt: the write controls need it, reads do not.
function ensureToken() {
  if (sessionStorage.getItem('frigoToken')) return true;
  const t = prompt('Dashboard write actions need DASHBOARD_TOKEN:');
  if (t) { sessionStorage.setItem('frigoToken', t.trim()); return true; }
  return false;
}
const _oldNote = note;
note = function (msg) {
  if (/blocked|unauthorized|locked|needs DASHBOARD_TOKEN/i.test(msg)) ensureToken();
  _oldNote(msg);
};

// --- controls ---
async function toggleStrategy(name) {
  const r = await fetch('/api/strategy/' + encodeURIComponent(name) + '/toggle', {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({ mode: 'paper' }),
  });
  const d = await r.json().catch(() => ({}));
  note(d.error ? name + ': ' + d.error : name + ': ' + (d.running ? 'started' : 'stopped'));
  refresh();
}
async function killBot(name) {
  await fetch('/api/bot/' + encodeURIComponent(name) + '/kill',
    { method: 'POST', headers: authHeaders() });
  note(name + ': kill requested');
  refresh();
}
async function saveAlerts() {
  const r = await fetch('/api/alerts', {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({
      telegram_url: $('tgUrl').value,
      discord_url: $('dcUrl').value,
      enabled: $('alertEnabled').checked,
    }),
  });
  const d = await r.json().catch(() => ({}));
  $('alertStatus').textContent = d.ok ? 'saved' : 'error';
}
async function saveConfig() {
  const obj = {};
  document.querySelectorAll('.pb input[data-key]').forEach(i => {
    const t = i.dataset.type;
    if (t === 'boolean') obj[i.dataset.key] = i.checked;
    else if (t === 'number') obj[i.dataset.key] = i.value === '' ? null : Number(i.value);
    else obj[i.dataset.key] = i.value;
  });
  const r = await fetch('/api/config', {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(obj),
  });
  const d = await r.json().catch(() => ({}));
  $('configStatus').textContent = d.ok ? 'saved ' + (d.updated || []).length + ' field(s)' : 'error';
}
async function loadLogs() {
  const n = $('logLines').value || 100;
  const r = await fetch('/api/logs?lines=' + encodeURIComponent(n)).then(r => r.json());
  $('logOutput').textContent = (r.lines || []).join('\n') || 'No log files yet.';
}

// --- equity chart, drawn from server-rendered data ---
let chart;
function drawChart() {
  if (typeof Chart === 'undefined') return;
  const ctx = $('pnlChart');
  if (!ctx) return;
  if (chart) chart.destroy();
  const flat = !EQUITY.pn.length;
  chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: flat ? ['no closed trades yet'] : EQUITY.labels,
      datasets: [{
        label: 'Cumulative P&L ($)',
        data: flat ? [0] : EQUITY.pnl,
        borderColor: '#2ee6a8',
        backgroundColor: 'rgba(46,230,168,.10)',
        tension: 0.3, fill: true, pointRadius: 2,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { labels: { color: '#e6edf6', boxWidth: 10 } } },
      scales: {
        x: { ticks: { color: '#5b6a80', maxTicksLimit: 7 }, grid: { color: 'rgba(255,255,255,.04)' } },
        y: { ticks: { color: '#5b6a80' }, grid: { color: 'rgba(255,255,255,.04)' } },
      },
    },
  });
}

// --- init ---
drawChart();
loadLogs();
refresh();
setInterval(refresh, 10000);
setInterval(loadLogs, 30000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
_workers_started = False


def start_background_workers():
    """Start the monitor and log-tail threads exactly once per process.

    gunicorn imports this module in the master process before forking, so the
    threads must be started after the fork (from the gunicorn hook) or they
    will not exist in the workers.
    """
    global _workers_started
    if _workers_started:
        return
    _workers_started = True
    for target in (_monitor_loop, _log_tail_loop):
        threading.Thread(target=target, daemon=True).start()


def main():
    """Run the built-in development server.

    Production (Railway) uses gunicorn instead:
        gunicorn --config gunicorn.conf.py web_dashboard:app
    gunicorn only runs on POSIX, so `python web_dashboard.py` stays the way to
    start the app on Windows.
    """
    import signal

    def handle_signal(signum, frame):
        for st in strategy_state.values():
            if st.get("pid"):
                try:
                    _stop_child(st)
                except Exception:
                    pass
        os._exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):
            pass  # not on the main thread (e.g. under a WSGI server)

    start_background_workers()

    port = int(os.environ.get("PORT", 8080))
    print(f"[web_dashboard] Kalshi-Frigo dashboard starting on port {port}", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
