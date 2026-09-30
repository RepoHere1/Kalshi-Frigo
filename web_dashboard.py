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
    "db_positions_count": 0,
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


def _monitor_loop():
    """Background thread: credentials, Kalshi connect, DB init."""
    dashboard_state["status"] = "online"

    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")

    dashboard_state["has_kalshi_creds"] = kalshi_configured()
    dashboard_state["has_openrouter_creds"] = bool(openrouter_key)

    # Make sure the schema exists so the dashboard shows real numbers even
    # on a fresh container where the trading loop has never run.
    try:
        _db()
    except Exception as e:
        _push_error(f"Database init: {e}")

    if not dashboard_state["has_kalshi_creds"]:
        _push_error("Kalshi credentials not configured — info mode only")
        dashboard_state["last_update"] = _now()
        return

    try:
        balance, positions = _run_async(_fetch_kalshi_data())
        if balance:
            dashboard_state["balance"] = balance.get("balance", 0) / 100.0
        if positions:
            # Kalshi uses different keys for event markets vs market tickers.
            merged = list(positions.get("market_positions") or [])
            merged += list(positions.get("event_positions") or [])
            dashboard_state["positions"] = merged
        dashboard_state["last_update"] = _now()
    except Exception as e:
        _push_error(f"Kalshi connect: {e}")


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


def _kalshi_positions() -> List[Dict[str, Any]]:
    raw: List[Dict[str, Any]] = cast(List[Dict[str, Any]], dashboard_state["positions"])
    out = []
    for p in raw:
        out.append(
            {
                "market_id": p.get("ticker") or p.get("market_ticker") or "?",
                "side": p.get("side", "?"),
                "entry_price": (p.get("last_entry_price") or 0) / 100.0,
                "quantity": p.get("position", 0),
                "strategy": "kalshi_api",
                "stop_loss": None,
                "take_profit": None,
                "status": p.get("status", "open"),
                "timestamp": str(p.get("last_update_time", "")),
                "source": "kalshi",
            }
        )
    return out


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
    if not positions:
        # Kalshi is the source of truth for anything the local DB missed.
        positions = _kalshi_positions()
    dashboard_state["db_positions_count"] = len(positions)

    running = _running_strategies()
    errors: List[Dict[str, Any]] = cast(List[Dict[str, Any]], dashboard_state["errors"])

    return {
        "generated_at": _now(),
        "uptime_sec": int(time.time() - _STARTED_AT),
        "status": dashboard_state["status"],
        "last_update": dashboard_state["last_update"],
        "has_kalshi_creds": kalshi_configured(),
        "has_openrouter_creds": bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
        "balance": dashboard_state["balance"],
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
        "logs": _read_log_tail(100) or list(log_buffer)[-100:],
        "errors": errors[-10:],
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
<title>Kalshi-Frigo — AI Trading Dashboard</title>
<style>
:root { --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#c9d1d9;
  --muted:#8b949e; --green:#3fb950; --red:#f85149; --blue:#58a6ff;
  --yellow:#d29922; --purple:#bc8cff; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:var(--bg); color:var(--text); min-height:100vh; padding:16px; }
.container { max-width:1300px; margin:0 auto; }
h1 { font-size:1.5rem; margin-bottom:.25rem; }
.subtitle { color:var(--muted); margin-bottom:1rem; font-size:.85rem; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:8px; margin-bottom:12px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:12px; margin-bottom:10px; }
.card h2 { font-size:.95rem; margin-bottom:8px; color:#fff; border-bottom:1px solid var(--border); padding-bottom:6px; }
.stat { text-align:center; background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:8px; }
.stat-value { font-size:1.3rem; font-weight:700; }
.stat-label { font-size:.65rem; color:var(--muted); text-transform:uppercase; }
.badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:.65rem; font-weight:600; }
.badge.ok { background:#033a16; color:var(--green); }
.badge.no { background:#1c2128; color:var(--muted); }
.badge.warn { background:#3d2e00; color:var(--yellow); }
table { width:100%; border-collapse:collapse; font-size:.8rem; }
th { text-align:left; padding:4px 6px; color:var(--muted); font-size:.65rem; text-transform:uppercase; border-bottom:2px solid var(--border); }
td { padding:4px 6px; border-bottom:1px solid var(--border); }
button { background:var(--blue); color:#fff; border:none; padding:4px 10px; border-radius:6px; cursor:pointer; font-size:.75rem; }
button:hover { opacity:.85; }
button.danger { background:var(--red); }
input, textarea { background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:4px 8px; font-size:.8rem; width:100%; }
input[type=checkbox] { width:auto; margin:0 6px 0 0; padding:0; vertical-align:middle; accent-color:var(--blue); }
pre { background:var(--bg); border:1px solid var(--border); border-radius:6px; padding:8px; font-size:.7rem; max-height:300px; overflow:auto; white-space:pre-wrap; }
.two-col { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.flex-row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.tag { display:inline-block; background:#21262d; color:var(--blue); padding:1px 6px; border-radius:8px; font-size:.6rem; margin-right:3px; }
.err { color:var(--muted); text-align:center; padding:8px; }
.pos { color:var(--green); }
.neg { color:var(--red); }
canvas { max-height:250px; }
.kv { display:grid; grid-template-columns:auto 1fr; gap:4px 10px; font-size:.78rem; }
.kv dt { color:var(--muted); }
.kv dd { text-align:right; }
.desc { font-size:.72rem; color:var(--muted); }
.steps { font-size:.78rem; line-height:1.7; }
.steps li { margin-bottom:2px; }
code { background:var(--bg); border:1px solid var(--border); border-radius:4px; padding:0 4px; font-size:.72rem; }
</style>
</head>
<body>
<div class="container">
<h1>&#129504; Kalshi-Frigo Dashboard</h1>
<p class="subtitle">
  AI trading bot for Kalshi prediction markets &middot; rendered {{ s.generated_at }}
  {%- if s.public_domain %} &middot; <code>{{ s.public_domain }}</code>{% endif %}
</p>

<div class="grid">
  <div class="stat"><div class="stat-value">{% if s.status == 'online' %}<span class="badge ok">ONLINE</span>{% else %}<span class="badge no">OFF</span>{% endif %}</div><div class="stat-label">Status</div></div>
  <div class="stat"><div class="stat-value">{{ s.balance if s.balance is not none else '-' }}</div><div class="stat-label">Balance $</div></div>
  <div class="stat"><div class="stat-value" id="sPositions">{{ s.open.positions if s.open else 0 }}</div><div class="stat-label">Open positions</div></div>
  <div class="stat"><div class="stat-value" id="sCapital">{{ s.open.capital if s.open else 0 }}</div><div class="stat-label">Capital at risk</div></div>
  <div class="stat"><div class="stat-value {{ 'pos' if s.trades and s.trades.realized_pnl > 0 else ('neg' if s.trades and s.trades.realized_pnl < 0 else '') }}">{{ s.trades.realized_pnl if s.trades else 0 }}</div><div class="stat-label">Realized P&amp;L</div></div>
  <div class="stat"><div class="stat-value">{{ s.trades.win_rate if s.trades else 0 }}%</div><div class="stat-label">Win rate</div></div>
  <div class="stat"><div class="stat-value">{{ s.trades.trades if s.trades else 0 }}</div><div class="stat-label">Closed trades</div></div>
  <div class="stat"><div class="stat-value">{{ s.data.ai_cost_today if s.data else 0 }}<span style="font-size:.7rem;color:var(--muted)">/{{ s.data.ai_budget if s.data else 0 }}</span></div><div class="stat-label">AI spend today $</div></div>
  <div class="stat"><div class="stat-value">{{ s.running_count if s.running_count is defined else 0 }}</div><div class="stat-label">Strategies running</div></div>
  <div class="stat"><div class="stat-value" id="sUptime">{{ (s.uptime_sec // 60) if s.uptime_sec is defined else 0 }}m</div><div class="stat-label">Uptime</div></div>
</div>

<div class="two-col">
  <div class="card">
    <h2>&#9881; System readiness</h2>
    <dl class="kv">
      <dt>Kalshi API credentials</dt>
      <dd>{% if s.has_kalshi_creds %}<span class="badge ok">CONFIGURED</span>{% else %}<span class="badge no">MISSING</span>{% endif %}</dd>
      <dt>OpenRouter API key</dt>
      <dd>{% if s.has_openrouter_creds %}<span class="badge ok">CONFIGURED</span>{% else %}<span class="badge no">MISSING</span>{% endif %}</dd>
      <dt>Database</dt>
      <dd>{% if s.db_exists %}<span class="badge ok">PRESENT</span>{% else %}<span class="badge warn">MISSING</span>{% endif %}</dd>
      <dt>Database persistence</dt>
      <dd>{% if s.db_persistent %}<span class="badge ok">VOLUME</span>{% else %}<span class="badge warn">EPHEMERAL</span>{% endif %}</dd>
      <dt>Database path</dt>
      <dd><code>{{ s.db_path }}</code></dd>
      <dt>Last Kalshi sync</dt>
      <dd>{{ s.last_update or 'never' }}</dd>
    </dl>
    {% if not s.db_persistent %}
    <p class="desc" style="margin-top:8px">
      <span class="badge warn">Why the tables are empty</span>
      The database lives in the container filesystem, so every redeploy recreates it
      from empty. Mount a Railway volume and set <code>DB_PATH</code> inside it to
      keep trade history between deployments.
    </p>
    {% endif %}
  </div>

  <div class="card">
    <h2>&#128202; Data inventory</h2>
    <dl class="kv">
      {%- for name, count in (s.data.tables if s.data else {}).items() %}
      <dt><code>{{ name }}</code></dt><dd>{{ count }} row{{ '' if count == 1 else 's' }}</dd>
      {%- endfor %}
      <dt>LLM queries logged</dt><dd>{{ s.data.llm_queries if s.data else 0 }}</dd>
      <dt>LLM tokens used</dt><dd>{{ s.data.llm_tokens if s.data else 0 }}</dd>
      <dt>LLM spend (all time)</dt><dd>${{ s.data.llm_cost if s.data else 0 }}</dd>
      <dt>Analyses today</dt><dd>{{ s.data.ai_analyses_today if s.data else 0 }}</dd>
    </dl>
  </div>
</div>

<div class="card">
  <h2>&#9888; Recent errors</h2>
  <div id="sErrors" class="flex-row">
    {%- if s.errors %}
    {%- for e in s.errors[-5:]|reverse %}
    <span class="tag">{{ e.time }} {{ e.error }}</span>
    {%- endfor %}
    {%- else %}
    <span class="badge ok">none</span>
    {%- endif %}
  </div>
</div>

<div class="two-col">
  <div class="card">
    <h2>&#128205; Open positions</h2>
    <table id="posTable"><thead><tr><th>Market</th><th>Side</th><th>Entry</th><th>Qty</th><th>Strategy</th><th>SL</th><th>TP</th></tr></thead><tbody>
    {%- if s.positions %}
    {%- for p in s.positions %}
      <tr>
        <td title="{{ p.market_id }}">{{ p.market_id[:20] }}</td>
        <td>{{ p.side }}</td><td>{{ '%.3f'|format(p.entry_price) }}</td>
        <td>{{ p.quantity }}</td><td><span class="tag">{{ p.strategy }}</span></td>
        <td>{{ p.stop_loss if p.stop_loss is not none else '-' }}</td>
        <td>{{ p.take_profit if p.take_profit is not none else '-' }}</td>
      </tr>
    {%- endfor %}
    {%- else %}
      <tr><td colspan="7" class="err">No open positions</td></tr>
    {%- endif %}
    </tbody></table>
  </div>

  <div class="card">
    <h2>&#128200; Cumulative realized P&amp;L</h2>
    <canvas id="pnlChart"></canvas>
    <p class="desc" id="pnlSummary">
      {%- if s.equity and s.equity.pn %}
      {{ s.equity.pn|length }} closed trade{{ '' if s.equity.pn|length == 1 else 's' }},
      ending at ${{ s.equity.pn[-1] }}.
      {%- else %}
      No closed trades yet, so the equity curve is flat at $0.
      {%- endif %}
    </p>
  </div>
</div>

<div class="two-col">
  <div class="card">
    <h2>&#128200; Strategy performance</h2>
    <table id="perfTable"><thead><tr><th>Strategy</th><th>Trades</th><th>Wins</th><th>Win&nbsp;rate</th><th>P&amp;L</th><th>Best</th><th>Worst</th></tr></thead><tbody>
    {%- if s.by_strategy %}
    {%- for r in s.by_strategy %}
      <tr>
        <td><span class="tag">{{ r.strategy }}</span></td>
        <td>{{ r.trades }}</td><td>{{ r.wins }}</td><td>{{ r.win_rate }}%</td>
        <td class="{{ 'pos' if r.pnl > 0 else ('neg' if r.pnl < 0 else '') }}">{{ r.pnl }}</td>
        <td class="pos">{{ r.best }}</td><td class="neg">{{ r.worst }}</td>
      </tr>
    {%- endfor %}
    {%- else %}
      <tr><td colspan="7" class="err">No closed trades to break down yet</td></tr>
    {%- endif %}
    </tbody></table>
  </div>

  <div class="card">
    <h2>&#128197; Recent closed trades</h2>
    <table id="tradeTable"><thead><tr><th>Market</th><th>Side</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&amp;L</th><th>Exited</th></tr></thead><tbody>
    {%- if s.recent_trades %}
    {%- for t in s.recent_trades %}
      <tr>
        <td title="{{ t.market_id }}">{{ t.market_id[:20] }}</td>
        <td>{{ t.side }}</td>
        <td>{{ '%.3f'|format(t.entry_price) }}</td>
        <td>{{ '%.3f'|format(t.exit_price) }}</td>
        <td>{{ t.quantity }}</td>
        <td class="{{ 'pos' if t.pnl > 0 else ('neg' if t.pnl < 0 else '') }}">{{ t.pnl }}</td>
        <td>{{ t.exit_timestamp[:16] if t.exit_timestamp else '-' }}</td>
      </tr>
    {%- endfor %}
    {%- else %}
      <tr><td colspan="7" class="err">No closed trades recorded</td></tr>
    {%- endif %}
    </tbody></table>
  </div>
</div>

<div class="card">
  <h2>&#129302; Strategies and what they do</h2>
  <table id="botTable"><thead><tr><th>Strategy</th><th>Mode</th><th>PID</th><th>State</th><th>Actions</th></tr></thead><tbody>
  {%- for b in s.bots %}
    <tr>
      <td><strong>{{ b.name }}</strong><div class="desc">{{ b.label }}</div></td>
      <td>{{ b.mode }}</td>
      <td>{{ b.pid if b.pid else '-' }}</td>
      <td>{% if b.running %}<span class="badge ok">RUNNING</span>{% else %}<span class="badge no">STOPPED</span>{% endif %}</td>
      <td>
        <button onclick="toggleStrategy('{{ b.name }}')">{% if b.running %}Stop{% else %}Start{% endif %}</button>
        <button class="danger" onclick="killBot('{{ b.name }}')">Kill</button>
      </td>
    </tr>
  {%- endfor %}
  </tbody></table>
</div>

<div class="card">
  <h2>&#129518; What this system does</h2>
  <p class="desc" style="margin-bottom:8px">
    Kalshi-Frigo scans prediction markets, asks an LLM whether each contract is
    mispriced, sizes a position with fractional Kelly sizing, and manages exits
    with stop-loss, take-profit and time-based rules. Each scan is recorded in
    SQLite, which is what every number on this page is read from.
  </p>
  <ol class="steps">
    <li><strong>Ingest</strong> &mdash; pulls open markets from the Kalshi API and upserts prices into <code>markets</code>.</li>
    <li><strong>Decide</strong> &mdash; scores each eligible market with an LLM decision pass (OpenRouter), subject to a daily cost budget.</li>
    <li><strong>Execute</strong> &mdash; sizes with quarter-Kelly and places paper or live orders, with price-sanity and fail-closed balance checks.</li>
    <li><strong>Track</strong> &mdash; monitors open positions and closes them on stop-loss, take-profit, time or resolution, writing each close to <code>trade_logs</code>.</li>
    <li><strong>Evaluate</strong> &mdash; aggregates realized P&amp;L, win rate and per-strategy attribution.</li>
  </ol>
  <table style="margin-top:8px"><thead><tr><th>Strategy</th><th>Approach</th></tr></thead><tbody>
  {%- for d in s.strategy_docs %}
    <tr><td><span class="tag">{{ d.name }}</span></td><td class="desc">{{ d.description }}</td></tr>
  {%- endfor %}
  </tbody></table>
  <p class="desc" style="margin-top:8px">
    Run modes: <code>python cli.py run --paper</code> (AI directional),
    <code>--safe-compounder</code> (no LLM), <code>--beast</code> (aggressive),
    <code>--live</code> (real money). Live trading additionally requires
    <code>LIVE_TRADING_ENABLED=true</code> and valid Kalshi credentials.
  </p>
</div>

<div class="two-col">
  <div class="card">
    <h2>&#9881; Config editor</h2>
    <div id="configEditor">
    {%- for key, value in s.config.items() %}
      {%- if value is sameas true or value is sameas false %}
      <label style="display:flex;gap:6px;align-items:center;margin:4px 0;font-size:.75rem">
        <input type="checkbox" data-key="{{ key }}" data-type="boolean" {{ 'checked' if value else '' }}> {{ key }}
      </label>
      {%- else %}
      <label style="display:block;margin:4px 0;font-size:.75rem">{{ key }}
        <input data-key="{{ key }}" data-type="{{ 'number' if value is number else 'string' }}" value="{{ value }}" step="any" style="width:100%">
      </label>
      {%- endif %}
    {%- endfor %}
    </div>
    <button onclick="saveConfig()" style="margin-top:8px">Save Config</button>
    <span id="configStatus" style="font-size:.7rem;color:var(--muted);margin-left:6px"></span>
  </div>

  <div class="card">
    <h2>&#128276; Alerts</h2>
    <label>Telegram webhook <input id="tgUrl" value="{{ s.alerts.telegram_url if s.alerts else '' }}" style="width:80%"></label><br><br>
    <label>Discord webhook <input id="dcUrl" value="{{ s.alerts.discord_url if s.alerts else '' }}" style="width:80%"></label><br><br>
    <label><input type="checkbox" id="alertEnabled" {{ 'checked' if s.alerts and s.alerts.enabled else '' }}> Enable alerts</label><br><br>
    <button onclick="saveAlerts()">Save</button>
    <span id="alertStatus"></span>
  </div>
</div>

<div class="two-col">
  <div class="card">
    <h2>&#128203; Log viewer</h2>
    <div class="flex-row" style="margin-bottom:6px">
      <input id="logLines" value="100" style="width:80px">
      <button onclick="loadLogs()">Load</button>
    </div>
    <pre id="logOutput" style="max-height:250px">{% if s.logs %}{{ s.logs|join('\n')|e }}{% else %}(no log files yet — start a strategy to generate logs){% endif %}</pre>
  </div>

  <div class="card">
    <h2>&#128225; Real-time event feed</h2>
    <pre id="tradeFeed" style="max-height:250px">Listening for events&hellip;</pre>
  </div>
</div>

<footer style="margin-top:16px;text-align:center;color:var(--muted);font-size:.7rem">
  Kalshi-Frigo &middot; Prediction-market automation &middot; Not financial advice
</footer>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
// Server-rendered snapshot, so the page is already correct before any fetch.
const SNAPSHOT = {{ s | tojson }};
const EQUITY_LABELS = SNAPSHOT.equity ? SNAPSHOT.equity.labels : [];
const EQUITY_VALUES = SNAPSHOT.equity ? SNAPSHOT.equity.pnl : [];
const $ = id => document.getElementById(id);
const esc = v => String(v == null ? '' : v);
const num = (v, d=2) => (v == null || v === '' ? '-' : Number(v).toFixed(d));

// --- SSE event feed ---
// Server sends unnamed `data:` frames, so onmessage fires. Named events
// (event: strategy / alert / config) need explicit listeners.
const feedLines = [];
function pushFeed(msg) {
  feedLines.unshift('[' + new Date().toLocaleTimeString() + '] ' + msg);
  if (feedLines.length > 50) feedLines.length = 50;
  $('tradeFeed').textContent = feedLines.join('\n');
}
const evtSource = new EventSource('/api/stream');
evtSource.onmessage = e => {
  try { const d = JSON.parse(e.data); pushFeed(JSON.stringify(d)); } catch (_) { pushFeed(e.data); }
};
['strategy','alert','config'].forEach(name =>
  evtSource.addEventListener(name, e => pushFeed(name + ': ' + e.data)));
evtSource.onerror = () => pushFeed('stream disconnected — retrying...');

// --- Snapshot refresh ---
// Polls the same endpoint the page was server-rendered from, so the live view
// and the server-rendered HTML always agree.
function renderSnapshot(s) {
  if (!s) return;
  if (s.positions) {
    const tb = document.querySelector('#posTable tbody');
    tb.innerHTML = s.positions.length ? s.positions.map(p => `<tr>
      <td title="${esc(p.market_id)}">${esc(String(p.market_id||'?').slice(0,20))}</td>
      <td>${esc(p.side)}</td><td>${num(p.entry_price,3)}</td><td>${esc(p.quantity)}</td>
      <td><span class="tag">${esc(p.strategy)}</span></td>
      <td>${p.stop_loss!=null?num(p.stop_loss,3):'-'}</td>
      <td>${p.take_profit!=null?num(p.take_profit,3):'-'}</td></tr>`).join('')
      : '<tr><td colspan="7" class="err">No open positions</td></tr>';
  }
  if (s.trades) {
    $('sCapital') && ($('sCapital').textContent = (s.open ? s.open.capital : 0));
  }
  const errs = (s.errors || []).slice(-5).reverse();
  $('sErrors').innerHTML = errs.length
    ? errs.map(e => `<span class="tag">${esc(e.time)} ${esc(e.error)}</span>`).join('')
    : '<span class="badge ok">none</span>';
  drawChart();
}

async function loadSnapshot() {
  try { renderSnapshot(await fetch('/api/snapshot').then(r => r.json())); }
  catch (e) { pushFeed('snapshot refresh failed: ' + e.message); }
}

async function loadStatus() {
  try {
    const r = await fetch('/api/status').then(r => r.json());
    $('sPositions').textContent = (r.positions_count || 0) + (r.db_positions_count || 0);
    $('sUptime').textContent = Math.floor((r.uptime_sec || 0) / 60) + 'm';
  } catch (e) { pushFeed('status refresh failed: ' + e.message); }
}

// --- Strategies ---
async function toggleStrategy(name) {
  const r = await fetch('/api/strategy/' + encodeURIComponent(name) + '/toggle',
    { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({mode:'paper'}) });
  const d = await r.json().catch(() => ({}));
  if (d.error) pushFeed(name + ': ' + d.error);
  loadSnapshot();
}
async function killBot(name) {
  await fetch('/api/bot/' + encodeURIComponent(name) + '/kill', { method: 'POST' });
  loadSnapshot();
}

// --- Alerts ---
async function saveAlerts() {
  const r = await fetch('/api/alerts', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ telegram_url: $('tgUrl').value, discord_url: $('dcUrl').value, enabled: $('alertEnabled').checked }) });
  const d = await r.json().catch(() => ({}));
  $('alertStatus').textContent = d.ok ? 'Saved' : 'Error';
}

// --- Config ---
async function saveConfig() {
  const obj = {};
  document.querySelectorAll('#configEditor input').forEach(i => {
    const t = i.dataset.type;
    if (t === 'boolean') obj[i.dataset.key] = i.checked;
    else if (t === 'number') obj[i.dataset.key] = i.value === '' ? null : Number(i.value);
    else obj[i.dataset.key] = i.value;
  });
  const r = await fetch('/api/config', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(obj) });
  const d = await r.json().catch(() => ({}));
  $('configStatus').textContent = d.ok ? 'Saved: ' + (d.updated || []).length + ' field(s)' : 'Error';
}

// --- Logs ---
async function loadLogs() {
  const n = $('logLines').value || 100;
  const r = await fetch('/api/logs?lines=' + encodeURIComponent(n)).then(r => r.json());
  $('logOutput').textContent = (r.lines || []).join('\n') || '(no log files yet — start a strategy to generate logs)';
}
// --- Equity chart (server-rendered data, no fetch needed) ---
let pnlChart;
function drawChart() {
  if (typeof Chart === 'undefined') return;
  const ctx = $('pnlChart');
  if (!ctx) return;
  if (pnlChart) pnlChart.destroy();
  const flat = EQUITY_VALUES.length === 0;
  pnlChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: flat ? ['no closed trades yet'] : EQUITY_LABELS,
      datasets: [{ label: 'Cumulative P&L ($)', data: flat ? [0] : EQUITY_VALUES,
        borderColor: '#3fb950', backgroundColor: 'rgba(63,185,80,0.1)', tension: 0.3, fill: true, pointRadius: 2 }]
    },
    options: { responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { labels: { color: '#c9d1d9' } } },
      scales: { x: { ticks: { color: '#8b949e', maxTicksLimit: 8 } }, y: { ticks: { color: '#8b949e' } } } }
  });
}

// --- Init ---
async function init() {
  drawChart();
  loadLogs();
  loadSnapshot();
  setInterval(loadSnapshot, 5000);
  setInterval(loadStatus, 10000);
  setInterval(loadLogs, 30000);
}
init();
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
