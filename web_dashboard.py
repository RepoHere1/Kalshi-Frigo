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
import base64
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
    "market": {},
}

# Strategy control state
strategy_state = {
    "ai_directional": {"running": False, "pid": None, "mode": "paper"},
    "safe_compounder": {"running": False, "pid": None, "mode": "paper"},
    "beast_mode": {"running": False, "pid": None, "mode": "paper"},
    "market_making": {"running": False, "pid": None, "mode": "paper"},
    "quick_flip": {"running": False, "pid": None, "mode": "paper"},
    "btc_updown": {"running": False, "pid": None, "mode": "paper"},
}

# The command each strategy is actually run with. Every one is a distinct
# process and writes its own log.
#
# Two of these used to be fiction. `market_making` and `quick_flip` had no CLI
# entry point, so their buttons spawned the AI directional loop: two "strategies"
# on the page were the same bot twice, with one shared log. And `safe_compounder`
# ran a single pass and exited, so its button reported "started" for a process
# that was already gone. `--loop` is what makes a strategy continuous, so it is
# now part of the command rather than an afterthought.
STRATEGY_COMMANDS: Dict[str, List[str]] = {
    "ai_directional": ["cli.py", "run", "--paper", "--loop", "--interval", "300"],
    "safe_compounder": [
        "cli.py",
        "run",
        "--safe-compounder",
        "--paper",
        "--loop",
        "--interval",
        "300",
    ],
    "beast_mode": ["cli.py", "run", "--beast", "--paper"],
    "market_making": [
        "cli.py",
        "run",
        "--market-making",
        "--paper",
        "--loop",
        "--interval",
        "180",
    ],
    "quick_flip": ["cli.py", "run", "--quick-flip", "--paper", "--loop", "--interval", "120"],
    "btc_updown": ["cli.py", "run", "--btc-updown", "--paper", "--loop", "--interval", "0"],
}

# `trade_logs.strategy` and `positions.strategy` are written by the strategies
# themselves and are not consistent with the dashboard's button names. Without
# this map every card would read "unattributed" except ai_directional.
STRATEGY_ALIASES = {
    "ai_directional": "ai_directional",
    "ai directional": "ai_directional",
    "directional_trading": "ai_directional",
    "directional": "ai_directional",
    "safe_compounder": "safe_compounder",
    "safe compounding": "safe_compounder",
    "safe_compounding": "safe_compounder",
    "beast_mode": "beast_mode",
    "beast": "beast_mode",
    "market_making": "market_making",
    "market making": "market_making",
    "market_maker": "market_making",
    "quick_flip": "quick_flip",
    "quick_flip_scalping": "quick_flip",
    "quick flip": "quick_flip",
    "quick_flip_scalping_strategy": "quick_flip",
    "btc_updown": "btc_updown",
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


BACKUP_DIR_NAME = "backups"
BACKUP_INTERVAL_SEC = 6 * 60 * 60  # every 6h
BACKUP_KEEP = 48  # ~2 weeks


def backup_database() -> Optional[str]:
    """Snapshot the SQLite file with the online backup API.

    The Railway volume is a single copy in one region with no history, so a
    lost volume is a lost trade record. Uses sqlite3's backup() rather than a
    file copy because the database is written by several threads and a raw copy
    can capture a torn page.
    """
    import sqlite3

    src = DB_PATH
    if not Path(src).exists():
        return None
    out_dir = Path(src).parent / BACKUP_DIR_NAME
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = _now().replace(":", "").replace("-", "").replace(" ", "_")
        dest = out_dir / f"trading_system_{stamp}.db"
        with sqlite3.connect(src) as source, sqlite3.connect(dest) as target:
            source.backup(target)
    except Exception as e:
        _push_error(f"Database backup failed: {e}")
        return None
    _prune_backups(out_dir)
    _audit(f"database backed up to {dest.name}")
    return str(dest)


def _prune_backups(out_dir: Path) -> None:
    try:
        files = sorted(out_dir.glob("trading_system_*.db"), reverse=True)
        for stale in files[BACKUP_KEEP:]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass


def _backup_loop():
    """Back up on a timer, and once at startup."""
    while True:
        backup_database()
        time.sleep(BACKUP_INTERVAL_SEC)


def _backup_status() -> Dict[str, Any]:
    """How many snapshots exist on disk, for the readiness panel."""
    out_dir = Path(DB_PATH).parent / BACKUP_DIR_NAME
    try:
        count = len(list(out_dir.glob("trading_system_*.db")))
    except OSError:
        count = 0
    return {"count": count, "keep": BACKUP_KEEP, "every_hours": BACKUP_INTERVAL_SEC // 3600}


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
    "btc_updown": (
        "BTC 15-min up/down",
        'Reads Kalshi\'s own KXBTC15M contract - "BTC price up in next 15 mins?" - '
        "and compares its Up/Down price against live Coinbase spot. Takes one $5 "
        "clip only when the two disagree by more than the configured edge, never "
        "inside the noise band around the target, and never inside the 60-second "
        "settlement window. Settlement is CF Benchmarks BRTI, so spot is a proxy "
        "for it.",
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


def _market_data_loop():
    """Own the live BTC feeds for the life of the process.

    The feeds are websocket-driven and therefore async, while the dashboard's
    handlers are synchronous Flask routes. Rather than opening a loop per
    request - which would tear down the socket on every poll - one thread holds a
    single event loop and one long-lived connection, and the routes read the
    cached payload it produces.
    """
    import asyncio

    from src.jobs.market_data import MarketDataHub

    hub = MarketDataHub()

    async def _run() -> None:
        await hub.start()
        while True:
            try:
                dashboard_state["market"] = hub.chart_payload()
            except Exception as e:  # noqa: BLE001 - a bad payload must not kill the feed
                _push_error(f"Market data: {e}")
            await asyncio.sleep(2)

    def _thread_main() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run())
        except Exception as e:  # noqa: BLE001
            _push_error(f"Market data thread: {e}")
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_thread_main, daemon=True, name="market-data").start()


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
            "running": bool(st.get("running")),
            "pid": st.get("pid"),
            "mode": st.get("mode", "paper"),
            "label": STRATEGY_DOCS.get(name, ("", ""))[0],
            "description": STRATEGY_DOCS.get(name, ("", ""))[1],
            "started_at": st.get("started_at"),
            "stop_reason": st.get("stop_reason") or "",
        }
        for name, st in strategy_state.items()
    ]


def _strategy_bucket(name: Optional[str]) -> str:
    """Map whatever a trade log recorded onto one of the five buttons."""
    key = (name or "").strip().lower()
    return STRATEGY_ALIASES.get(key, key or "unattributed")


def _current_book_mode() -> str:
    """Which book's numbers the page is showing: 'dry' or 'live'."""
    try:
        from src.utils.mode import run

        return str(run(_mode_manager().current()))
    except Exception:
        return "dry"


def _strategy_cards(
    curve_rows: List[Dict[str, Any]],
    position_rows: List[Dict[str, Any]],
    book: str,
) -> List[Dict[str, Any]]:
    """One card per strategy: headline stats plus its own cumulative P&L curve.

    Everything is derived from persisted rows (`trade_logs`, `positions`), never
    from in-process counters, so a strategy's chart still has its history after a
    redeploy. A card with no closes yet renders an explicit empty state instead
    of a flat zero line that reads like a real result.
    """
    cards: Dict[str, Dict[str, Any]] = {}
    for name, st in strategy_state.items():
        label, description = STRATEGY_DOCS.get(name, (name, ""))
        cards[name] = {
            "name": name,
            "label": label,
            "description": description,
            "running": bool(st.get("running")),
            "pid": st.get("pid"),
            "mode": st.get("mode", "paper"),
            "command": " ".join(STRATEGY_COMMANDS.get(name, [])),
            "started_at": st.get("started_at"),
            # Shown on the card so "stopped" is never ambiguous about whether the
            # strategy ever ran, or how it ended.
            "stop_reason": st.get("stop_reason") or "",
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "realized": 0.0,
            "best": 0.0,
            "worst": 0.0,
            "open_positions": 0,
            "deployed": 0.0,
            "first_trade": None,
            "last_trade": None,
            "equity": {"labels": [], "pnl": []},
        }

    # Cumulative P&L per strategy over the book's own closes.
    running: Dict[str, float] = {}
    for row in curve_rows:
        if _resolve_book(row.get("mode")) != book:
            continue
        name = _strategy_bucket(row.get("strategy"))
        card = cards.get(name)
        if card is None:
            continue  # a close from a strategy this build no longer exposes
        pnl = float(row.get("pnl") or 0.0)
        running[name] = running.get(name, 0.0) + pnl
        card["trades"] += 1
        card["wins"] += 1 if pnl > 0 else 0
        card["losses"] += 0 if pnl > 0 else 1
        card["realized"] = round(running[name], 2)
        card["best"] = max(card["best"], round(pnl, 2))
        card["worst"] = min(card["worst"], round(pnl, 2))
        stamp = str(row.get("exit_timestamp") or "")[:19]
        if stamp:
            card["first_trade"] = card["first_trade"] or stamp
            card["last_trade"] = stamp
            card["equity"]["labels"].append(stamp)
            card["equity"]["pnl"].append(card["realized"])

    for row in position_rows:
        if _resolve_book(row.get("mode")) != book:
            continue
        card = cards.get(_strategy_bucket(row.get("strategy")))
        if card is None:
            continue
        card["open_positions"] += 1
        card["deployed"] = round(
            card["deployed"] + float(row.get("quantity") or 0) * float(row.get("entry_price") or 0),
            2,
        )

    for card in cards.values():
        card["win_rate"] = (
            round(100.0 * card["wins"] / card["trades"], 1) if card["trades"] else 0.0
        )
    return list(cards.values())


def _resolve_book(value: Optional[str]) -> str:
    """Normalize a stored book name. Anything unrecorded is a simulated row."""
    return (value or "dry").strip().lower()


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


def _book_filter(book: str) -> str:
    """SQL that selects exactly one book's rows.

    Everything the page reports - trade totals, the equity curve, the recent
    trades table, per-strategy performance, open positions - must answer with
    the book the switch says it is in. A DRY page quoting LIVE closes (or the
    reverse) is the identity leak this whole function exists to prevent.
    """
    wanted = "live" if str(book).strip().lower() == "live" else "dry"
    return f"COALESCE(NULLIF(mode, ''), 'dry') = '{wanted}'"


_SQL_TRADES = (
    "SELECT COUNT(*) AS trades,"
    " COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,"
    " COALESCE(SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END), 0) AS losses,"
    " COALESCE(SUM(pnl), 0.0) AS realized_pnl,"
    " COALESCE(AVG(pnl), 0.0) AS avg_pnl,"
    " COALESCE(MAX(pnl), 0.0) AS best_trade,"
    " COALESCE(MIN(pnl), 0.0) AS worst_trade"
    " FROM trade_logs WHERE {book}"
)
_SQL_OPEN = (
    "SELECT COUNT(*) AS positions,"
    " COALESCE(SUM(quantity * entry_price), 0.0) AS capital,"
    " COALESCE(SUM(CASE WHEN live = 1 THEN 1 ELSE 0 END), 0) AS live"
    " FROM positions WHERE status = 'open' AND {book}"
)
# The simulated book only. `live = 0` is what marks a DRY position, because
# execute_position deliberately does not promote DRY fills.
_SQL_OPEN_DRY = (
    "SELECT COUNT(*) AS positions,"
    " COALESCE(SUM(quantity * entry_price), 0.0) AS capital"
    " FROM positions WHERE status = 'open' AND live = 0"
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
    " FROM trade_logs WHERE {book} GROUP BY strategy ORDER BY pnl DESC"
)
_SQL_RECENT = (
    "SELECT market_id, side, entry_price, exit_price, quantity, pnl,"
    " exit_timestamp, rationale, strategy FROM trade_logs WHERE {book}"
    " ORDER BY exit_timestamp DESC LIMIT 25"
)
_SQL_EQUITY = (
    "SELECT exit_timestamp, pnl FROM trade_logs WHERE {book}"
    " ORDER BY exit_timestamp DESC LIMIT 200"
)
# Every close, oldest first, so each strategy's own curve can be reconstructed.
# This is the persistent backing for the per-strategy charts: the series is
# rebuilt from the trade log rather than accumulated in memory, so it survives a
# redeploy and grows without a background writer.
_SQL_STRATEGY_CURVE = (
    "SELECT COALESCE(NULLIF(strategy, ''), 'unattributed') AS strategy,"
    " COALESCE(NULLIF(mode, ''), 'dry') AS mode,"
    " exit_timestamp, pnl, market_id FROM trade_logs"
    " ORDER BY COALESCE(exit_timestamp, entry_timestamp) ASC LIMIT 2000"
)
_SQL_POSITIONS = (
    "SELECT market_id, side, entry_price, quantity, strategy, stop_loss_price,"
    " take_profit_price, status, timestamp, mode FROM positions"
    " WHERE status = 'open' AND {book} ORDER BY timestamp DESC"
)


def _row_open_dry(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The simulated book's own open positions - never the real ones."""
    row = rows[0] if rows else {}
    return {
        "positions": int(row.get("positions") or 0),
        "capital": round(float(row.get("capital") or 0.0), 2),
    }


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
                "mode": r.get("mode") or "dry",
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

    Every trade/position figure is scoped to the book the switch is in, so a
    DRY page can only ever quote DRY rows and a LIVE page only LIVE rows.
    """
    book = _current_book_mode()
    where = _book_filter(book)
    batch = _db_many(
        [
            (_SQL_TRADES.format(book=where), ()),
            (_SQL_OPEN.format(book=where), ()),
            (_SQL_OPEN_DRY, ()),
            (_SQL_AI, ()),
            (_SQL_LLM, ()),
            (_SQL_STRATEGY.format(book=where), ()),
            (_SQL_RECENT.format(book=where), ()),
            (_SQL_EQUITY.format(book=where), ()),
            (_SQL_POSITIONS.format(book=where), ()),
            (_SQL_STRATEGY_CURVE, ()),
        ]
        + [(f"SELECT COUNT(*) AS n FROM {t}", ()) for t in DATA_TABLES]
    )
    trades_r, open_r, dry_open_r, ai_r, llm_r, strat_r, recent_r, equity_r, pos_r, curve_r = batch[
        :10
    ]
    counts = batch[10:]

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
    if not positions and account["connected"] and book == "live":
        # Kalshi is the source of truth for the LIVE book's own rows. Falling
        # back to it in DRY put the real account's positions on the DRY screen
        # whenever the simulated book happened to be empty.
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
    cards = _strategy_cards(curve_r, pos_r, book)

    def _count(index: int) -> int:
        """Rows in a count result, tolerating a table that does not exist yet.

        _db_many yields an empty list for a failed query, so a brand-new
        database (no schema at all) must not index blindly.
        """
        rows = counts[index] if index < len(counts) else []
        return int(rows[0].get("n", 0) or 0) if rows else 0

    never_run = _count(0) == 0 and _count(5) == 0 and _count(2) == 0

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
        # Nothing has run if we have never ingested a market or recorded a
        # decision. Shown as an explicit banner instead of a page of zeros.
        "never_run": never_run,
        "trades": _row_trades(trades_r),
        "open": _row_open(open_r),
        "open_dry": _row_open_dry(dry_open_r),
        "data": _row_data(counts, ai_r, llm_r),
        "positions": positions,
        "recent_trades": recent,
        "by_strategy": _row_breakdown(strat_r),
        "equity": {"labels": labels, "pnl": values},
        "bots": _bots_payload(),
        "running_count": len(running),
        "book": book,
        "strategy_cards": cards,
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
        "backups": _backup_status(),
        "market": _market_context(),
        "accounts": {
            _current_book_mode(): _account_payload(_current_book_mode()),
        },
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
            "live": {
                "starting_balance": 0.0,
                "cash": 0.0,
                "deployed": 0.0,
                "open_positions": 0,
                "equity": 0.0,
                "realized": 0.0,
                "total_pnl": 0.0,
                "closed_trades": 0,
                "ledger_entries": 0,
                "return_pct": 0.0,
            },
            "dry_funding": {
                "connected": True,
                "balance": 300.0,
                "can_fund": True,
                "reason": "",
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


def _basic_auth_ok() -> bool:
    """HTTP Basic check against DASHBOARD_USER / DASHBOARD_PASSWORD."""
    user = os.environ.get("DASHBOARD_USER", "").strip()
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not user or not password:
        return False
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return False
    got_user, _, got_pass = decoded.partition(":")
    return secrets.compare_digest(got_user, user) and secrets.compare_digest(got_pass, password)


def require_token():
    """Flask guard for mutating routes. Returns None when allowed, else a response.

    Accepts either the write token (for fetch/XHR from the page) or HTTP Basic
    credentials. Basic auth is what protects the deployment, because the write
    token is embedded in the page and therefore readable by any visitor.
    """
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
    if secrets.compare_digest(_presented_token(), _configured_token()):
        return None
    if _basic_auth_ok():
        return None
    _audit(f"rejected unauthorized {request.method} {request.path} from {request.remote_addr}")
    return jsonify({"error": "Unauthorized: bad or missing X-Auth-Token"}), 401


# ---------------------------------------------------------------------------
# DRY / LIVE trading mode
# ---------------------------------------------------------------------------
def _mode_manager():
    from src.utils.mode import TradingMode

    return TradingMode(db_path=DB_PATH)


def _mode_payload() -> Dict[str, Any]:
    from src.utils.mode import MODE_DRY, MODE_LIVE, run

    mgr = _mode_manager()
    mode = run(mgr.current())
    # Reconcile before reporting, so the DRY headline is derived from the
    # persisted book rather than from a counter that can drift from it.
    drift = run(mgr.reconcile_dry())
    dry = run(mgr.dry_account())
    payload: Dict[str, Any] = {
        "mode": mode,
        "token_set": token_required(),
        "drift": drift,
        # Both books are reported, always scoped to themselves. The page renders
        # the one it is in, but a DRY view that merely *hid* the real account
        # would still be one refactor away from showing it again.
        "dry": dry,
        "live": run(mgr.live_account()),
        "funding": {},
        # The DRY book funds itself out of simulated cash. Reporting the real
        # Kalshi balance here is what made the DRY screen "pose" the live
        # account: the funding card answered with live money under a DRY heading.
        "dry_funding": {
            "connected": True,
            "balance": dry["cash"],
            "can_fund": dry["cash"] >= 1.0,
            "reason": ""
            if dry["cash"] >= 1.0
            else (
                f"DRY cash ${dry['cash']:.2f} is below Kalshi's $1.00 minimum order "
                "size - reset the DRY account to keep rehearsing"
            ),
        },
    }
    # Only the live path needs the network, so only probe it in LIVE mode.
    if mode == MODE_LIVE:
        payload["funding"] = run(mgr.funding(materialize_private_key()))
    else:
        # DRY's payload must not carry the real balance at all. Nothing on the
        # DRY screen reads funding.balance - the funding card reads dry_funding -
        # so shipping it anyway only smuggled a live number onto a DRY page,
        # buried in the snapshot JSON where nobody could see it disappear.
        payload["funding"] = {
            "connected": dashboard_state["has_kalshi_creds"],
            "balance": 0.0,
            "can_fund": False,
            "reason": "",
        }
    assert MODE_DRY  # keeps the import meaningful for readers
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
    # The write token is handed to the template only - never to /api/snapshot,
    # which is an unauthenticated GET. Embedding it lets the page's controls work
    # without a prompt, at the cost of anyone who loads the page being able to
    # write. That is an accepted trade for a single-operator dashboard; drop this
    # line and the controls fall back to prompting.
    snap["token"] = _configured_token()
    # Context for the live-feed row. Best-effort by design: a feed that has not
    # connected yet must still render a page, with its own honest empty state.
    context = _market_context()
    acct = _account_payload(_current_book_mode())
    return render_template_string(
        _TEMPLATE,
        s=snap,
        m={
            "spot": context["spot"],
            "kalshi": context["kalshi"],
            "series": context["series"],
        },
        lead=context["lead"],
        acct=acct,
    )


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
    _recorded_state()
    return jsonify(strategy_state)


# Keep strong references to spawned processes so Python's GC never reaps them,
# and always route child output to a file. Pipes would fill after ~64KB and
# deadlock the trading loop, since nothing drains them.
_child_procs: Dict[int, "subprocess.Popen[Any]"] = {}


def _stop_child(st):
    """Stop a running strategy process: SIGTERM, then SIGKILL if it lingers.

    Returns the child's exit code when the OS will report it, so the page can
    say what happened rather than just that it stopped.
    """
    pid = st.get("pid")
    if not pid:
        return None
    proc = _child_procs.get(pid)
    code: Optional[int] = None
    if proc is not None:
        # Preferred path: the Popen object works identically on every platform.
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            code = getattr(proc, "returncode", None)
        except (OSError, ValueError):
            pass
        _child_procs.pop(pid, None)
    else:
        # No Popen reference (the process was started by another worker, or the
        # dashboard restarted). Fall back to signals. SIGKILL does not exist on
        # Windows, and on Windows signalling anything but CTRL_* terminates the
        # target outright - which is what we want here anyway.
        import signal

        for sig in (getattr(signal, "SIGTERM", 15), getattr(signal, "SIGKILL", 9)):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                break
            from src.utils.strategy_runtime import pid_alive as safe_pid_alive

            for _ in range(10):
                if not safe_pid_alive(pid):
                    break
                time.sleep(0.2)
            else:
                continue
            break
    st["running"] = False
    st["pid"] = None
    return code


def _runtime_store():
    from src.utils.strategy_runtime import StrategyRuntime

    return StrategyRuntime(db_path=str(DB_PATH))


def _recorded_state() -> Dict[str, Dict[str, Any]]:
    """Persisted strategy state, reconciled against the OS.

    This is the source of truth for the Start/Stop pills. In-memory
    `strategy_state` alone produced a page that said "stopped" for strategies
    that were running, because the flag was written by whichever request started
    the process and never learned what happened afterwards.
    """
    recorded = _run_async(_runtime_store().snapshot())
    for name, st in strategy_state.items():
        row = recorded.get(name) or {}
        pid = row.get("pid")
        if pid and not _pid_alive(pid):
            # Died without us being told. Say so instead of implying it never ran.
            _run_async(_runtime_store().record_stop(name, "exited on its own"))
            st["pid"] = None
            st["running"] = False
            st["stop_reason"] = "exited on its own"
            continue
        st["pid"] = pid if pid else None
        st["running"] = bool(pid)
        st["started_at"] = row.get("started_at")
        st["command"] = row.get("command")
        st["stop_reason"] = row.get("stop_reason") or ""
    return cast(Dict[str, Dict[str, Any]], recorded)


def _pid_alive(pid):
    """True if a strategy process is still running.

    Delegates to the runtime store so the check is platform-safe: on Windows
    os.kill(pid, 0) would terminate the process being probed, and the Popen
    handle only exists in the process that spawned it.
    """
    from src.utils.strategy_runtime import pid_alive as safe_pid_alive

    if not pid:
        return False
    proc = _child_procs.get(pid)
    if proc is not None:
        # Authoritative when we own the handle.
        return proc.poll() is None
    return safe_pid_alive(pid)


def _running_strategies():
    """Names of strategies whose process is actually alive."""
    _recorded_state()
    return [name for name, st in strategy_state.items() if st.get("running")]


@app.route("/api/strategy/<name>", methods=["GET"])
def api_strategy_detail(name):
    """Everything known about one strategy: its card, its book, its own closes.

    Read-only and unauthenticated, like the rest of the read surface. The card
    grid on the page is rendered from the same builder, so a card and its detail
    view can never disagree about a number.
    """
    if name not in strategy_state:
        return jsonify({"error": f"Unknown strategy: {name}"}), 404

    book = _current_book_mode()
    where = _book_filter(book)
    batch = _db_many(
        [
            (_SQL_STRATEGY_CURVE, ()),
            (_SQL_POSITIONS.format(book=where), ()),
            (_SQL_RECENT.format(book=where), ()),
            # Not filtered in SQL: `strategy` is written by each strategy under
            # its own name (quick_flip_scalping, directional_trading, ...), so an
            # equality filter against the button name would match nothing. The
            # alias map is applied in Python instead.
            (
                "SELECT market_id, side, entry_price, exit_price, quantity, pnl,"
                " exit_timestamp, rationale, strategy, mode FROM trade_logs"
                f" WHERE {where}"
                " ORDER BY COALESCE(exit_timestamp, entry_timestamp) DESC LIMIT 400",
                (),
            ),
        ]
    )
    curve_r, pos_r, _recent_r, trades_r = batch

    cards = _strategy_cards(curve_r, pos_r, book)
    card = next((c for c in cards if c["name"] == name), None)
    if card is None:
        return jsonify({"error": f"Unknown strategy: {name}"}), 404

    def _mine(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [r for r in rows if _strategy_bucket(r.get("strategy")) == name]

    def _in_book(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [r for r in rows if _resolve_book(r.get("mode")) == book]

    closed = _in_book(_mine(trades_r))[:50]
    for r in closed:
        r["pnl"] = round(float(r.get("pnl") or 0.0), 2)
        r["exit_timestamp"] = str(r.get("exit_timestamp") or "")[:19]

    return jsonify(
        {
            "strategy": card,
            "book": book,
            "positions": [
                {
                    "market_id": r.get("market_id"),
                    "side": r.get("side"),
                    "entry_price": r.get("entry_price"),
                    "quantity": r.get("quantity"),
                    "stop_loss": r.get("stop_loss_price"),
                    "take_profit": r.get("take_profit_price"),
                    "opened": str(r.get("timestamp") or "")[:19],
                }
                for r in _in_book(_mine(pos_r))
            ],
            "trades": closed,
            "logs": _strategy_log_tail(name),
        }
    )


def _strategy_log_tail(name: str, lines: int = 60) -> List[str]:
    """Tail the log this strategy's own process writes."""
    path = LOG_DIR / f"strategy_{name}.log"
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 16384))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    tail = [ln for ln in text.splitlines() if ln.strip()]
    return tail[-lines:]


@app.route("/api/strategy/<name>/toggle", methods=["POST"])
def api_strategy_toggle(name):
    """Start/stop a strategy subprocess."""
    denied = require_token()
    if denied is not None:
        return denied
    if name not in strategy_state:
        return jsonify({"error": f"Unknown strategy: {name}"}), 404

    st = strategy_state[name]
    # Get the current persisted book mode (dry/live). This is the SOURCE OF TRUTH
    # for which book we are in. The request MUST match it — a button pressed while
    # the switch reads DRY cannot spawn a LIVE process, and vice versa.
    persisted_mode = _current_book_mode()
    requested_mode = (request.json or {}).get("mode")

    # If the caller explicitly provides a mode, it MUST match the persisted mode.
    # If they don't provide one, we use the persisted mode. Any mismatch = 400.
    if requested_mode is not None:
        if requested_mode != persisted_mode:
            return jsonify(
                {
                    "error": f"Mode mismatch: request asked for '{requested_mode}' but the current book is '{persisted_mode}'. Cannot start a strategy in a different book."
                }
            ), 400
        mode = requested_mode
    else:
        mode = "live" if persisted_mode == "live" else "paper"

    if mode not in ("paper", "live"):
        return jsonify({"error": "mode must be 'paper' or 'live'"}), 400
    if mode == "live" and not os.environ.get("KALSHI_API_KEY"):
        return jsonify({"error": "Live mode needs KALSHI_API_KEY configured"}), 400

    store = _runtime_store()
    if st.get("running"):
        code = _stop_child(st)
        _run_async(store.record_stop(name, "stopped by operator"))
        _recorded_state()
        _broadcast("strategy", {"name": name, "action": "stopped"})
        return jsonify({"name": name, "running": False, "exit_code": code})

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
    cmd = [py, *STRATEGY_COMMANDS.get(name, STRATEGY_COMMANDS["ai_directional"])]
    if mode == "live":
        # The command tables are written in paper; LIVE is the same process with
        # the real-money flag. should_trade_live() inside the child re-checks the
        # persisted mode, so the switch - not this flag - is the money gate.
        cmd = [c if c != "--paper" else "--live" for c in cmd]

    # The child needs a private key it can actually open. Railway injects
    # KALSHI_PRIVATE_KEY as PEM *text*, but KalshiClient loads a *path*, so
    # without this every spawned strategy died on "Private key file not found"
    # the moment it started - which is why the buttons always read "stopped".
    child_env = os.environ.copy()
    key_path = materialize_private_key()
    if key_path:
        child_env["KALSHI_PRIVATE_KEY_PATH"] = key_path
    # Children must share the volume-mounted database, not a fresh container one.
    child_env["DB_PATH"] = str(DB_PATH)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        out = open(LOG_DIR / f"strategy_{name}.log", "ab", buffering=0)
        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=child_env,
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
    # Persist before answering: the next request may be served by a different
    # process, and a button that reports "started" for a process nobody can see
    # is exactly the lie this store exists to remove.
    _run_async(store.record_start(name, proc.pid, mode, " ".join(cmd[1:])))
    _broadcast("strategy", {"name": name, "action": "started", "pid": proc.pid})
    return jsonify({"name": name, "running": True, "pid": proc.pid})


# 4. Charting data (P&L)
_SQL_ACCOUNT_CURVE = (
    "SELECT date(COALESCE(exit_timestamp, entry_timestamp)) AS day,"
    " COALESCE(SUM(pnl), 0.0) AS pnl, COUNT(*) AS trades"
    " FROM trade_logs WHERE {book} GROUP BY day ORDER BY day ASC LIMIT 90"
)


def _account_payload(mode: str) -> Dict[str, Any]:
    """One account card's worth of data: today's curve plus trader-standard stats.

    The curve is aggregated from `trade_logs` by day and rebuilt from the
    database on every call, so it is persistent across restarts rather than an
    in-memory buffer that a redeploy would erase. In DRY the book filter selects
    simulated rows only, which is what keeps the DRY card honest.
    """
    from src.utils.mode import MODE_DRY

    dry_book = mode == MODE_DRY
    where = (
        "COALESCE(NULLIF(mode, ''), 'dry') = 'dry'"
        if dry_book
        else ("COALESCE(NULLIF(mode, ''), 'dry') = 'live'")
    )
    curve_sql = _SQL_ACCOUNT_CURVE.format(book=where)
    today_sql = (
        "SELECT COALESCE(SUM(pnl), 0.0) AS pnl, COUNT(*) AS trades,"
        " COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins"
        f" FROM trade_logs WHERE {where} AND date(COALESCE(exit_timestamp,"
        " entry_timestamp)) = date('now')"
    )
    curve_r, today_r = _db_many([(curve_sql, ()), (today_sql, ())])
    today = today_r[0] if today_r else {}

    running_pnl = 0.0
    labels: List[str] = []
    values: List[float] = []
    for row in curve_r:
        running_pnl += float(row.get("pnl") or 0.0)
        labels.append(str(row.get("day") or ""))
        values.append(round(running_pnl, 2))

    today_pnl = round(float(today.get("pnl") or 0.0), 2)
    trades_today = int(today.get("trades") or 0)
    wins_today = int(today.get("wins") or 0)

    if dry_book:
        from src.utils.mode import run as mode_run

        book = mode_run(_mode_manager().dry_account())
        cash, equity = book["cash"], book["equity"]
        starting = book["starting_balance"]
        realized = book["realized"]
        exposure = book["deployed"]
        open_positions = book["open_positions"]
        closed = book["closed_trades"]
        wins = max(wins_today, 0)
    else:
        account = _kalshi_account()
        cash = float(dashboard_state.get("balance") or 0.0)  # type: ignore[arg-type]
        exposure = account["exposure"]
        equity = round(cash + exposure, 2)
        starting = 0.0
        realized = round(float(today.get("pnl") or 0.0), 2)
        open_positions = account["market_count"] + account["event_count"]
        closed = int(today.get("trades") or 0)
        wins = wins_today

    return {
        "book": "dry" if dry_book else "live",
        "label": "DRY account (simulated)" if dry_book else "LIVE Kalshi account",
        "cash": round(cash, 2),
        "equity": round(equity, 2),
        "starting_balance": round(starting, 2),
        "return_pct": round(100.0 * (equity - starting) / starting, 2) if starting else 0.0,
        "exposure": round(exposure, 2),
        "open_positions": open_positions,
        "realized_all_time": round(realized, 2),
        "today": {
            "pnl": today_pnl,
            "trades": trades_today,
            "wins": wins_today,
            "losses": max(trades_today - wins_today, 0),
            "win_rate": round(100.0 * wins_today / trades_today, 1) if trades_today else 0.0,
            "avg": round(today_pnl / trades_today, 2) if trades_today else 0.0,
        },
        "closed_trades": closed,
        "curve": {"labels": labels, "pnl": values},
    }


@app.route("/api/accounts", methods=["GET"])
def api_accounts():
    """Both account cards. Always both books, each scoped to itself."""
    mode = _current_book_mode()
    other = "live" if mode == "dry" else "dry"
    return jsonify(
        {
            "current": mode,
            "accounts": {mode: _account_payload(mode), other: _account_payload(other)},
        }
    )


def _market_context() -> Dict[str, Any]:
    """Live-feed context for the page, complete even before the socket connects.

    Every key the template reads is always present. A partial dict made Jinja
    raise on `lead.delta` during the first seconds after boot, which turned the
    whole page into a 500 instead of a page with a waiting feed.
    """
    market = cast(Dict[str, Any], dashboard_state.get("market") or {})
    lead = market.get("lead") or {}
    return {
        "spot": market.get("spot", {}),
        "kalshi": market.get("kalshi", {}),
        "series": market.get("series", []),
        "lead": {
            "spot": lead.get("spot"),
            "target": lead.get("target"),
            "spot_vs_target": lead.get("spot_vs_target"),
            "up_price": lead.get("up_price"),
            "down_price": lead.get("down_price"),
            "seconds_left": lead.get("seconds_left"),
            "series": (market.get("kalshi", {}) or {}).get("series"),
            "usable": bool(lead.get("usable")),
            "measured_at": lead.get("measured_at"),
            "note": lead.get("note") or "Reading the live 15-minute contract.",
        },
    }


@app.route("/api/marketdata", methods=["GET"])
def api_marketdata():
    """Live BTC spot, the Kalshi ladder quote, and the measured lead between them."""
    data = dashboard_state.get("market") or {}
    if not data:
        return jsonify({"error": "market data feed is still starting"}), 503
    return jsonify(data)


@app.route("/api/chart/pnl")
def api_chart_pnl():
    """Return P&L data for Chart.js, scoped to the current book.

    Unused by the current page (the equity chart reads the snapshot), but kept
    honest anyway: an endpoint that answered with both books mixed was a leak
    waiting for the next refactor to pick it up.
    """
    try:
        db = _db()
        perf = _run_async(db.get_performance_by_strategy())

        labels = []
        pnl_data = []
        cumulative = 0.0
        try:
            import aiosqlite

            where = _book_filter(_current_book_mode())

            async def _read_trades():
                async with aiosqlite.connect(db.db_path) as conn:
                    cur = await conn.execute(
                        "SELECT exit_timestamp, pnl FROM trade_logs "
                        f"WHERE {where} ORDER BY exit_timestamp ASC LIMIT 200"
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
<title>Kalshi-Frigo — {{ 'LIVE' if s.mode.mode == 'live' else 'DRY' }} Trading Dashboard</title>
<!-- Favicon colour tracks the mode, so the tab itself shows which one is active. -->
<link rel="icon" id="favicon" href="data:image/svg+xml,{{ '<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 32 32%22><rect width=%2232%22 height=%2232%22 rx=%228%22 fill=%22%23ff5c7a%22/><text x=%2216%22 y=%2223%22 font-size=%2219%22 font-weight=%22bold%22 text-anchor=%22middle%22 fill=%22%23000%22>L</text></svg>' if s.mode.mode == 'live' else '<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 32 32%22><rect width=%2232%22 height=%2232%22 rx=%228%22 fill=%22%232ee6a8%22/><text x=%2216%22 y=%2223%22 font-size=%2219%22 font-weight=%22bold%22 text-anchor=%22middle%22 fill=%22%23000%22>D</text></svg>' }}">
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

/* ---------- Mode is a whole-page state, not a badge you have to find ----------
   A 5px frame around the entire viewport plus a pulsing glow. Green = simulated
   money only. Red and animating = real orders leaving the account. This has to
   be impossible to mistake, because the earlier behaviour (click LIVE, get
   refused, sit in DRY) was indistinguishable from actually being LIVE. */
body::before{
  content:"";position:fixed;inset:0;z-index:9999;pointer-events:none;
  border:5px solid var(--mode-color, var(--up));
  box-shadow:inset 0 0 22px var(--mode-glow, rgba(46,230,168,.30));
  transition:border-color .18s ease, box-shadow .18s ease;
}
body[data-mode="dry"]{--mode-color:var(--up);--mode-glow:rgba(46,230,168,.32)}
body[data-mode="live"]{
  --mode-color:var(--down);--mode-glow:rgba(255,92,122,.55);
  animation:livepulse 1.4s ease-in-out infinite;
}
@keyframes livepulse{
  0%,100%{box-shadow:inset 0 0 22px rgba(255,92,122,.55)}
  50%    {box-shadow:inset 0 0 46px rgba(255,92,122,1)}
}
/* Redundant, non-colour signal so the state survives colour-blindness. */
body[data-mode="live"] .brand .logo{background:linear-gradient(145deg,#ff2d55,#ff5c7a)}
body[data-mode="dry"]  .brand .logo{background:linear-gradient(145deg,#2b6cff,#8b5cf6)}

.modeflag{
  display:inline-flex;align-items:center;gap:8px;
  font-size:13px;font-weight:800;letter-spacing:.14em;
  padding:5px 14px;border-radius:9px;border:2px solid currentColor;
}
.modeflag.dry {color:var(--up);background:rgba(46,230,168,.10)}
.modeflag.live{color:var(--down);background:rgba(255,92,122,.14)}
.modeflag .dot{width:9px;height:9px}
body[data-mode="live"] .modeflag.live{animation:blink 1.1s steps(1) infinite}
@keyframes blink{0%,60%{opacity:1}61%,100%{opacity:.25}}
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
/* strategy cards: one per strategy, each with its own persistent curve */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(258px,1fr));gap:10px}
.row.three{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px}
.card{
  background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:11px 12px;
  cursor:pointer;transition:border-color .15s,transform .15s;
}
.card:hover{border-color:var(--blue);transform:translateY(-1px)}
.card.hot{border-color:rgba(46,230,168,.45)}
.ctop{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.clabel{font-size:13px;font-weight:650}
.cname{font-size:10.5px;color:var(--faint);margin-top:1px}
.cchart{height:54px;margin:8px 0 6px}
.cstats{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;text-align:center}
.cstats b{display:block;font-size:13px;font-weight:640}
.cstats span{font-size:9.5px;color:var(--faint);text-transform:uppercase;letter-spacing:.04em}
.cfoot{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:9px;padding-top:8px;border-top:1px solid var(--line)}
.cfoot button{padding:3px 9px;font-size:11px}
code{background:var(--bg);border:1px solid var(--line2);border-radius:5px;padding:1px 5px;font-size:11px;font-family:ui-monospace,monospace}
footer{margin-top:22px;text-align:center;color:var(--faint);font-size:11px}
</style>
</head>
<body data-mode="{{ s.mode.mode }}">
<div class="wrap">

<header>
  <div class="brand">
    <div class="logo">&#129504;</div>
    <div>
      <h1>Kalshi-Frigo <span>&middot; {{ 'LIVE' if s.mode.mode == 'live' else 'DRY' }} trading dashboard</span></h1>
      <div class="sub">LLM-driven Kalshi automation &middot; paper &amp; live &middot; multi-strategy</div>
    </div>
  </div>
  <div class="headright">
    <!-- Unmistakable state flag: text, colour and a blinking dot. -->
    <div id="modeFlag" class="modeflag {{ 'live' if s.mode.mode == 'live' else 'dry' }}">
      <span class="dot"></span>
      <span id="modeFlagText">{{ 'LIVE MODE' if s.mode.mode == 'live' else 'DRY MODE' }}</span>
    </div>
    <!-- DRY / LIVE switch -->
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
  Every fill below is a real Kalshi trade against the production account.
  Account balance: <b>{{ '$%.2f'|format(s.mode.funding.get('balance', 0) or 0) }}</b>.
  Every strategy started from here will place real orders until you switch back to DRY.
</div>
{% endif %}

{% if s.never_run %}
<div class="note-box" style="margin-bottom:14px">
  <b>The pipeline has never run.</b>
  {{ s.data.tables.markets }} markets ingested, {{ s.data.llm_queries }} LLM calls,
  {{ s.trades.trades }} closed trades &mdash; every figure on this page is a default,
  not a measurement. Start a strategy below to exercise ingest &rarr; decide &rarr;
  execute &rarr; track. In DRY it will rehearse against real prices without spending money.
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

<!-- ============ headline tiles ============ -->
{% set live = s.mode.mode == 'live' %}
{% set dry = s.mode.dry %}
<!-- Headline tiles describe the book the bot is ACTUALLY trading.
     DRY -> the simulated account. LIVE -> the real Kalshi account.
     Never the other one: in DRY the real balance and real positions sitting in
     the headline made the page contradict its own DRY MODE flag. -->
<div class="tiles">
  <div class="tile">
    <div class="k">Engine</div>
    <div class="v">{% if s.status == 'online' %}<span class="pill ok"><span class="dot pulse"></span>ONLINE</span>{% else %}<span class="pill no">OFF</span>{% endif %}</div>
    <div class="s" id="tUptime">up {{ (s.uptime_sec // 60) if s.uptime_sec is defined else 0 }}m</div>
  </div>

  {% if live %}
  <div class="tile">
    <div class="k">Real balance</div>
    <div class="v" id="tBalance">{{ '$%.2f'|format(s.balance) if s.balance is not none else '-' }}</div>
    <div class="s">live Kalshi account</div>
  </div>
  <div class="tile">
    <div class="k">Live positions</div>
    <div class="v" id="tLivePos">{{ (s.kalshi.market_count + s.kalshi.event_count) if s.kalshi else 0 }}</div>
    <div class="s">{{ s.kalshi.market_count if s.kalshi else 0 }} market &middot; {{ s.kalshi.event_count if s.kalshi else 0 }} event</div>
  </div>
  <div class="tile">
    <div class="k">Real exposure</div>
    <div class="v" id="tExposure">{{ '$%.2f'|format(s.kalshi.exposure) if s.kalshi else '$0.00' }}</div>
    <div class="s">cost basis {{ '$%.2f'|format(s.kalshi.cost_basis) if s.kalshi else '$0.00' }}</div>
  </div>
  <div class="tile">
    <div class="k">Real realized P&amp;L</div>
    <div class="v {{ 'up' if s.kalshi and s.kalshi.realized > 0 else ('down' if s.kalshi and s.kalshi.realized < 0 else 'flat') }}" id="tKalshiPnl">{{ '$%.2f'|format(s.kalshi.realized) if s.kalshi else '$0.00' }}</div>
    <div class="s">fees {{ '$%.2f'|format(s.kalshi.fees) if s.kalshi else '$0.00' }}</div>
  </div>
  {% else %}
  <div class="tile">
    <div class="k">DRY cash</div>
    <div class="v" id="dCashTile">{{ '$%.2f'|format(dry.cash) }}</div>
    <div class="s">simulated &middot; no real money</div>
  </div>
  <div class="tile">
    <div class="k">DRY positions</div>
    <div class="v" id="dPosTile">{{ s.open_dry.positions if s.open_dry else 0 }}</div>
    <div class="s">simulated fills</div>
  </div>
  <div class="tile">
    <div class="k">DRY deployed</div>
    <div class="v" id="dDepTile">{{ '$%.2f'|format(s.open_dry.capital) if s.open_dry else '$0.00' }}</div>
    <div class="s">of {{ '$%.2f'|format(dry.equity) }} equity</div>
  </div>
  <div class="tile">
    <div class="k">DRY realized P&amp;L</div>
    <div class="v {{ 'up' if dry.realized > 0 else ('down' if dry.realized < 0 else 'flat') }}" id="dRealTile">{{ '$%.2f'|format(dry.total_pnl) }}</div>
    <div class="s">{{ dry.ledger_entries }} simulated fill{{ '' if dry.ledger_entries == 1 else 's' }}</div>
  </div>
  {% endif %}

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

<!-- ============ strategy cards ============ -->
<div class="panel" style="margin-bottom:12px">
  <div class="ph">
    <h2>Strategies</h2>
    <span class="note">click a card for everything about that strategy</span>
    <span class="bar"><button id="startAllBtn" onclick="startAll()">Start all in DRY</button><button onclick="stopAll()">Stop all</button></span>
  </div>
  <div class="pb">
    <div class="cards">
      {%- for c in s.strategy_cards %}
      <div class="card{{ ' hot' if c.running else '' }}" id="card-{{ c.name }}" onclick="openStrategy('{{ c.name }}')">
        <div class="ctop">
          <div>
            <div class="clabel">{{ c.label }}</div>
            <div class="mono cname">{{ c.name }}</div>
          </div>
          <span class="pill {{ 'ok' if c.running else 'no' }}" id="pill-{{ c.name }}">{% if c.running %}<span class="dot pulse"></span>running{% elif c.stop_reason %}stopped{% else %}not started{% endif %}</span>
        </div>
        <div class="cchart"><canvas id="spark-{{ c.name }}" height="54"></canvas></div>
        <div class="cstats">
          <div><b id="c-{{ c.name }}-trades">{{ c.trades }}</b><span>trades</span></div>
          <div><b id="c-{{ c.name }}-pnl" class="{{ 'up' if c.realized > 0 else ('down' if c.realized < 0 else 'flat') }}">${{ '%.2f'|format(c.realized) }}</b><span>realized</span></div>
          <div><b id="c-{{ c.name }}-win">{{ c.win_rate }}%</b><span>win rate</span></div>
          <div><b id="c-{{ c.name }}-open">{{ c.open_positions }}</b><span>open</span></div>
        </div>
        <div class="cfoot">
          <span class="mono" style="color:var(--faint)">$<span id="c-{{ c.name }}-dep">{{ '%.2f'|format(c.deployed) }}</span> deployed{% if not c.running and c.stop_reason %} &middot; {{ c.stop_reason }}{% endif %}</span>
          <span class="bar" onclick="event.stopPropagation()">
            <button id="btn-{{ c.name }}" onclick="toggleStrategy('{{ c.name }}')">{% if c.running %}Stop{% else %}Start{% endif %}</button>
            <button class="danger" onclick="killBot('{{ c.name }}')">Kill</button>
          </span>
        </div>
      </div>
      {%- endfor %}
    </div>
    <p style="color:var(--faint);font-size:11.5px;margin-top:10px">
      Curves are rebuilt from <code>trade_logs</code>, so a strategy keeps its history across restarts and deploys.
      Cards show the <b>{{ s.book|upper }}</b> book only.
    </p>
  </div>
</div>

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
    <div class="ph">
      <h2>Funding source</h2>
      <span class="note">{{ 'real Kalshi account' if live else 'DRY account &middot; simulated cash' }}</span>
    </div>
    <div class="pb">
      <dl class="kv">
        {% if live %}
        <dt>API connection</dt>
        <dd>{% if s.mode.funding.get('connected') %}<span class="pill ok">connected</span>{% else %}<span class="pill no">offline</span>{% endif %}</dd>
        <dt>Available balance</dt><dd id="fBalance">{{ '$%.2f'|format(s.mode.funding.get('balance', 0) or 0) }}</dd>
        <dt>Can fund an order</dt>
        <dd>{% if s.mode.funding.get('can_fund') %}<span class="pill ok">yes</span>{% else %}<span class="pill no">no</span>{% endif %}</dd>
        {% else %}
        <dt>Source</dt>
        <dd><span class="pill ok">simulated ledger</span></dd>
        <dt>DRY cash available</dt><dd id="fBalance">{{ '$%.2f'|format(s.mode.dry_funding.get('balance', 0) or 0) }}</dd>
        <dt>Can fund an order</dt>
        <dd>{% if s.mode.dry_funding.get('can_fund') %}<span class="pill ok">yes</span>{% else %}<span class="pill no">no</span>{% endif %}</dd>
        {% endif %}
      </dl>
      {% if not live and s.mode.dry_funding.get('reason') %}
      <div class="note-box" style="margin-top:12px"><b>DRY cannot place orders:</b> {{ s.mode.dry_funding.get('reason') }}</div>
      {% elif live and s.mode.funding.get('reason') %}
      <div class="note-box" style="margin-top:12px"><b>Cannot trade LIVE:</b> {{ s.mode.funding.get('reason') }}</div>
      {% elif not live %}
      <p class="note" style="margin-top:10px;color:var(--faint);font-size:11px">
        This is the DRY book's own money: the simulated ledger that starts at
        {{ '$%.2f'|format(s.mode.dry.starting_balance) }}. The real Kalshi balance is
        in the <b>Real Kalshi account</b> panel below and is never spent while the
        switch reads DRY.
      </p>
      {% endif %}
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

<!-- ============ live feeds + account ============ -->
<div class="panel" style="margin-bottom:12px">
  <div class="ph">
    <h2>Live feeds &amp; account</h2>
    <span class="note" id="feedNote">streaming</span>
  </div>
  <div class="pb">
    <div class="row three">
      <div class="card" style="cursor:default">
        <div class="ctop">
          <div><div class="clabel">BTC spot</div><div class="mono cname" id="spotSource">connecting...</div></div>
          <span class="pill" id="spotPill">--</span>
        </div>
        <div class="cchart" style="height:130px"><canvas id="spotChart"></canvas></div>
        <div class="cfoot"><span class="mono" style="color:var(--faint)" id="spotAge">no tick yet</span></div>
      </div>

      <div class="card" style="cursor:default">
        <div class="ctop">
          <div>
            <div class="clabel">Kalshi BTC 15-min</div>
            <div class="mono cname" id="k15Ticker">{{ (m.kalshi.market.ticker if m.kalshi.get('market') else 'KXBTC15M') }}</div>
          </div>
          <span class="pill" id="k15Pill">--</span>
        </div>
        <div class="cchart" style="height:130px"><canvas id="k15Chart"></canvas></div>
        <div class="cstats" style="grid-template-columns:repeat(2,1fr);gap:4px 10px">
          <div><b id="k15Up">--</b><span>UP ask</span></div>
          <div><b id="k15Down">--</b><span>DOWN ask</span></div>
          <div><b id="k15Target">--</b><span>target</span></div>
          <div><b id="k15Clock">--</b><span>settles in</span></div>
        </div>
        <div class="cfoot">
          <span class="mono" style="color:var(--faint)" id="k15Detail">waiting for the next contract</span>
        </div>
      </div>

      <div class="card" style="cursor:default" id="accountCard">
        <div class="ctop">
          <div><div class="clabel" id="acctLabel">{{ acct.label }}</div><div class="mono cname">{{ acct.book }} book</div></div>
          <span class="pill {{ 'ok' if acct.book == 'dry' else 'warn' }}" id="acctPill">{{ acct.book|upper }}</span>
        </div>
        <div class="cchart" style="height:130px"><canvas id="accountChart"></canvas></div>
        <div class="cstats">
          <div><b id="acctEquity">{{ '$%.2f'|format(acct.equity) }}</b><span>equity</span></div>
          <div><b id="acctToday" class="{{ 'up' if acct.today.pnl > 0 else ('down' if acct.today.pnl < 0 else 'flat') }}">{{ '$%.2f'|format(acct.today.pnl) }}</b><span>today</span></div>
          <div><b id="acctWin">{{ acct.today.win_rate }}%</b><span>today WR</span></div>
          <div><b id="acctOpen">{{ acct.open_positions }}</b><span>open</span></div>
        </div>
        <div class="cfoot">
          <span class="mono" style="color:var(--faint)" id="acctFoot">cash {{ '$%.2f'|format(acct.cash) }} &middot; {{ acct.today.trades }} trades today</span>
        </div>
      </div>
    </div>
    <p style="color:var(--faint);font-size:11.5px;margin-top:10px">
      {{ lead.note }}
      The contract resolves on the 60-second average of <b>CF Benchmarks BRTI</b> against the
      previous window, so live spot is a <b>proxy</b> for the settlement value, not the value itself.
      Spot vs target right now:
      <b class="mono">{{ ('%+.2f'|format(lead.spot_vs_target)) if lead.spot_vs_target is not none else '--' }}</b>
    </p>
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
        <dt>Database backups</dt><dd id="dBackups">{{ s.backups.count }} (every 6h, keeps {{ s.backups.keep }})</dd>
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
      <h2>{{ 'Kalshi account — real, and being traded' if s.mode.mode == 'live' else 'Real Kalshi account — NOT what DRY is trading' }}</h2>
      <span class="note">{% if s.last_update %}synced {{ s.last_update }}{% else %}not synced yet{% endif %}</span>
    </div>
    {%- if s.mode.mode != 'live' %}
    <div class="pb" style="padding-bottom:0">
      <p class="note" style="font-size:11.5px;color:var(--faint)">
        Read-only reference, and <b>managed outside this bot</b>. This deployment
        does not trade it: the strategy loop has never placed an order here, and
        the book is driven by a separate system. The simulated $300 book above is
        what DRY is trading, so none of these figures describe the bot's results.
      </p>
    </div>
    {%- endif %}
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

<!-- ============ strategy detail ============ -->
<div class="panel" id="detailPanel" style="margin-bottom:12px;display:none">
  <div class="ph">
    <h2 id="detailTitle">Strategy</h2>
    <span class="note" id="detailState"></span>
    <span class="bar"><button onclick="closeStrategy()">Close</button></span>
  </div>
  <div class="pb" id="detailBody"></div>
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
SNAPSHOT.mode = SNAPSHOT.mode || { mode: 'dry' };
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
  const money = v => '$' + Number(v || 0).toFixed(2);

  // The headline tiles are mode-specific markup, and each set of ids is written
  // from its OWN book. The DRY and LIVE tiles previously shared element ids, so
  // this line wrote the real Kalshi balance, position count, exposure and
  // realized P&L into the DRY tiles ten seconds after load - the page
  // contradicting its own DRY MODE flag. Live ids do not exist in DRY and vice
  // versa, so each write is a no-op in the other mode rather than a leak.
  if (s.mode && s.mode.mode === 'live') {
    set('tBalance', s.balance == null ? '-' : money(s.balance));
    set('tLivePos', (k.market_count || 0) + (k.event_count || 0));
    set('tExposure', money(k.exposure));
    set('tKalshiPnl', money(k.realized));
    const kr = document.getElementById('tKalshiPnl');
    if (kr) kr.className = 'v ' + sgn(k.realized || 0);
  } else {
    const dry = (s.mode && s.mode.dry) || {}, od = s.open_dry || {};
    set('dCashTile', money(dry.cash));
    set('dPosTile', od.positions || 0);
    set('dDepTile', money(od.capital));
    set('dRealTile', money(dry.total_pnl));
    const dr = document.getElementById('dRealTile');
    if (dr) dr.className = 'v ' + sgn(dry.total_pnl || 0);
  }

  const t = s.trades || {}, d = s.data || {};
  set('tBotPnl', money(t.realized_pnl));
  const br = document.getElementById('tBotPnl');
  if (br) br.className = 'v ' + sgn(t.realized_pnl || 0);
  set('tWinRate', (t.win_rate || 0) + '%');
  set('tAiSpend', money(d.ai_cost_today));
  set('tRunning', s.running_count || 0);
  set('tUptime', Math.floor((s.uptime_sec || 0) / 60) + 'm');

  SNAPSHOT.equity = s.equity || SNAPSHOT.equity;
  paintCards(s.strategy_cards);
  const eq = SNAPSHOT.equity || {};
  const closes = (eq.pnl || []).length;
  set('pnlSummary', closes
    ? closes + ' closed trades'
    : 'no closed trades yet');
}

// Patch the volatile parts of each card in place. The cards themselves are
// server-rendered, so this only has to keep numbers and state current - it must
// not rebuild the DOM, or the sparkline canvases would be destroyed.
function paintCards(cards) {
  if (!cards) return;
  SNAPSHOT.strategy_cards = cards;
  cards.forEach(c => {
    const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    set('c-' + c.name + '-trades', c.trades);
    set('c-' + c.name + '-win', c.win_rate + '%');
    set('c-' + c.name + '-open', c.open_positions);
    set('c-' + c.name + '-dep', Number(c.deployed || 0).toFixed(2));
    const pnl = $('c-' + c.name + '-pnl');
    if (pnl) {
      pnl.textContent = money(c.realized);
      pnl.className = c.realized > 0 ? 'up' : (c.realized < 0 ? 'down' : 'flat');
    }
    const card = $('card-' + c.name);
    if (card) card.classList.toggle('hot', !!c.running);
    const pill = $('pill-' + c.name);
    if (pill) {
      pill.className = 'pill ' + (c.running ? 'ok' : 'no');
      pill.innerHTML = c.running
        ? '<span class="dot pulse"></span>running'
        : (c.stop_reason ? 'stopped' : 'not started');
    }
    const btn = $('btn-' + c.name);
    if (btn) btn.textContent = c.running ? 'Stop' : 'Start';
  });
  drawSparks(cards);
}

async function refresh() {
  try {
    paint(await fetch('/api/snapshot').then(r => r.json()));
    drawChart();
    // Keep an open detail view current without yanking the page around.
    if (openName) openStrategy(openName, true);
    try { paintMode(await fetch('/api/mode').then(r => r.json())); } catch (_) {}
  } catch (e) {
    note('snapshot refresh failed: ' + e.message);
  }
}

// --- DRY / LIVE mode -----------------------------------------------------
// The write token is embedded server-side so the controls work without a
// prompt. WARNING: this dashboard is on a public hostname, so anyone who can
// load the page can also read this value out of view-source and therefore use
// every write endpoint (mode switch, strategy start/stop, config). The token
// gate still stops drive-by POSTs from scripts that never loaded the page; it
// is no longer a boundary against a determined visitor.
const WRITE_TOKEN = {{ s.token | tojson }};
sessionStorage.setItem('frigoToken', WRITE_TOKEN);

function authHeaders(extra) {
  return Object.assign({ 'Content-Type': 'application/json' },
    { 'X-Auth-Token': WRITE_TOKEN }, extra || {});
}

function setMode(mode) {
  if (mode === 'live') {
    const ok = prompt(
      'GO LIVE?\n\n' +
      'Every order from now on is a REAL order on the Kalshi PRODUCTION account ' +
      'with REAL money. Simulated fills stop.\n\n' +
      'Type LIVE to confirm:');
    if (ok === null) { note('LIVE cancelled'); paintMode(SNAPSHOT.mode); return; }
    if (String(ok).trim().toUpperCase() !== 'LIVE') {
      note('LIVE cancelled - confirmation did not match');
      paintMode(SNAPSHOT.mode);
      return;
    }
  }
  postMode(mode, mode === 'live');
}

async function postMode(mode, confirmLive) {
  try {
    const r = await fetch('/api/mode', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ mode: mode, confirm: !!confirmLive }),
    });
    const d = await r.json().catch(() => ({}));
    if (d.error) {
      // Refused - stay visibly DRY and say exactly why.
      note('mode switch blocked: ' + d.error);
      paintMode(SNAPSHOT.mode);
      flashBanner('STILL IN DRY MODE - ' + d.error, false);
      return;
    }
    note('trading mode is now ' + String(d.mode).toUpperCase());
    paintMode(d);
    flashBanner(d.mode === 'live'
      ? 'LIVE MODE - REAL ORDERS ARE NOW LEAVING THE ACCOUNT'
      : 'DRY MODE - simulated fills only, no real money', d.mode === 'live');
  } catch (e) {
    note('mode switch failed: ' + e.message);
    paintMode(SNAPSHOT.mode);
  }
}

// A temporary full-width banner so a mode change is impossible to miss even if
// you looked away at the moment it happened.
let bannerTimer = null;
function flashBanner(text, isLive) {
  let el = $('modeBanner');
  if (!el) {
    el = document.createElement('div');
    el.id = 'modeBanner';
    el.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:10000;' +
      'padding:14px 18px;text-align:center;font-weight:800;letter-spacing:.12em;' +
      'font-size:14px;box-shadow:0 6px 24px rgba(0,0,0,.5)';
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.style.background = isLive ? '#ff5c7a' : '#2ee6a8';
  el.style.color = '#000';
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => { el.remove(); }, 6000);
}

async function resetDry() {
  const r = await fetch('/api/dry/reset', { method: 'POST', headers: authHeaders() });
  const d = await r.json().catch(() => ({}));
  if (d.error) { $('dryStatus').textContent = d.error; return; }
  $('dryStatus').textContent = 'reset to ' + money(d.dry.starting_balance);
  paintMode(d);
}

function money(v) { return '$' + Number(v || 0).toFixed(2); }

// One function owns every mode indicator: frame colour, body attribute, flag
// text, button state, title and favicon. They cannot drift apart.
let lastMode = document.body.dataset.mode || 'dry';
function paintMode(d) {
  if (!d) return;
  // Missing mode must read as DRY, never as an ambiguous third state.
  const m = d.mode === undefined ? 'dry' : d.mode;
  const live = m === 'live';

  document.body.dataset.mode = live ? 'live' : 'dry';
  $('modeDry').classList.toggle('on', !live);
  $('modeLive').classList.toggle('on', live);
  const flag = $('modeFlag');
  flag.className = 'modeflag ' + (live ? 'live' : 'dry');
  $('modeFlagText').textContent = live ? 'LIVE MODE' : 'DRY MODE';

  document.title = 'Kalshi-Frigo — ' + (live ? 'LIVE' : 'DRY') + ' Trading Dashboard';
  const f = $('favicon');
  if (f) {
    const bg = live ? '%23ff5c7a' : '%232ee6a8';
    const ch = live ? 'L' : 'D';
    f.href = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">' +
      '<rect width="32" height="32" rx="8" fill="' + bg + '"/>' +
      '<text x="16" y="23" font-size="19" font-weight="bold" text-anchor="middle" fill="#000">' +
      ch + '</text></svg>';
  }

  // The headline tiles are different markup per mode - DRY shows the simulated
  // book, LIVE the real account - so they cannot be patched in place. Repaint the
  // safety indicators immediately, then reload for correct tiles. This also
  // catches a mode change made from another browser.
  if (m !== lastMode) {
    lastMode = m;
    setTimeout(() => location.reload(), 450);
    return;
  }

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
  // The funding card answers with the money the CURRENT book can actually spend:
  // the real Kalshi balance in LIVE, the simulated ledger in DRY. Reading the
  // real balance into the DRY card is what made DRY "pose" the live account.
  set('fBalance', money((d.mode === 'live' ? d.funding : d.dry_funding || {}).balance));
  // The bulk-start button must say - and do - what the switch says.
  const sab = $('startAllBtn');
  if (sab) sab.textContent = d.mode === 'live' ? 'Start all LIVE' : 'Start all in DRY';
}

// --- controls ---
async function toggleStrategy(name, quiet) {
  // Start or stop based on the card's actual state, so a bulk "start all" can
  // never be turned into a bulk "stop all" by a stale button label.
  const card = (SNAPSHOT.strategy_cards || []).find(c => c.name === name);
  const body = {}; // no mode: the server uses whatever the DRY/LIVE switch says
  if (card && card.running) {
    await fetch('/api/strategy/' + encodeURIComponent(name) + '/toggle', {
      method: 'POST', headers: authHeaders(), body: JSON.stringify(body),
    });
    if (!quiet) note(name + ': stopped');
  } else {
    const r = await fetch('/api/strategy/' + encodeURIComponent(name) + '/toggle', {
      method: 'POST', headers: authHeaders(), body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (!quiet) note(d.error ? name + ': ' + d.error : name + ': ' + (d.running ? 'started' : 'stopped'));
  }
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

// --- live feeds: BTC spot, Kalshi ladder, account curve ---
// Drawn from whatever the server has cached, then polled. Nothing here invents
// a number: an empty series renders "waiting" rather than a flat line that reads
// like a real result.
const feedCharts = {};
function drawFeed(id, labels, datasets, opts) {
  if (typeof Chart === 'undefined') return;
  const ctx = $(id);
  if (!ctx) return;
  const cfg = {
    type: 'line',
    data: { labels: labels, datasets: datasets },
    options: Object.assign({
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { labels: { color: '#e6edf6', boxWidth: 10, font: { size: 10 } } } },
      scales: {
        x: { ticks: { color: '#5b6a80', maxTicksLimit: 5, font: { size: 9 } }, grid: { color: 'rgba(255,255,255,.04)' } },
        y: { ticks: { color: '#5b6a80', font: { size: 9 } }, grid: { color: 'rgba(255,255,255,.04)' } },
      },
    }, opts || {}),
  };
  if (feedCharts[id]) { feedCharts[id].destroy(); }
  feedCharts[id] = new Chart(ctx, cfg);
}
function hhmmss(t) { return new Date(t * 1000).toLocaleTimeString(); }

function paintMarket(md) {
  if (!md || !md.spot) return;
  const spot = md.spot || {};
  const pts = spot.points || [];
  $('spotSource').textContent = spot.source || 'connecting...';
  $('spotAge').textContent = spot.price
    ? '$' + Number(spot.price).toLocaleString() + '  ·  ' + (spot.age_sec != null ? spot.age_sec.toFixed(1) + 's old' : '')
    : 'no tick yet';
  const pill = $('spotPill');
  if (pill) {
    pill.className = 'pill ' + (spot.fresh ? 'ok' : 'warn');
    pill.textContent = spot.fresh ? 'live' : (spot.price ? 'stale' : 'waiting');
  }
  drawFeed('spotChart', pts.map(p => hhmmss(p.t)), [{
    label: 'BTC-USD spot', borderColor: '#f7a600', backgroundColor: 'rgba(247,166,0,.10)',
    data: pts.map(p => p.p), tension: 0.2, fill: true, pointRadius: 0, borderWidth: 1.5,
  }]);

  const k = md.kalshi || {};
  const kpts = md.series || [];
  const contract = k.market || null;
  if (contract) $('k15Ticker').textContent = contract.ticker;
  const kp = $('k15Pill');
  if (kp) {
    kp.className = 'pill ' + (contract ? 'ok' : 'warn');
    kp.textContent = contract ? 'quoted' : 'no quote';
  }
  const set15 = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set15('k15Up', contract && contract.up_ask != null ? Math.round(contract.up_ask * 100) + '¢' : '--');
  set15('k15Down', contract && contract.down_ask != null ? Math.round(contract.down_ask * 100) + '¢' : '--');
  set15('k15Target', contract && contract.target != null ? '$' + Number(contract.target).toLocaleString() : '--');
  // Countdown runs locally between polls so it ticks smoothly instead of
  // jumping every five seconds.
  if (contract && contract.close_time) {
    DEADLINE = new Date(contract.close_time).getTime() / 1000;
  }
  tickClock();
  $('k15Detail').textContent = contract
    ? (contract.title || 'up or down') + ' · ' + (k.tradable || 0) + ' quotable of ' + (k.open_markets || 0)
      + (k.error ? ' · ' + k.error : '')
    : 'waiting for the next contract';
  drawFeed('k15Chart', kpts.map(p => hhmmss(p.t)), [
    {
      label: 'spot', borderColor: '#f7a600', backgroundColor: 'rgba(247,166,0,.08)',
      data: kpts.map(p => p.spot), tension: 0.2, pointRadius: 0, borderWidth: 1.5,
    },
    {
      label: 'target', borderColor: '#8fa3bd', borderDash: [3, 3],
      data: kpts.map(p => p.target), tension: 0, pointRadius: 0, borderWidth: 1.2,
    },
  ], { scales: { x: { ticks: { color: '#5b6a80', maxTicksLimit: 4, font: { size: 9 } } },
                  y: { ticks: { color: '#5b6a80', font: { size: 9 } },
                       // Spot and target are ~$84k; a zero-based axis would
                       // flatten both into a single line.
                       min: (function () {
                         const vals = kpts.flatMap(p => [p.spot, p.target]).filter(v => v != null);
                         return vals.length ? Math.min.apply(null, vals) - 40 : undefined;
                       })(),
                       max: (function () {
                         const vals = kpts.flatMap(p => [p.spot, p.target]).filter(v => v != null);
                         return vals.length ? Math.max.apply(null, vals) + 40 : undefined;
                       })(),
                  } } });

  const lead = md.lead || {};
  $('feedNote').textContent = lead.usable
    ? 'streaming · ' + (lead.series || 'KXBTC15M') + ' live'
    : (k.error ? 'streaming · Kalshi: ' + k.error : 'streaming');
}

let DEADLINE = 0;
function tickClock() {
  const el = $('k15Clock');
  if (!el) return;
  if (!DEADLINE) { el.textContent = '--'; return; }
  const left = Math.max(0, DEADLINE - Date.now() / 1000);
  const m = Math.floor(left / 60);
  const s = Math.floor(left % 60);
  el.textContent = m + ':' + String(s).padStart(2, '0');
  el.className = left < 60 ? 'down' : '';
}

function paintAccount(a) {
  if (!a) return;
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set('acctLabel', a.label);
  set('acctEquity', money(a.equity));
  set('acctWin', (a.today ? a.today.win_rate : 0) + '%');
  set('acctOpen', a.open_positions);
  set('acctFoot', 'cash ' + money(a.cash) + ' · ' + (a.today ? a.today.trades : 0) + ' trades today');
  const pnl = $('acctToday');
  const v = a.today ? a.today.pnl : 0;
  if (pnl) {
    pnl.textContent = money(v);
    pnl.className = v > 0 ? 'up' : (v < 0 ? 'down' : 'flat');
  }
  const pill = $('acctPill');
  if (pill) {
    pill.className = 'pill ' + (a.book === 'dry' ? 'ok' : 'warn');
    pill.textContent = String(a.book || '').toUpperCase();
  }
  const curve = a.curve || { labels: [], pnl: [] };
  drawFeed('accountChart', curve.labels, [{
    label: "Today's P&L ($)", data: curve.pnl,
    borderColor: (a.today && a.today.pnl > 0) ? '#2ee6a8' : ((a.today && a.today.pnl < 0) ? '#ff5c7a' : '#4d9fff'),
    backgroundColor: 'rgba(46,230,168,.10)', tension: 0.3, fill: true, pointRadius: 2,
  }]);
}

async function refreshFeeds() {
  try {
    paintMarket(await fetch('/api/marketdata').then(r => r.json()));
  } catch (_) { /* feed still starting */ }
  try {
    const d = await fetch('/api/accounts').then(r => r.json());
    if (d.accounts) paintAccount(d.accounts[d.current]);
  } catch (_) { /* ignore */ }
}

// --- strategy cards ---
// Each card's curve is read from the server-rendered snapshot, so the grid is
// populated on the first byte and does not depend on Chart.js loading.
const sparks = {};
function drawSparks(cards) {
  if (typeof Chart === 'undefined') return;
  (cards || SNAPSHOT.strategy_cards || []).forEach(c => {
    const ctx = $('spark-' + c.name);
    if (!ctx) return;
    if (sparks[c.name]) sparks[c.name].destroy();
    const flat = !(c.equity && c.equity.pnl.length);
    sparks[c.name] = new Chart(ctx, {
      type: 'line',
      data: {
        labels: flat ? ['no closed trades yet'] : c.equity.labels,
        datasets: [{
          label: 'Cumulative P&L ($)',
          data: flat ? [0] : c.equity.pnl,
          borderColor: c.realized > 0 ? '#2ee6a8' : (c.realized < 0 ? '#ff5c7a' : '#4d9fff'),
          backgroundColor: 'rgba(46,230,168,.08)',
          tension: 0.3, fill: true, pointRadius: 0, borderWidth: 1.5,
        }],
      },
      options: sparkOptions(),
    });
  });
}
function sparkOptions() {
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: {
      legend: { display: false },
      tooltip: { callbacks: { title: it => it[0].label, label: it => '$' + it.parsed.y } },
    },
    scales: {
      x: { display: false },
      y: { ticks: { color: '#5b6a80', maxTicksLimit: 3, font: { size: 9 } }, grid: { color: 'rgba(255,255,255,.04)' } },
    },
  };
}

// --- strategy detail ---
let detailChart = null;
let openName = null;
function cell(t, v, cls) { return '<td' + (cls ? ' class="' + cls + '"' : '') + '>' + esc(v) + '</td>'; }

async function openStrategy(name, keepScroll) {
  openName = name;
  const panel = $('detailPanel');
  panel.style.display = '';
  $('detailTitle').textContent = name;
  $('detailState').textContent = 'loading...';
  const r = await fetch('/api/strategy/' + encodeURIComponent(name));
  const d = await r.json().catch(() => ({}));
  if (d.error) {
    $('detailState').textContent = '';
    $('detailBody').innerHTML = '<div class="empty">' + esc(d.error) + '</div>';
    return;
  }
  const s = d.strategy;
  $('detailTitle').textContent = s.label;
  $('detailState').textContent = s.running
    ? 'running (pid ' + s.pid + ')'
    : 'stopped';

  const stat = (label, value, cls) =>
    '<div class="cstats" style="grid-template-columns:repeat(auto-fit,minmax(88px,1fr));margin:0 0 12px">'
    + '<div><b class="' + (cls || '') + '">' + value + '</b><span>' + label + '</span></div></div>';

  let html = '<p style="color:var(--dim);font-size:12.5px;margin-bottom:10px">' + esc(s.description) + '</p>';
  html += '<div class="bar" style="margin-bottom:12px"><span class="tag">python ' + esc(s.command) + '</span>'
    + '<span class="tag">' + esc(d.book) + ' book</span>'
    + '<span class="mono" style="color:var(--faint)">pid ' + (s.pid == null ? '-' : s.pid) + '</span></div>';
  html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:8px;margin-bottom:14px">'
    + stat('trades', s.trades)
    + stat('realized', money(s.realized), s.realized > 0 ? 'up' : (s.realized < 0 ? 'down' : 'flat'))
    + stat('win rate', s.win_rate + '%')
    + stat('best', money(s.best), 'up')
    + stat('worst', money(s.worst), 'down')
    + stat('open', s.open_positions)
    + stat('deployed', money(s.deployed))
    + '</div>';
  html += '<div style="height:190px;margin-bottom:14px"><canvas id="detailChart"></canvas></div>';

  html += '<h3 style="font-size:12px;margin:0 0 6px">Open positions</h3>';
  html += d.positions.length
    ? '<table><thead><tr><th>Market</th><th>Side</th><th>Entry</th><th>Qty</th><th>Stop</th><th>Target</th><th>Opened</th></tr></thead><tbody>'
      + d.positions.map(p => '<tr>' + cell('', p.market_id, 'mono') + cell('', p.side) + cell('', money(p.entry_price))
        + cell('', p.quantity) + cell('', p.stop_loss == null ? '-' : money(p.stop_loss))
        + cell('', p.take_profit == null ? '-' : money(p.take_profit)) + cell('', p.opened, 'mono') + '</tr>').join('')
      + '</tbody></table>'
    : '<div class="empty">No open positions in this book.</div>';

  html += '<h3 style="font-size:12px;margin:14px 0 6px">Closed trades</h3>';
  html += d.trades.length
    ? '<table><thead><tr><th>Market</th><th>Side</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&amp;L</th><th>Closed</th></tr></thead><tbody>'
      + d.trades.map(t => '<tr>' + cell('', t.market_id, 'mono') + cell('', t.side) + cell('', money(t.entry_price))
        + cell('', money(t.exit_price)) + cell('', t.quantity)
        + cell('', money(t.pnl), t.pnl > 0 ? 'up' : (t.pnl < 0 ? 'down' : 'flat'))
        + cell('', t.exit_timestamp, 'mono') + '</tr>').join('')
      + '</tbody></table>'
    : '<div class="empty">No closed trades yet for this strategy.</div>';

  html += '<h3 style="font-size:12px;margin:14px 0 6px">Process log</h3>';
  html += '<pre>' + esc((d.logs || []).join('\n') || 'No log yet. Start the strategy to generate one.') + '</pre>';
  $('detailBody').innerHTML = html;

  if (detailChart) detailChart.destroy();
  const ctx = $('detailChart');
  if (ctx && typeof Chart !== 'undefined') {
    const eq = s.equity || { labels: [], pnl: [] };
    const empty = !eq.pnl.length;
    detailChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: empty ? ['no closed trades yet'] : eq.labels,
        datasets: [{
          label: 'Cumulative P&L ($)',
          data: empty ? [0] : eq.pnl,
          borderColor: s.realized > 0 ? '#2ee6a8' : (s.realized < 0 ? '#ff5c7a' : '#4d9fff'),
          backgroundColor: 'rgba(46,230,168,.10)',
          tension: 0.3, fill: true, pointRadius: 2, borderWidth: 2,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { labels: { color: '#e6edf6', boxWidth: 10 } } },
        scales: {
          x: { ticks: { color: '#5b6a80', maxTicksLimit: 8 }, grid: { color: 'rgba(255,255,255,.04)' } },
          y: { ticks: { color: '#5b6a80' }, grid: { color: 'rgba(255,255,255,.04)' } },
        },
      },
    });
  }
  if (!keepScroll) panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
function closeStrategy() {
  openName = null;
  $('detailPanel').style.display = 'none';
  if (detailChart) { detailChart.destroy(); detailChart = null; }
}

async function startAll() {
  const names = (SNAPSHOT.strategy_cards || []).map(c => c.name);
  for (const n of names) await toggleStrategy(n, true);
  note('started: ' + names.join(', '));
}
async function stopAll() {
  const names = (SNAPSHOT.strategy_cards || []).filter(c => c.running).map(c => c.name);
  for (const n of names) await toggleStrategy(n, true);
  note('stopped: ' + (names.join(', ') || 'nothing was running'));
}

// --- init ---
drawChart();
drawSparks();
paintMarket({{ (m | tojson) }});
paintAccount({{ (acct | tojson) }});
loadLogs();
paintMode({ mode: SNAPSHOT.mode.mode });
refresh();
refreshFeeds();
setInterval(refresh, 10000);
setInterval(loadLogs, 30000);
setInterval(refreshFeeds, 5000);
setInterval(tickClock, 500);
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
    for target in (_monitor_loop, _log_tail_loop, _backup_loop, _market_data_loop):
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
