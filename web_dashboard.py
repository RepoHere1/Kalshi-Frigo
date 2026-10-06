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
import re
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
from typing import Any, Deque, Dict, List, Optional, Tuple, cast

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
# Consecutive respawns the supervisor will attempt for one strategy before it
# gives up and leaves it stopped. A strategy that cannot start at all (bad
# credentials, a syntax error) must fail loudly, not become a spawn loop.
#
# This counts *consecutive* attempts only. A crash-loop that briefly succeeds is
# not terminal - the counter resets whenever a process is found alive - so a
# transient failure such as `database is locked` during startup cannot spend the
# whole budget in a few seconds and permanently disable a strategy.
_SUPERVISOR_MAX_ATTEMPTS = 8
# A strategy must stay up this long to count as a real recovery.
_SUPERVISOR_STABLE_SECONDS = 120
# A running strategy whose own process has not stamped a heartbeat this long is
# factually dead or wedged, whatever its pid says. The supervisor kills and
# respawns it; the page refuses to render it as "running".
_HEARTBEAT_STALE_SECONDS = 120
# Strategies that burn the OpenRouter key out of proportion to their value.
# quick_flip is the #1 spender (3,079 calls, $14.61 tracked, half the lifetime
# burn) - the Start button on its card is blocked with a popup so it stays off,
# in BOTH books. The block lives in the client, so the operator is reminded,
# not secretly overridden.
HEAVY_API_ABUSERS = {
    "quick_flip": (
        "quick_flip is the #1 OpenRouter spender - 3,079 LLM calls and over "
        "half of the tracked API cost. It is left OFF on purpose."
    )
}
# Every strategy is wanted from boot. The operator stops lanes by hand; the app
# does not decide that a strategy it could not start once is better off down.
# A strategy only stays down when Stop was pressed, which clears `desired`.
AUTO_START_ALL = True
# Stop reasons that mean the operator said stop. Only these keep a lane down
# across a restart; anything else was a crash, a redeploy or a lost record and
# the strategy comes back up.
_OPERATOR_STOP_REASONS = frozenset(
    {"stopped by operator", "stopped by app restart and by operator"}
)
# Backoff ceiling between respawns. Retries never stop, they just slow down, so
# a prolonged outage cannot become a busy loop.
_SUPERVISOR_MAX_BACKOFF = 60.0

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
    "ledger": None,
    # When the Kalshi numbers below were last actually refreshed from the API
    # (wall-clock string + epoch). A failed sync leaves these untouched, so the
    # page can say "STALE (6m old)" instead of presenting a dead $0.29 as the
    # live truth forever.
    "kalshi_at": None,
    "kalshi_at_ts": 0.0,
    "market": {},
    "market_titles": {},
}

# Strategy control state
strategy_state = {
    "ai_directional": {"running": False, "pid": None, "mode": "paper"},
    "safe_compounder": {"running": False, "pid": None, "mode": "paper"},
    "beast_mode": {"running": False, "pid": None, "mode": "paper"},
    "market_making": {"running": False, "pid": None, "mode": "paper"},
    "quick_flip": {"running": False, "pid": None, "mode": "paper"},
    "btc_updown": {"running": False, "pid": None, "mode": "live"},
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
    "beast_mode": ["cli.py", "run", "--beast", "--paper", "--loop", "--interval", "300"],
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
    # The AI directional strategy records its trades under the name of the
    # routine that produced them. Without this, every trade and position it made
    # landed in "unattributed" and the ai_directional card read 0 trades / $0.00
    # realized while the bot had in fact been trading.
    "immediate_portfolio_optimization": "ai_directional",
    "portfolio_optimization": "ai_directional",
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


# Page cap for the fills/settlements history walk. Kalshi's fills endpoint is
# cursor-paged and defaults to 100 rows, so a single page would silently truncate
# a busy account's history and understate realized P&L - the same class of bug as
# reading only open positions. Bounded so a large history cannot stall the sync.
_LEDGER_MAX_PAGES = 20
_LEDGER_PAGE_SIZE = 1000


async def _fetch_all_fills(client) -> List[Dict[str, Any]]:
    """Every fill the account has, following the cursor to the end of the history."""
    out: List[Dict[str, Any]] = []
    cursor = ""
    for _ in range(_LEDGER_MAX_PAGES):
        page = await client.get_fills(cursor=cursor or None, limit=_LEDGER_PAGE_SIZE)
        rows = page.get("fills") or [] if isinstance(page, dict) else []
        out.extend(r for r in rows if isinstance(r, dict))
        cursor = str((page or {}).get("cursor") or "") if isinstance(page, dict) else ""
        if not rows or not cursor:
            break
    return out


async def _fetch_all_settlements(client) -> List[Dict[str, Any]]:
    """Every settlement still in the live data set, following the cursor."""
    out: List[Dict[str, Any]] = []
    cursor = ""
    for _ in range(_LEDGER_MAX_PAGES):
        page = await client.get_settlements(cursor=cursor or None, limit=_LEDGER_PAGE_SIZE)
        rows = page.get("settlements") or [] if isinstance(page, dict) else []
        out.extend(r for r in rows if isinstance(r, dict))
        cursor = str((page or {}).get("cursor") or "") if isinstance(page, dict) else ""
        if not rows or not cursor:
            break
    return out


async def _fetch_kalshi_data():
    """Fetch balance, positions, fills and settlements from the Kalshi API.

    The history endpoints are fetched in their own try/except so a failure there
    degrades the account panel's realized P&L to "unknown" instead of taking the
    balance and position count down with it. Those two are what the page needs to
    be worth reading at all.
    """
    from src.clients.kalshi_client import KalshiClient

    if not kalshi_configured():
        return None, None, [], []
    try:
        key_path = materialize_private_key()
    except OSError as e:
        _push_error(f"Kalshi key material: {e}")
        return None, None, [], []

    # KalshiClient's constructor is eager: it loads the PEM and raises if it is
    # missing, so it must never be built when the key is unavailable. There is no
    # initialize() step - signing happens per request in _make_authenticated_request.
    try:
        client = KalshiClient(private_key_path=key_path) if key_path else KalshiClient()
    except Exception as e:
        _push_error(f"Kalshi client init: {e}")
        return None, None, [], []

    try:
        balance = await client.get_balance()
        positions = await client.get_positions()
    except Exception as e:
        _push_error(f"Kalshi fetch: {e}")
        return None, None, [], []
    finally:
        await client.close()

    fills: List[Dict[str, Any]] = []
    settlements: List[Dict[str, Any]] = []
    try:
        # Rebuilt for the history walk: the client above is already closed, and
        # constructing a second one is cheaper than holding the first open across
        # the extra paginated requests.
        client = KalshiClient(private_key_path=key_path) if key_path else KalshiClient()
        try:
            # Bounded: balance + positions above already stand on their own, so
            # a hung history walk degrades realized P&L to "unknown" instead
            # of holding a gthread for the full 120s _run_async timeout.
            fills = await asyncio.wait_for(_fetch_all_fills(client), _KALSHI_HISTORY_BUDGET_SEC)
            settlements = await asyncio.wait_for(
                _fetch_all_settlements(client), _KALSHI_HISTORY_BUDGET_SEC
            )
        finally:
            await client.close()
    except Exception as e:  # noqa: BLE001 - the balance/positions result still stands
        _push_error(f"Kalshi history: {e}")

    return balance, _normalise_positions(positions), fills, settlements


def _normalise_balance(payload: Any) -> Dict[str, Any]:
    """Coerce the balance response into a dict.

    Same shape drift as the positions endpoint: `/portfolio/balance` has
    answered as an object and as a single-element list. Read as a dict, a list
    raised `'list' object has no attribute 'get'` inside the monitor loop and the
    whole sync was abandoned - which is why the account panel showed nothing at
    all rather than stale numbers.
    """
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        for row in payload:
            if isinstance(row, dict):
                return row
        return {}
    if payload is None:
        return {}
    return {"balance": payload}


def _normalise_positions(payload: Any) -> Dict[str, Any]:
    """Coerce the positions response into the dict shape callers expect.

    `/portfolio/positions` has answered in two shapes: a dict of
    `market_positions` / `event_positions`, and a bare list. The dashboard read
    it as a dict unconditionally, so a list response raised
    `'list' object has no attribute 'get'` every refresh - which surfaced as a
    permanent "Kalshi connect" error while the panel quietly kept rendering
    whatever was last cached.

    A bare list is classified by whether the row names an event, because an
    event position settles differently from a single market.
    """
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        markets: List[Dict[str, Any]] = []
        events: List[Dict[str, Any]] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            if row.get("event_ticker"):
                events.append(row)
            else:
                markets.append(row)
        return {"market_positions": markets, "event_positions": events}
    return {}


def _fill_side_and_price(fill: Dict[str, Any]) -> Tuple[str, float]:
    """(direction, unit price) for one fill, in dollars.

    Kalshi quotes everything from the YES side, and the canonical fields are
    `outcome_side` (which outcome you end up holding) and `book_side` (bid = buy,
    ask = sell). The legacy `action` field is deprecated and its removal was
    scheduled, so it is only a fallback.

    Getting the sign wrong here is not cosmetic: `outcome_side` alone does not
    say whether the fill added or removed exposure. `outcome_side: no` describes
    both "bought NO" and "sold YES", which are opposite cash flows.
    """
    outcome = str(fill.get("outcome_side") or "").strip().lower()
    book = str(fill.get("book_side") or "").strip().lower()
    action = str(fill.get("action") or "").strip().lower()

    price = _as_float(
        fill.get("no_price_dollars") if outcome == "no" else fill.get("yes_price_dollars")
    )
    # `yes_price_dollars`/`no_price_dollars` are both always populated and
    # sum to 1.0, so a zero here means the field was absent under a different
    # name rather than a genuinely free fill. Fall back to the other side.
    if price <= 0.0:
        other = "yes_price_dollars" if outcome == "no" else "no_price_dollars"
        price = _as_float(fill.get(other))

    if book in ("bid", "ask"):
        direction = "buy" if book == "bid" else "sell"
    elif action in ("buy", "sell"):
        direction = action
    else:
        direction = ""

    # Defensive: if the two disagree, trust book_side (the field Kalshi says is
    # canonical) but keep the price consistent with what was actually paid.
    return direction, max(price, 0.0)


def _kalshi_ledger(
    fills: List[Dict[str, Any]], settlements: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Account totals derived from the fill and settlement history.

    WHY NOT `/portfolio/positions`
    -----------------------------
    Positions describe only what is still held. Kalshi drops a ticker from that
    list once its market settles, and with it goes that market's realized P&L.
    Summing `realized_pnl_dollars` across the rows therefore reports the P&L of
    markets still open, not the account's realized P&L - which is why the tile
    read a fixed $11.54 that never moved while trades were being opened and
    closed underneath it. It is not a stale cache; it is the wrong set of rows.

    So the totals come from the two endpoints that are complete by construction:

    - `/portfolio/fills` - every fill, so cost basis and sales proceeds are exact.
    - `/portfolio/settlements` - every resolved market's cost basis and revenue.

    Realized P&L is then an identity over cash that actually moved:

        on a sale:    (sell price - average cost of the shares sold) * shares
        on settlement: revenue - the cost basis of the contracts that settled

    so it changes when a trade closes or a market resolves. Note the sale case
    needs the average cost of the *specific* shares being sold, which is why the
    fills are walked in time order carrying a per-ticker running position; netting
    total sales against total buys would always give zero.

    Cost basis is what those running positions still hold, so it reports capital
    currently deployed rather than everything ever bought.
    """
    realized = 0.0
    fees = 0.0
    fill_count = 0
    total_bought = 0.0
    total_proceeds = 0.0
    sales_realized = 0.0
    settlement_cost = 0.0
    settlement_realized = 0.0
    window_start: Optional[str] = None
    window_end: Optional[str] = None

    # Per-ticker running position, so a sale can be matched against what that
    # sale actually cost rather than against the account's total buying.
    #
    # Proceeds alone cannot give realized P&L: `sales - sales` is always zero.
    # The cost of the specific contracts being sold has to come from the buys that
    # created them, which is what these two maps carry.
    held_qty: Dict[str, float] = {}
    held_cost: Dict[str, float] = {}

    ordered = sorted(
        (f for f in fills if isinstance(f, dict)),
        key=lambda f: (_as_float(f.get("ts")), str(f.get("created_time") or "")),
    )

    for f in ordered:
        ticker = str(f.get("ticker") or f.get("market_ticker") or "").strip()
        if not ticker:
            continue
        ts = str(f.get("created_time") or f.get("ts") or "")
        if window_start is None or ts < window_start:
            window_start = ts
        if window_end is None or ts > window_end:
            window_end = ts
        direction, price = _fill_side_and_price(f)
        qty = abs(_as_float(f.get("count_fp")))
        if direction == "" or qty <= 0.0:
            continue
        amount = qty * price
        fees += _as_float(f.get("fee_cost"))
        fill_count += 1

        if direction == "buy":
            held_qty[ticker] = held_qty.get(ticker, 0.0) + qty
            held_cost[ticker] = held_cost.get(ticker, 0.0) + amount
            total_bought += amount
            continue

        # A sale. Clamp to what is actually held: after a historical cutoff the
        # buys behind some sales may be missing, and claiming a bigger cost basis
        # than existed would invent a loss.
        on_hand = held_qty.get(ticker, 0.0)
        closing = min(qty, on_hand) if on_hand > 0.0 else 0.0
        avg_cost = (held_cost.get(ticker, 0.0) / on_hand) if on_hand > 0.0 else 0.0
        cost_released = avg_cost * closing
        total_proceeds += amount
        if closing > 0.0:
            realized += (price - avg_cost) * closing
            sales_realized += (price - avg_cost) * closing
            held_qty[ticker] = on_hand - closing
            held_cost[ticker] = held_cost.get(ticker, 0.0) - cost_released

    # Whatever is still held is capital at work, at the average price paid for
    # it -- but only AFTER settlements zero their tickers below. Settled
    # contracts are consumed without a sale fill, so capturing the total before
    # that loop reported the cost of the dead as "capital currently deployed":
    # the account showed cost $485.82 next to a $1.92 mark-to-market, and both
    # were "true" only under two different definitions. The map is therefore
    # built at the end, from what actually still has shares.
    settle_revenue = 0.0
    settlement_count = 0
    for s in settlements:
        if not isinstance(s, dict):
            continue
        # `revenue` is integer cents; the cost fields are fixed-point dollar
        # strings. Mixing the two units is a 100x error, so they are converted
        # separately and explicitly.
        revenue = _as_float(s.get("revenue")) / 100.0
        settled_cost = _as_float(s.get("yes_total_cost_dollars")) + _as_float(
            s.get("no_total_cost_dollars")
        )
        settle_revenue += revenue
        settlement_cost += settled_cost
        realized += revenue - settled_cost
        settlement_realized += revenue - settled_cost
        fees += _as_float(s.get("fee_cost"))
        settlement_count += 1
        # The contracts are gone now, so they are no longer deployed capital.
        ticker = str(s.get("ticker") or "").strip()
        if ticker:
            held_qty[ticker] = 0.0
            held_cost[ticker] = 0.0

    # Cost basis of what is held RIGHT NOW: tickers with shares left. Cost
    # residue on a zero-qty row is rounding dust from avg-cost release and
    # counts as nothing.
    basis_by_ticker = {t: c for t, c in held_cost.items() if held_qty.get(t, 0.0) > 0.0 and c > 0.0}
    open_cost = sum(basis_by_ticker.values())

    return {
        "realized": round(realized, 2),
        "fees": round(fees, 2),
        "cost_basis": round(max(open_cost, 0.0), 2),
        "bought": round(total_bought, 2),
        "proceeds": round(total_proceeds, 2),
        "settlement_revenue": round(settle_revenue, 2),
        "sales_realized": round(sales_realized, 2),
        "settlement_cost": round(settlement_cost, 2),
        "settlement_realized": round(settlement_realized, 2),
        "fills_counted": fill_count,
        "settlements_counted": settlement_count,
        "open_positions_in_ledger": sum(1 for v in held_qty.values() if v > 0.0),
        # Per-ticker cost of what is still held, after settlements. The
        # account tile sums this over the tickers Kalshi says are held; a held
        # ticker missing from this map means its buys predate the retained
        # fill history and its cost is unknown, not zero.
        "basis_by_ticker": {t: round(c, 4) for t, c in basis_by_ticker.items()},
        "window_start": window_start,
        "window_end": window_end,
        "complete": True,
    }


_ASYNC_LOOP: Optional[asyncio.AbstractEventLoop] = None
_ASYNC_THREAD: Optional[threading.Thread] = None


def _async_loop() -> asyncio.AbstractEventLoop:
    """One event loop for the whole process, running on its own thread.

    Delegated to src/utils.async_bridge: every module that bridges sync code
    into async (the dashboard, mode.run, strategy_runtime.run) now shares ONE
    loop instead of minting a throwaway loop per call. Each throwaway loop
    leaked its aiosqlite executor threads, which is what exhausted the worker
    with "can't start new thread" and made Railway restart the container - and
    every strategy child died with it.
    """
    from src.utils.async_bridge import _get_loop

    return _get_loop()


def _run_async(coro):
    """Run a coroutine to completion from synchronous code.

    Safe to call from any thread, including a request handler, by handing the
    coroutine to the process-wide loop and waiting for the result.
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

    loop = _async_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    # Generous, because a busy database can legitimately take seconds under
    # six concurrent strategies - but bounded, so a wedged loop surfaces as an
    # error instead of a hung request.
    return future.result(timeout=120)


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
    """One balance/positions/history sync from the Kalshi API into dashboard_state."""
    result = _run_async(_fetch_kalshi_data())
    # Tolerate a 2-tuple: an older or partially-patched fetch still answers
    # (balance, positions), and the history would then be empty rather than fatal.
    balance, positions, fills, settlements = (
        list(result) + [[], []]
        if isinstance(result, (list, tuple)) and len(result) == 2
        else result
    )
    if balance:
        # Kalshi reports cents; the dashboard shows dollars.
        balance = _normalise_balance(balance)
        dashboard_state["balance"] = float(balance.get("balance", 0) or 0) / 100.0
        # Freshness truth: only a sync that actually delivered a balance moves
        # this. Failures and empty answers keep the previous stamp, so the
        # page ages the numbers instead of freezing them silently.
        dashboard_state["kalshi_at"] = _now()
        dashboard_state["kalshi_at_ts"] = time.time()
    if positions:
        # Kalshi uses different keys for event markets vs market tickers.
        merged = list(positions.get("market_positions") or [])
        merged += list(positions.get("event_positions") or [])
        dashboard_state["positions"] = merged
    # Recomputed on every sync rather than accumulated, so it is always the
    # history as it stands now and cannot drift by double-counting a page.
    if fills or settlements:
        dashboard_state["ledger"] = _kalshi_ledger(
            [f for f in fills if isinstance(f, dict)],
            [s for s in settlements if isinstance(s, dict)],
        )
    dashboard_state["last_update"] = _now()
    dashboard_state["kalshi_position_count"] = len(dashboard_state["positions"])
    # Titles are resolved only for the book actually being traded. Caching them
    # from `merged` shipped the production account's tickers inside the DRY
    # snapshot's market_titles map - real tickers on an otherwise simulated page,
    # which is the same blur as the account panel, one map smaller.
    _refresh_market_titles(_current_book_tickers())


_title_client: Any = None
_TITLE_CACHE_MAX = 400
# One request per interval now resolves the whole batch, so the floor is a
# politeness gap rather than a rate-limit workaround. It used to be 120s because
# a batch cost 25 sequential requests; at one request the same politeness is 30s
# and the panel converges in a couple of syncs instead of a couple of hours.
_TITLE_MIN_INTERVAL_SECONDS = 30
_title_last_attempt = 0.0
_title_backoff_until = 0.0
# Tickers whose last lookup did not yield a title. Kept separately from the cache
# because the cached value may be a perfectly good decoded fallback, and "value
# equals ticker" is not a reliable test of whether the API was ever asked.
_title_unresolved: set = set()


def _current_book_tickers() -> List[Dict[str, Any]]:
    """Position rows belonging to the book currently being traded, and no others.

    In LIVE these come from Kalshi itself. In DRY they come from the database,
    because the DRY book's positions are simulated and exist nowhere else - and
    because reading them off Kalshi would put the real account's tickers into
    the DRY page's title cache.
    """
    if _current_book_mode() == "live":
        rows = list(dashboard_state.get("positions") or [])
        # Only rows that still hold contracts are shown, so only those need a
        # title. Resolving all 31 rows spent the whole lookup budget on tickers
        # the page never renders - the zero-share rows Kalshi keeps for every
        # market the account ever touched.
        held = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            if r.get("ticker"):
                # A market row: contracts currently held.
                if _as_float(r.get("position_fp")) != 0.0:
                    held.append(r)
            elif r.get("event_ticker"):
                # An event row: `total_cost_shares_fp` is cumulative history, so
                # exposure is the only signal that something is still open.
                if _as_float(r.get("event_exposure_dollars")) != 0.0:
                    held.append(r)
        return held or rows
    try:
        where = _book_filter("dry")
        rows = _db_many([(_SQL_POSITIONS.format(book=where), ())])

        # Inside the try, and tolerant of a row type that is not a dict: this
        # runs on the monitor thread's critical path, and an exception here
        # aborts the whole sync rather than just the title lookup.
        def _ticker(r):
            if isinstance(r, dict):
                return r.get("market_id") or r.get("ticker")
            try:
                return r["market_id"]
            except Exception:  # noqa: BLE001
                return None

        return [{"ticker": _ticker(r), "event_ticker": _ticker(r)} for r in rows]
    except Exception:  # noqa: BLE001 - titles are cosmetic; never fail the sync
        return []


def _refresh_market_titles(positions):
    """Fetch English titles for market tickers and cache them.

    Kalshi tickers like KXBTC15M-26OCT021600-00 are opaque to users. The API
    returns a human-readable title per market, and we cache the mapping so the
    dashboard can show "BTC 15-minute" instead of the raw ticker.

    ONE REQUEST PER BATCH, NOT ONE PER TICKER
    ------------------------------------------
    This used to call `get_market(ticker)` inside a loop - 25 separate HTTP
    requests every interval, for 25 tickers. That is what drove the markets
    endpoint into HTTP 429, and the 429 handler then abandoned the whole batch,
    so the panel never resolved anything at all. Kalshi's `get_markets` accepts
    a `tickers` list and answers all of them in one call, so the batch now costs
    one request and the rate limit stops being the bottleneck.

    A FAILED LOOKUP IS NOT CACHED AS THE TITLE
    --------------------------------------------
    The old code wrote `cached[ticker] = ticker` when a lookup failed. Because
    the "already cached?" test is `ticker not in cached`, that one-off failure
    was permanent: the ticker was stored as its own title, every later pass saw
    it as resolved, and it was never retried. A transient 404 or timeout
    therefore locked the raw ticker into the page for the life of the process.

    Now a failure caches the prettified fallback instead - still a readable
    label, but recorded in a separate `_title_unresolved` set so the next pass
    retries the API and replaces it with the real title when one is available.

    The retry test has to be that set, not "is the cached value still equal to
    the ticker". Inferring it from the value fails the moment the fallback is
    decodable: `_prettify_ticker("KXMYSTERY-26")` returns "MYSTERY", which
    differs from the ticker, so the value looks resolved and the lookup is never
    attempted again.
    """
    global _title_client, _title_last_attempt, _title_backoff_until

    cached = dashboard_state["market_titles"]
    if len(cached) > _TITLE_CACHE_MAX:
        cached.clear()
        _title_unresolved.clear()

    missing = set()
    for p in positions:
        t = p.get("ticker") or p.get("event_ticker") or ""
        if t and (t not in cached or t in _title_unresolved):
            missing.add(t)
    if not missing or not os.environ.get("KALSHI_API_KEY"):
        return

    now = time.time()
    if now < _title_backoff_until or now - _title_last_attempt < _TITLE_MIN_INTERVAL_SECONDS:
        return

    batch = sorted(missing)[:50]
    _title_last_attempt = now
    try:
        if _title_client is None:
            key_path = materialize_private_key()
            from src.clients.kalshi_client import KalshiClient

            _title_client = KalshiClient(private_key_path=key_path) if key_path else KalshiClient()
        resp = _run_async(_title_client.get_markets(tickers=batch, limit=len(batch)))
        # Same shape drift as every other Kalshi endpoint: /markets has answered
        # as {"markets": [...]} and as a bare list. Reading it only as a dict
        # raised AttributeError, the whole batch was abandoned, and titles
        # silently stayed as raw tickers forever.
        rows: List[Dict[str, Any]] = []
        if isinstance(resp, dict):
            rows = [r for r in (resp.get("markets") or []) if isinstance(r, dict)]
        elif isinstance(resp, list):
            rows = [r for r in resp if isinstance(r, dict)]
        resolved = set()
        for m in rows:
            if not isinstance(m, dict):
                continue
            t = str(m.get("ticker") or "")
            title = str(m.get("title") or m.get("subtitle") or "").strip()
            if t and title:
                cached[t] = title
                resolved.add(t)
        _title_unresolved.difference_update(resolved)

        # Anything the batch did not return (delisted, or a stale reference) gets
        # the decoded ticker, and is remembered as unresolved so the next pass
        # tries again instead of treating this as settled.
        for ticker in batch:
            if ticker in resolved:
                continue
            cached[ticker] = _prettify_ticker(ticker) or ticker
            _title_unresolved.add(ticker)
    except Exception as exc:  # noqa: BLE001 - titles are cosmetic
        if "429" in str(exc) or "Too Many Requests" in str(exc):
            _title_backoff_until = now + _TITLE_MIN_INTERVAL_SECONDS * 4
            _push_error("Kalshi rate-limited title lookup; backing off.")
            return
        return


def _prettify_ticker(ticker: str) -> str:
    """Turn an opaque Kalshi ticker into something a human can read.

    Kalshi tickers are a series prefix plus a date/stake suffix, e.g.
    `KXDJIA-26DEC31-54000`. When the API cannot supply the English title - the
    markets endpoint is rate limited to the point of returning 429 under the
    polling load this dashboard puts on it - the ticker is all the user sees, and
    it reads as noise. This decodes the parts that are reliably decodable and
    leaves anything it cannot explain as the original ticker rather than
    inventing a confident wrong label.

    Returns the ticker unchanged when nothing is recognisable.
    """
    if not ticker:
        return ""
    parts = str(ticker).split("-")
    if len(parts) < 2:
        return str(ticker)

    series = parts[0]
    if not series.startswith("KX"):
        return str(ticker)
    name = series[2:]

    # Date suffix: 26DEC31 -> Dec 31 2026. Also handles 26OCT021845 (15-min).
    months = {
        "JAN": "Jan",
        "FEB": "Feb",
        "MAR": "Mar",
        "APR": "Apr",
        "MAY": "May",
        "JUN": "Jun",
        "JUL": "Jul",
        "AUG": "Aug",
        "SEP": "Sep",
        "OCT": "Oct",
        "NOV": "Nov",
        "DEC": "Dec",
    }

    def _date(raw: str) -> str:
        m = re.match(r"^(\d{2})([A-Z]{3})(\d{1,2})$", raw or "")
        if m and m.group(2) in months:
            return f"{months[m.group(2)]} {int(m.group(3))} 20{m.group(1)}"
        # Intraday: 26OCT022215 -> Oct 2 2026 22:15
        m = re.match(r"^(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})$", raw or "")
        if m and m.group(2) in months:
            return (
                f"{months[m.group(2)]} {int(m.group(3))} 20{m.group(1)}"
                f" {m.group(4)}:{m.group(5)}"
            )
        return ""

    # Series names Kalshi abbreviates to something unreadable.
    series_names = {
        "SB": "Super Bowl",
        "NFL": "NFL",
        "WNBA": "NBA Championship",
        "NBAFINALS": "NBA Finals",
        "NBA": "NBA",
        "MLB": "MLB",
        "NEXTTEAMNBA": "Next Team NBA",
        "MLBW": "MLB World Series",
        "NFLCHAMP": "NFL Championship",
        "NFLAFCCHAMP": "NFL AFC Championship",
        "NFLAFC": "NFL AFC",
        "NFLCONFCHAMP": "NFL Conference Championship",
        "NFLMVP": "NFL MVP",
        "BOXING": "Boxing",
        "CABILLIONAIRETAX": "CA Billionaire Tax",
        "DJIA": "Dow Jones Industrial Average",
        "NASDAQ": "Nasdaq 100",
        "SPX": "S&P 500",
        "F1": "Formula 1",
        "BTC15M": "BTC 15-minute",
        "BTC": "Bitcoin",
        "ETH": "Ethereum",
        "GAS": "Gasoline",
        "HIGH": "NYC high temperature",
        "RAIN": "Rainfall",
        "TEMP": "Temperature",
    }
    label_name = series_names.get(name, name)

    tail = parts[1:]
    date = ""
    label = label_name
    year = ""
    for idx, tok in enumerate(tail):
        d = _date(tok)
        if d:
            date = date or d
        elif re.fullmatch(r"\d{2}", tok) and name in series_names:
            # A bare 2-digit token on a known series is the season/year.
            year = f"20{tok}"
        elif re.fullmatch(r"\d{1,2}", tok):
            # A horizon suffix (15m, 1h) carries no meaning for a human.
            continue
        elif tok.isdigit() and len(tok) >= 3:
            # A strike. 54000 -> 54,000
            label = f"{label_name} above {int(tok):,}"
        else:
            # A team/city code or a venue marker.
            label = f"{label_name} · {tok}"

    if year and not date:
        date = year
    if not label.strip(" ·"):
        # Nothing decodable (e.g. "KX-"). Echo the ticker rather than a stub.
        return str(ticker)
    if date and label != label_name:
        return f"{label} · {date}"
    if date:
        return f"{label_name} · {date}"
    return label or str(ticker)


def _market_title(ticker):
    """Return the cached English title for a ticker, or a readable fallback."""
    if not ticker:
        return ""
    cached = (dashboard_state.get("market_titles") or {}).get(ticker, "")
    # A cached value equal to the ticker means the lookup failed; the prettifier
    # is strictly more useful than echoing the ticker back.
    if cached and cached != ticker:
        return cached
    return _prettify_ticker(ticker) or ticker


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
        _monitor_once(interval, {"warned_missing": warned_missing})
        warned_missing = True  # _monitor_once warns only on the first miss
        time.sleep(interval)


def _kalshi_digest() -> str:
    """Fingerprint of the Kalshi numbers the tally tiles render.

    Balance, position rows and the realized ledger: anything a fill,
    settlement or funding event would move. Deliberately excludes the error
    list, so a failing sync does not look like fresh activity.
    """
    try:
        return json.dumps(
            {
                "balance": dashboard_state.get("balance"),
                "positions": dashboard_state.get("positions"),
                "ledger": dashboard_state.get("ledger"),
            },
            sort_keys=True,
            default=str,
        )
    except Exception:  # noqa: BLE001 - a digest never breaks the loop
        return ""


def _kalshi_age_sec() -> int:
    """Age of the Kalshi numbers in seconds, or -1 when never synced."""
    try:
        ts = float(dashboard_state.get("kalshi_at_ts") or 0.0)
    except (TypeError, ValueError):
        return -1
    if ts <= 0:
        return -1
    return max(0, int(time.time() - ts))


_LAST_SNAPSHOT_DIGEST: Optional[str] = None


def _publish_if_changed() -> bool:
    """Bust the snapshot memo and push an SSE event when Kalshi moved.

    Returns True when something changed. The monitor calls this after every
    sync: connected pages refetch exactly when there is something new instead
    of polling blind, and a quiet hour costs no rebuilds at all.
    """
    global _LAST_SNAPSHOT_DIGEST
    digest = _kalshi_digest()
    if digest == _LAST_SNAPSHOT_DIGEST:
        return False
    _LAST_SNAPSHOT_DIGEST = digest
    _SNAPSHOT_CACHE["payload"] = None
    _SNAPSHOT_CACHE["at"] = 0.0
    _broadcast("snapshot", {"at": _now(), "kalshi_at": dashboard_state.get("kalshi_at")})
    return True


def _monitor_once(interval: int, flags: Dict[str, Any]) -> bool:
    """One monitor pass. Split out so tests can drive it without the sleep."""
    dashboard_state["status"] = "online"
    dashboard_state["has_openrouter_creds"] = bool(os.environ.get("OPENROUTER_API_KEY", "").strip())
    dashboard_state["has_kalshi_creds"] = kalshi_configured()

    # Make sure the schema exists so the dashboard shows real numbers even
    # on a fresh container where the trading loop has never run.
    try:
        _db()
    except Exception as e:
        _push_error(f"Database init: {e}")

    if not dashboard_state["has_kalshi_creds"]:
        if not flags.get("warned_missing"):
            _push_error("Kalshi credentials not configured — info mode only")
            flags["warned_missing"] = True
        dashboard_state["last_update"] = _now()
    else:
        try:
            _refresh_kalshi()
        except Exception as e:
            _push_error(f"Kalshi connect: {e}")
        _publish_if_changed()
    return True


def _market_data_loop():
    """Own the live BTC feeds for the life of the process.

    The feeds are websocket-driven and therefore async, while the dashboard's
    handlers are synchronous Flask routes. Rather than opening a loop per
    request - which would tear down the socket on every poll - one thread holds a
    single event loop and one long-lived connection, and the routes read the
    cached payload it produces.

    This used to be fire-and-forget: `hub.start()` sat outside the inner
    try/except, so a single failure at startup (a refused socket, a DNS blip, an
    import that resolved differently in the container) ended the thread
    permanently. The page then rendered the empty initial payload forever -
    "connecting...", "no tick yet", "waiting for the next contract" - with no
    error anywhere, because the one error that explained it had been raised once
    and then scrolled out of the buffer. The only cure was a redeploy. The whole
    hub is now rebuilt on failure instead.
    """
    import asyncio

    from src.jobs.market_data import MarketDataHub

    async def _run() -> None:
        hub = MarketDataHub()
        await hub.start()
        while True:
            try:
                dashboard_state["market"] = hub.chart_payload()
            except Exception as e:  # noqa: BLE001 - a bad payload must not kill the feed
                _push_error(f"Market data: {e}")
            await asyncio.sleep(2)

    def _thread_main() -> None:
        backoff = 1.0
        while True:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                dashboard_state["market_started_at"] = time.time()
                loop.run_until_complete(_run())
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                _push_error(f"Market data feed restarting: {type(e).__name__}: {e}")
                dashboard_state["market_error"] = f"{type(e).__name__}: {e}"
            finally:
                try:
                    loop.close()
                except Exception:  # noqa: BLE001
                    pass
            # Never leave the feed dark for long: retry, backing off so a hard
            # outage does not turn into a busy loop.
            dashboard_state["market"] = {}
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    threading.Thread(target=_thread_main, daemon=True, name="market-data").start()


def _strategy_supervisor_loop():
    """Keep every strategy the operator asked for actually running.

    On boot, every strategy is wanted (AUTO_START_ALL). Stopping a lane clears
    its `desired` flag and it stays down; nothing else takes a strategy out of
    service. That inverts the old failure mode, where a strategy that died once
    - or was never recorded - simply stayed down and the book quietly ran on
    whatever happened to still be alive.

    A strategy process is a child, and children die: an unhandled exception in a
    trading cycle, an OOM kill, a transient API failure during boot, a locked
    database. Retries never stop - they slow to a ceiling and keep going - so a
    bad stretch cannot leave a lane down. Stopping is the only way down, and that
    is the operator pressing Stop.
    """
    # Backoff per strategy, and a hard cap on consecutive respawns. Without the
    # cap a strategy that cannot start (bad credentials, a syntax error) becomes
    # a spawn loop that buries the real error in the log.
    failures: Dict[str, float] = {}
    attempts: Dict[str, int] = {}

    while True:
        try:
            store = _runtime_store()
            if AUTO_START_ALL and not _creds_present():
                time.sleep(10)
                continue
            # THIS BOOK ONLY. DRY and LIVE are separate books with separate
            # runtime rows; the supervisor manages whichever book the page is
            # in and never touches the other one. Stopping a strategy in LIVE
            # therefore leaves DRY's own state exactly as it was.
            # An unreadable book means NO book: spawn nothing rather than
            # guess, because a guessed book is how LIVE lanes got armed
            # without a single operator push.
            book_mode = _runtime_mode()
            if book_mode is None:
                time.sleep(10)
                continue
            wanted = _run_async(store.desired(mode=book_mode))
            # Every DRY lane is wanted from boot. Stopping a lane clears
            # its `desired` flag and it stays down; nothing else takes
            # a strategy out of service. That inverts the old failure
            # mode, where a strategy that died once - or was never recorded -
            # simply stayed down and the book quietly ran on
            # whatever happened to still be alive.
            #
            # THE LAW: AUTO-START is a DRY-book convenience only. The LIVE
            # book trades real money, so a LIVE lane may come into being
            # ONLY by an explicit operator push in the LIVE book (which
            # persists desired=1 and is thereafter resumed by the supervisor).
            # Booting while the book reads LIVE must never arm anything by
            # itself - this exact path is what turned the LIVE LLM strategy
            # on behind a DRY button push.
            if AUTO_START_ALL and book_mode == "dry":
                recorded = _run_async(store.snapshot(mode=book_mode))

                for name in strategy_state:
                    if _runtime_key(name) in wanted:
                        continue
                    row = recorded.get(_runtime_key(name))
                    # LAW + cost guard: heavy API abusers (quick_flip) are never
                    # auto-started onto a book with no record. The operator left
                    # them OFF on purpose; AUTO_START_ALL must not resurrect
                    # them. An explicit Start press still goes through the toggle
                    # endpoint (which refuses abusers with an explanation), and
                    # a pre-existing desired=1 row is still honoured/resumed.
                    if row is None and name in HEAVY_API_ABUSERS:
                        continue
                    if row is not None:
                        # A row exists, so this book's button has been pushed
                        # before. The operator's last click is PERMANENT truth:
                        # a Stop keeps the lane down across redeploys, book
                        # switches and any number of logins, forever, until
                        # Start is pressed in this book again. No expiry, no
                        # instance scoping - the button is welded to the state.
                        if (row.get("stop_reason") or "") in _OPERATOR_STOP_REASONS:
                            continue
                    if not _creds_present():
                        continue
                    try:
                        _run_async(store.set_desired(name, True, book_mode))
                        _spawn_strategy(name, book_mode)
                        _push_error(
                            f"Started {name} in {book_mode.upper()} "
                            f"(all strategies run by default)."
                        )
                        # Stagger the boot storm. Six strategies starting at once
                        # all race for the SQLite write lock, and their lock
                        # contention is what turned the dashboard's DB reads into
                        # timed-out, thread-leaking calls on boot.
                        time.sleep(3)
                    except Exception as exc:  # noqa: BLE001
                        _push_error(f"Auto-start {name} failed: {exc}")
            for _ck, row in wanted.items():
                name = row.get("name") or ""
                if name not in strategy_state:
                    continue
                row_pid = row.get("pid")
                if row_pid and _pid_alive(row_pid, row):
                    # A pid that exists is not proof of life. A strategy that
                    # has stopped stamping heartbeats is wedged - its process is
                    # up but its loop is not running. HOWEVER: do NOT kill it
                    # just because of a stale heartbeat. The old code killed +
                    # respawned on stale heartbeat, which crossed into the wrong
                    # mode if the book changed during respawn. The operator's
                    # button is LAW: once they click START in a mode, that mode
                    # is permanent until they click STOP. No supervisor respawn
                    # may cross-contaminate the book by guessing a different mode.
                    #
                    # So: monitor the heartbeat and alert, but ONLY STOP on
                    # explicit operator click. Never auto-kill a strategy.
                    hb = row.get("heartbeat_at")
                    if hb:
                        try:
                            hb_age = (
                                datetime.now() - datetime.fromisoformat(str(hb))
                            ).total_seconds()
                            if hb_age > _HEARTBEAT_STALE_SECONDS:
                                # Alert the operator, but do NOT kill the process.
                                # The button is the only kill switch.
                                _push_error(
                                    f"WARNING: {name} heartbeat is {hb_age:.0f}s old "
                                    f"(process may be hung). Press STOP and START to restart, "
                                    f"or contact support."
                                )
                                # Do NOT kill; the operator's button is the only stop.
                        except Exception:  # noqa: BLE001 - malformed stamp: ignore
                            pass
                    # A process that survives a settling period is a genuine
                    # recovery, so the failure budget starts again. Without this
                    # the budget was spent by a few rapid spawns during a brief
                    # database lock and the strategy was never retried again.
                    started = row.get("started_at")
                    if started:
                        try:
                            born = datetime.fromisoformat(str(started))
                            if (
                                datetime.now() - born
                            ).total_seconds() >= _SUPERVISOR_STABLE_SECONDS:
                                if attempts.get(name):
                                    _push_error(
                                        f"{name} stayed up past "
                                        f"{_SUPERVISOR_STABLE_SECONDS}s - recovery confirmed."
                                    )
                                attempts[name] = 0
                                failures.pop(name, None)
                        except Exception:  # noqa: BLE001
                            pass
                    continue

                # Not alive. A pid from a previous instance of the app is stale
                # by definition, not a crash.
                from src.utils.strategy_runtime import INSTANCE

                stale_instance = bool(row_pid) and (row.get("instance") or "") != INSTANCE
                if stale_instance:
                    # "Run until I say otherwise" has to survive a
                    # redeploy, or every push silently stops the book.
                    # Resume it regardless of mode - the operator
                    # armed it before the restart, and that intent
                    # must survive.
                    failures[name] = 0.0
                    attempts[name] = 0

                if not _creds_present():
                    continue  # nothing to start with; wait for configuration

                mode = book_mode
                backoff = failures.get(name, 0.0)
                if attempts.get(name, 0) >= _SUPERVISOR_MAX_ATTEMPTS:
                    # Never terminal. The old behaviour gave up permanently
                    # after 8 attempts, so one bad stretch - a locked database,
                    # a deploy mid-write - left strategies down until somebody
                    # noticed and pressed Start. It now keeps trying forever and
                    # simply slows down, reporting that it is still trying. Only
                    # pressing Stop clears `desired`, and only that leaves a
                    # strategy down.
                    if backoff == 0.0:
                        _push_error(
                            f"Supervisor has retried {name} {_SUPERVISOR_MAX_ATTEMPTS}"
                            f"+ times without a stable start; still retrying every"
                            f" {_SUPERVISOR_MAX_BACKOFF:.0f}s until you press Stop."
                        )
                        failures[name] = _SUPERVISOR_MAX_BACKOFF
                    continue
                if backoff > 0:
                    time.sleep(min(backoff, _SUPERVISOR_MAX_BACKOFF))

                # SUPERVISOR RESPAWN GUARD: Never respawn a strategy in the wrong book.
                # If this is a respawn (stale_instance or recent crash), verify the book
                # mode hasn't changed since the row was recorded.
                if stale_instance or (started and (datetime.now() - datetime.fromisoformat(str(started))).total_seconds() < _SUPERVISOR_STABLE_SECONDS):
                    # This is a respawn attempt. Check if the current book matches the recorded mode.
                    current_book = _current_book_mode()
                    recorded_mode = row.get("mode")
                    if current_book and recorded_mode:
                        expected_book = "live" if recorded_mode == "live" else "dry"
                        if current_book != expected_book:
                             # Book changed! Refuse to respawn in the wrong book.
                             _push_error(
                                 f"BOOK MODE GUARD: {name} was recorded in {expected_book} "
                                 f"but the current book is {current_book}. Refusing respawn "
                                 f"to prevent cross-contamination. It is still DESIRED=ON in its "
                                 f"original book - press Start in {expected_book} to resume it."
                             )
                             # DO NOT clear_desired here! The operator's intent is permanent.
                             # They clicked START in {expected_book}, so that switch must survive
                             # until they manually click STOP in that book. Never auto-off a strategy.
                             continue  # Skip this strategy; don't spawn it

                try:
                    started = _spawn_strategy(name, mode)
                    attempts[name] = attempts.get(name, 0) + 1
                    failures[name] = min(max(backoff * 2, 2.0), 30.0)
                    why = (
                        "resumed after an app restart"
                        if stale_instance
                        else "restarted after it died"
                    )
                    _push_error(f"Supervisor {why}: {name} (pid {started['pid']}, {mode}).")
                    _broadcast("strategy", {"name": name, "action": "restarted"})
                except Exception as exc:  # noqa: BLE001
                    attempts[name] = attempts.get(name, 0) + 1
                    failures[name] = min(max(backoff * 2, 2.0), 30.0)
                    _push_error(f"Supervisor could not start {name}: {type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - the supervisor must never die
            _push_error(f"Strategy supervisor: {type(exc).__name__}: {exc}")
        time.sleep(10)


def _creds_present() -> bool:
    return bool(os.environ.get("KALSHI_API_KEY")) and bool(
        os.environ.get("KALSHI_PRIVATE_KEY") or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    )


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
    """The bot list for the page - THIS BOOK's lanes only.

    THE LAW: the operator's button pushes are facts about the book the page
    is in. Listing the other book's lanes (its pids, its running pills) made
    a DRY Start look like it had switched LIVE strategies on. Each book's
    truth comes from its own runtime rows; the other book is not rendered.
    """
    book = _runtime_mode()
    rows: Dict[str, Dict[str, Any]] = {}
    if book is not None:
        from src.utils.strategy_runtime import key

        try:
            rows = _run_async(_runtime_store().snapshot(mode=book))
        except Exception:  # noqa: BLE001 - render the page even if the store hiccups
            rows = {}
    payload: List[Dict[str, Any]] = []
    for name, st in strategy_state.items():
        row = rows.get(key(name, book)) if book is not None else None
        row = row or {}
        pid = row.get("pid") or st.get("pid")
        running = bool(pid) and _pid_alive(pid, row or st)
        payload.append(
            {
                "name": name,
                "running": running,
                "pid": pid,
                "mode": book or st.get("mode", "paper"),
                "label": STRATEGY_DOCS.get(name, ("", ""))[0],
                "description": STRATEGY_DOCS.get(name, ("", ""))[1],
                "started_at": row.get("started_at") or st.get("started_at"),
                "stop_reason": row.get("stop_reason") or st.get("stop_reason") or "",
            }
        )
    return payload


def _strategy_bucket(name: Optional[str]) -> str:
    """Map whatever a trade log recorded onto one of the five buttons."""
    key = (name or "").strip().lower()
    return STRATEGY_ALIASES.get(key, key or "unattributed")


def _current_book_mode() -> Optional[str]:
    """Which book's numbers the page is showing: 'dry' or 'live'.

    THE LAW: a button push in one book is a fact about THAT book only. What
    broke the law was a guessed book - this read used to fail open to 'dry',
    so during a boot-time database blip one worker answered 'dry' while
    another read the persisted 'live', and strategy processes got armed in a
    book the operator never pushed. A read failure therefore returns None,
    and every mutating caller (toggle, supervisor, reconciler) REFUSES to act
    on an unknown book. Display may degrade; writes may not guess.
    """
    try:
        from src.utils.mode import run

        mode = str(run(_mode_manager().current())).strip().lower()
        return mode if mode in ("dry", "live") else None
    except Exception:
        return None


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

    The `running` flag is checked against the DATABASE, not the in-memory
    `strategy_state` dict. That dict is per-process and can be stale across
    gunicorn workers. The database is the shared source of truth, and we
    filter by book mode so a LIVE-running strategy does not show as "running"
    on the DRY page and vice versa.
    """
    store = _runtime_store()
    recorded = _run_async(store.snapshot(mode=_runtime_mode()))

    cards: Dict[str, Dict[str, Any]] = {}
    for name, st in strategy_state.items():
        label, description = STRATEGY_DOCS.get(name, (name, ""))
        db_row = recorded.get(_runtime_key(name)) or {}
        db_pid = db_row.get("pid")
        db_mode = db_row.get("mode")
        # The runtime store records "paper"/"live", but the book
        # mode is "dry"/"live". Map "paper" → "dry" so the comparison
        # with `book` is correct and DRY strategies show as running.
        if db_mode == "paper":
            db_mode = "dry"
        db_running = bool(db_pid) and _pid_alive(db_pid, db_row)
        # Constant live proof: a heartbeat stamped by the strategy's own loop.
        # A pid can be recycled or zombie; only a fresh heartbeat proves the
        # strategy factually executed a cycle just now.
        hb_age: Optional[float] = None
        hb = db_row.get("heartbeat_at")
        if hb:
            try:
                hb_age = (datetime.now() - datetime.fromisoformat(str(hb))).total_seconds()
            except Exception:  # noqa: BLE001
                hb_age = None
        hb_stale = hb_age is not None and hb_age > _HEARTBEAT_STALE_SECONDS
        db_running = db_running and not hb_stale
        # NO BOOK FILTER. Filtering by book made every strategy read "off" the
        # moment the operator switched books, even though the process was still
        # running in the other book - and the supervisor migration then had a
        # page full of "off" cards to contradict. Running means running, period;
        # the book it runs in is shown next to it, and the supervisor migrates
        # desired lanes into the current book within one pass.
        running_for_this_book = bool(db_running)
        cards[name] = {
            "name": name,
            "label": label,
            "description": description,
            "running": bool(running_for_this_book),
            "running_book": db_mode or None,
            "heavy_api_abuser": name in HEAVY_API_ABUSERS,
            "heavy_api_abuser_reason": HEAVY_API_ABUSERS.get(name, ""),
            "stuck": bool(bool(db_pid) and _pid_alive(db_pid, db_row) and hb_stale),
            "heartbeat_age_sec": round(hb_age, 1) if hb_age is not None else None,
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
# Every close, ever, for the ANALYSIS OF panel. The trade log is a forever
# database: rows are appended and never pruned, and the file lives on the
# Railway volume, so this query sees the account's entire history - the
# analysis only gets deeper as more trades close.
_SQL_ANALYSIS = (
    "SELECT market_id, side, entry_price, exit_price, quantity, pnl, strategy,"
    " entry_timestamp, exit_timestamp, exit_reason FROM trade_logs"
    " WHERE {book} ORDER BY COALESCE(exit_timestamp, entry_timestamp) ASC"
)
_SQL_ANALYSIS_STRAT = (
    "SELECT market_id, side, entry_price, exit_price, quantity, pnl, strategy,"
    " entry_timestamp, exit_timestamp, exit_reason FROM trade_logs"
    " WHERE {book} AND COALESCE(NULLIF(strategy, ''), 'unattributed') = ?"
    " ORDER BY COALESCE(exit_timestamp, entry_timestamp) ASC"
)
# Who is actually consuming the OpenRouter key. Every LLM call is logged with
# its strategy and query type, so the burn is attributable to a named consumer
# - which strategy, doing what - instead of a single mystery total.
_SQL_LLM_USAGE = (
    "SELECT strategy, query_type, COUNT(*) AS n,"
    " COALESCE(SUM(tokens_used), 0) AS tokens,"
    " COALESCE(SUM(cost_usd), 0) AS cost,"
    " MIN(timestamp) AS first_at, MAX(timestamp) AS last_at"
    " FROM llm_queries GROUP BY strategy, query_type"
    " ORDER BY cost DESC"
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
    if wins > trades:
        wins = trades
    return {
        "trades": trades,
        "wins": wins,
        # Derived, never trusted from SQL: a row whose pnl is exactly 0 was
        # counted as neither win nor loss by the query, which once rendered
        # "36.4% · 30W / 0L" on the same card - an impossibility that made
        # every tally suspect. losses is BY DEFINITION trades minus wins.
        "losses": trades - wins,
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


def _truth_positions_for_live(
    positions: List[Dict[str, Any]], account: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """The LIVE open-position list, reconciled against Kalshi.

    Local rows whose market is not among Kalshi's held markets are settled or
    phantom and must not render as open. Kalshi-held markets the local book
    never recorded are added from the account payload so nothing real is
    missing. Returns only what Kalshi says is actually open.
    """
    held = {r["ticker"] for r in account.get("markets", [])}
    out = [p for p in positions if p.get("market_id") in held]
    seen = {p.get("market_id") for p in out}
    for r in account.get("markets", []):
        if r["ticker"] in seen:
            continue
        out.append(
            {
                "market_id": r["ticker"],
                "side": "long" if r["shares"] > 0 else "short",
                "entry_price": None,
                "quantity": r["shares"],
                "strategy": "kalshi_api",
                "stop_loss": None,
                "take_profit": None,
                "status": "open",
                "timestamp": "",
                "mode": "live",
                "source": "kalshi",
            }
        )
    dashboard_state["stale_live_rows"] = sum(1 for p in positions if p.get("market_id") not in held)
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

    # Realized P&L, fees and cost basis come from the fill/settlement ledger, not
    # from these rows. `realized_pnl_dollars` on a position describes that market
    # only for as long as the market is still held: Kalshi drops a settled ticker
    # from /portfolio/positions, taking its realized P&L with it. Summing the
    # column therefore reports "P&L on markets still open" and reads as a frozen
    # number while trades open and close beneath it.
    #
    # With no ledger the tallies are reported as unknown rather than as $0.00. A
    # zero here would be indistinguishable from a real break-even account, which is
    # the one thing a P&L tile must never be.
    ledger = dashboard_state.get("ledger") or {}
    have_ledger = bool(ledger.get("complete"))
    positions_realized = round(total(markets, "realized") + total(events, "realized"), 2)
    positions_fees = round(total(markets, "fees") + total(events, "fees"), 2)

    # ZERO-SHARE ROWS ARE NOT POSITIONS, AND EVENT ROWS ARE NOT SEPARATE ONES
    # ---------------------------------------------------------------------
    # /portfolio/positions answers with two views of the same book:
    #
    #   market_positions - one row per market, `position_fp` = contracts held now.
    #   event_positions  - one row per EVENT, aggregating every market in it.
    #     `total_cost_shares_fp` / `total_cost_dollars` are CUMULATIVE history,
    #     not current holdings, so they stay large long after nothing is open.
    #
    # Two separate mistakes lived here. The first was counting rows: Kalshi keeps
    # a row per ticker the account ever touched, so 15 markets at exactly 0 shares
    # were being reported as open positions, and event rows carrying $925.84 of
    # historical cost behind 0 exposure were counted too - 33 "live positions"
    # for an account holding 3.
    #
    # The second was summing them. An event row and its market rows describe the
    # SAME contracts, so adding both double-counts: the live account reported
    # $3.84 of exposure against a true $1.92, because KXDJIA-26DEC31 and
    # KXDJIA-26DEC31-54000 each carried $0.94 of the same position.
    #
    # So: a market row is held when `position_fp != 0`; an event row is held when
    # it has exposure, and it is reported for context only. Every money total and
    # the open-position count come from market rows alone, which are the atomic
    # positions. The zero-share rows are still reported as a count, because
    # "18 rows, 3 held" is honest and hiding them just invites the same question.
    held_markets = [r for r in markets if float(r.get("shares") or 0.0) != 0.0]
    held_events = [r for r in events if float(r.get("exposure") or 0.0) != 0.0]
    # The event panel is a per-event view of where money went, so it keeps rows
    # with historical cost even when nothing is open - that is the useful part of
    # it. Those rows are listed but never counted as open positions.
    listed_events = [
        r
        for r in events
        if float(r.get("exposure") or 0.0) != 0.0 or float(r.get("cost") or 0.0) != 0.0
    ]
    ghost_count = (len(markets) - len(held_markets)) + (len(events) - len(held_events))

    # Cost basis answers "what did what I hold RIGHT NOW cost", so it is
    # summed over the tickers Kalshi says are still held, from the ledger's
    # post-settlement basis map. A held ticker missing from the map means its
    # buys predate Kalshi's retained fill history; a negative share count is a
    # short whose "cost" is proceeds received, not basis paid. Either makes
    # the account total unknowable -- the tile then says unknown instead of
    # quoting a confident wrong number (a $485.82 phantom was worse than n/a).
    _basis_map = ledger.get("basis_by_ticker", {}) if have_ledger else {}
    _held_cost_basis = 0.0
    _cost_basis_missing = 0
    for _r in held_markets:
        if float(_r.get("shares") or 0.0) < 0.0:
            _cost_basis_missing += 1
            continue
        _b = _basis_map.get(_r["ticker"])
        if _b is None:
            _cost_basis_missing += 1
        else:
            _held_cost_basis += float(_b)
    _cost_basis_known = have_ledger and _cost_basis_missing == 0

    return {
        "connected": bool(markets or events),
        # Held counts drive every headline. `*_rows` is what the endpoint returned,
        # so the difference between the two is visible rather than mysterious.
        "market_count": len(held_markets),
        "event_count": len(held_events),
        # Atomic truth: event rows are rollups of market rows, so counting both
        # would report one position twice.
        "held_count": len(held_markets),
        "market_rows": len(markets),
        "event_rows": len(events),
        "ghost_count": ghost_count,
        "markets": sorted(held_markets, key=lambda r: -abs(r["exposure"]))[:25],
        "events": sorted(listed_events, key=lambda r: -abs(r["cost"]))[:25],
        # Exposure is what the open positions are worth if sold into the current
        # book. Market rows only - see the double-count note above. It is also not
        # the same quantity as cost basis, so the two are never substituted.
        "exposure": round(total(held_markets, "exposure"), 2),
        "event_exposure": round(total(held_events, "exposure"), 2),
        "cost_basis": round(_held_cost_basis, 2) if _cost_basis_known else 0.0,
        "cost_basis_known": _cost_basis_known,
        "cost_basis_missing": _cost_basis_missing,
        "realized": round(float(ledger.get("realized", 0.0)), 2) if have_ledger else 0.0,
        "fees": round(float(ledger.get("fees", 0.0)), 2) if have_ledger else 0.0,
        "realized_known": have_ledger,
        "traded": round(total(markets, "traded"), 2),
        # Retained for the per-position tables, where a market's own realized
        # figure is exactly right. Only the account-wide total is wrong without
        # the ledger.
        "positions_realized": positions_realized,
        "positions_fees": positions_fees,
        "ledger": ledger,
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
    if book == "live" and account["connected"]:
        # LIVE truth: Kalshi is the only authority on what the real account
        # holds. Local rows for markets Kalshi has settled are stale - the
        # contracts are gone and the money already moved - and rendering them
        # as open positions is a lie. The 29 "open" BTC15M rows came from here:
        # their contracts settled on Kalshi hours ago while the local book kept
        # them open. Only rows Kalshi still holds are shown, and Kalshi rows
        # the local book never saw are added so the list is complete.
        positions = _truth_positions_for_live(positions, account)
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
        # Freshness truth for the tally tiles: when the Kalshi numbers were
        # last actually refreshed, and how old that makes them. -1 means the
        # sync has never delivered, so the page says "syncing" instead of
        # showing a number with no age at all.
        "kalshi_at": dashboard_state.get("kalshi_at"),
        "kalshi_age_sec": _kalshi_age_sec(),
        "has_kalshi_creds": kalshi_configured(),
        "has_openrouter_creds": bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
        # Real-money figures belong to the LIVE book. A DRY snapshot carries no
        # real balance and no real positions at all, so the client cannot render
        # them even if the markup were changed to ask for them.
        "balance": dashboard_state["balance"] if book == "live" else None,
        "kalshi": account if book == "live" else None,
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
        "stale_live_rows": dashboard_state.get("stale_live_rows", 0),
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
        # Every position ticker gets an entry here, real title when available and
        # a decoded human label otherwise - NEVER the raw ticker. The client-side
        # mktTitle() reads this map and falls back to the bare ticker when a key
        # is missing, which is what printed KXBTC15M-26OCT041545-45 in the
        # positions table while the cache was empty or mid-resolution. Sourced
        # from THIS BOOK's position rows only: a DRY snapshot must not carry the
        # real account's tickers in its title map.
        "market_titles": _complete_title_map(
            positions + (account["markets"] + account["events"] if book == "live" else [])
        ),
        "errors": errors[-10:],
        "events": events[-8:],
    }


def _complete_title_map(book_positions: List[Dict[str, Any]]) -> Dict[str, str]:
    """Cached titles plus a decoded fallback for every position ticker."""
    out = dict(dashboard_state.get("market_titles", {}))
    for p in book_positions or []:
        if not isinstance(p, dict):
            continue
        t = p.get("ticker") or p.get("event_ticker") or p.get("market_id") or ""
        if not t:
            continue
        if t not in out or out[t] == t:
            out[t] = _prettify_ticker(t) or t
    return out


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
        # LIVE-ONLY funding guidance (display only, never blocks, never touches
        # DRY): 20%-fraction clips with a $25 book cap need ~$125 to hold a
        # full book and $200+ to survive a losing streak at real size.
        try:
            _bal = float((payload["funding"] or {}).get("balance") or 0.0)
            if _bal < 125.0:
                _why = str((payload["funding"] or {}).get("reason") or "")
                payload["funding"]["recommendation"] = (
                    f"LIVE BTC 15-min wants a $200 minimum ($125 to hold a full "
                    f"$25 book at 20% clips). Current ${_bal:.2f}. "
                    + (_why + " " if _why else "")
                    + "Fund before expecting DRY-like trade counts."
                )
        except Exception:  # noqa: BLE001 - guidance never breaks the payload
            pass
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
            # An unreachable Kalshi is a genuine blocker. An empty balance is not:
            # it used to refuse the switch outright, which trapped the operator in
            # DRY and made the only way into LIVE a top-up. Affordability is
            # enforced per order in build_order_request, which refuses anything the
            # funding source cannot cover, so nothing unfunded can be sent by
            # dropping the check here. The balance is still reported, loudly, on
            # the LIVE page and in this response.
            if not funding.get("can_fund"):
                _push_error(
                    "LIVE mode entered with an unfunded balance: "
                    + str(funding.get("reason", "insufficient funds"))
                    + " - orders will be refused until it is funded."
                )

        mode = run(mgr.set(target, confirmed=confirmed))
    except ModeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        _push_error(f"Mode switch: {e}")
        return jsonify({"error": str(e)}), 500

    # Every figure in a snapshot is scoped to one book, so a payload built before
    # the switch describes the book that is no longer being traded. Served from the
    # memo it would put DRY positions and DRY cash on a page whose own flag reads
    # LIVE - the exact contradiction the book-scoped queries exist to prevent, just
    # arriving up to _SNAPSHOT_TTL seconds late. The switch is rare and explicit,
    # so it is always worth rebuilding.
    _SNAPSHOT_CACHE["payload"] = None
    _SNAPSHOT_CACHE["at"] = 0.0

    _broadcast("mode", {"mode": mode})
    if mode == MODE_DRY:
        # Back to simulated: make sure a DRY book exists and is funded. This
        # CREATES the book if it is missing; it must never wipe one that already
        # has history. Calling reset_dry_account() here meant every pass through
        # the DRY switch erased the ledger, the open positions and the closes, so
        # a profitable book kept snapping back to $300 with nothing to show for
        # the difference. Only the explicit reset endpoint wipes.
        run(mgr.ensure_dry_account())
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


@app.route("/api/trades")
def api_trades():
    """Recent closed trades for veto replay harness (limit 500).

    Each row includes the book mode (dry/live), the strategy name, and
    the rationale string so the two books can be compared side-by-side.
    Pass ?mode=dry or ?mode=live to filter to one book; omit for all.
    """
    try:
        import aiosqlite
        import asyncio
        
        mode_filter = request.args.get("mode", "").strip().lower()
        if mode_filter in ("dry", "live"):
            where = f"COALESCE(NULLIF(mode, ''), 'dry') = '{mode_filter}'"
        else:
            where = "1=1"
        
        async def _fetch():
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT market_id, side, entry_price, pnl, quantity, "
                    "exit_timestamp, mode, strategy, rationale, exit_reason "
                    "FROM trade_logs WHERE exit_timestamp IS NOT NULL "
                    f"AND {where} "
                    "ORDER BY exit_timestamp DESC LIMIT 500"
                ) as cur:
                    rows = await cur.fetchall()
            trades = []
            for (market_id, side, entry_price, pnl, quantity, exit_ts,
                 mode, strategy, rationale, exit_reason) in rows:
                trades.append({
                    "market_id": market_id,
                    "side": side,
                    "entry_price": entry_price,
                    "pnl": pnl,
                    "quantity": quantity,
                    "exit_timestamp": exit_ts,
                    "mode": mode or "dry",
                    "strategy": strategy or "",
                    "rationale": rationale or "",
                    "exit_reason": exit_reason or "",
                })
            return {"trades": trades, "mode_filter": mode_filter or "all"}
        
        return jsonify(asyncio.run(_fetch()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/live/flatten")
def api_live_flatten():
    """Inspect what closing every real position would actually fetch.

    Read-only. Placing market sells into these books without seeing the quotes
    first is how a $925 cost basis turns into a very different number: most of
    these are thin markets where the bid is the only realistic fill and it sits
    well below the cost basis. This returns, per position, the live bid/ask, the
    side held, and what selling into the bid would realise - so the decision is
    made against prices rather than against cost basis.

    Pass ?confirm=1 to actually place the sells.
    """
    denied = require_token()
    if denied is not None:
        return denied

    import uuid

    from src.clients.kalshi_client import KalshiClient
    from src.utils.market_prices import get_market_prices

    confirm = request.args.get("confirm") == "1"

    async def _work():
        key_path = materialize_private_key()
        client = KalshiClient(private_key_path=key_path) if key_path else KalshiClient()
        try:
            balance, positions = await _fetch_kalshi_data()
            rows: List[Dict[str, Any]] = []
            for row in (positions or {}).get("market_positions") or []:
                ticker = row.get("ticker") or ""
                shares = float(row.get("position") or 0)
                if not ticker or not shares:
                    continue
                try:
                    m = await client.get_market(ticker)
                except Exception:  # noqa: BLE001
                    m = {}
                yes_bid, yes_ask, no_bid, no_ask = get_market_prices(m or {})
                # A positive position is long; which side it is long is the
                # `position` field. Selling means hitting the bid on that side.
                side = str(row.get("position") or "yes").lower()
                bid = yes_bid if side == "yes" else no_bid
                ask = yes_ask if side == "yes" else no_ask
                entry = float(row.get("cost_basis") or 0.0) / max(abs(shares), 1)
                proceeds = bid * abs(shares)
                cost = entry * abs(shares)
                placed = None
                if confirm and abs(shares) >= 1:
                    # Marketable limit at the bid, rounded down a cent: a
                    # market order on a thin book can fill far below this.
                    limit = max(int(bid * 100) - 1, 1)
                    try:
                        placed = await client.place_order(
                            ticker=ticker,
                            client_order_id=str(uuid.uuid4()),
                            side=side,
                            action="sell",
                            count=int(abs(shares)),
                            type_="limit",
                            yes_price=limit if side == "yes" else None,
                            no_price=limit if side == "no" else None,
                        )
                    except Exception as exc:  # noqa: BLE001
                        placed = {"error": f"{type(exc).__name__}: {exc}"}
                rows.append(
                    {
                        "ticker": ticker,
                        "title": _market_title(ticker),
                        "side": side,
                        "shares": shares,
                        "entry": round(entry, 4),
                        "bid": bid,
                        "ask": ask,
                        "cost_basis": round(cost, 2),
                        "est_proceeds": round(proceeds, 2),
                        "est_pnl": round(proceeds - cost, 2),
                        "spread": round(ask - bid, 4),
                        "status": m.get("status") if isinstance(m, dict) else None,
                        "close_time": m.get("close_time") if isinstance(m, dict) else None,
                        "order": placed,
                    }
                )
            return {
                "confirm": confirm,
                "count": len(rows),
                "cost_basis": round(sum(r["cost_basis"] for r in rows), 2),
                "est_proceeds": round(sum(r["est_proceeds"] for r in rows), 2),
                "est_pnl": round(sum(r["est_proceeds"] - r["cost_basis"] for r in rows), 2),
                "cash_cents": balance.get("balance") if isinstance(balance, dict) else None,
                "positions": rows,
            }
        finally:
            await client.close()

    try:
        result = _run_async(_work())
    except Exception as e:  # noqa: BLE001
        _push_error(f"Live flatten: {e}")
        return jsonify({"error": str(e)}), 500
    _audit(
        f"live flatten {'EXECUTED' if confirm else 'previewed'}: " f"{result['count']} positions"
    )
    return jsonify(result)


@app.route("/api/dry/audit")
def api_dry_audit():
    """Check the DRY book against its own ledger and report what disagrees.

    Read-only. Reports orphan positions (rows with no fill behind them),
    duplicated closes, and a cash balance that cannot be derived from the
    ledger - the three ways this book's headline numbers came to contradict
    each other.
    """
    try:
        from src.utils.mode import run

        return jsonify(_run_async(_mode_manager().repair_dry_book()))
    except Exception as e:
        _push_error(f"DRY audit: {e}")
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
        snap = build_snapshot_cached()
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

    def _format_date(date_str: str) -> str:
        """Convert YYYY-MM-DD to MON DD, YYYY."""
        try:
            from datetime import datetime

            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return dt.strftime("%b %d, %Y").upper()
        except:
            return date_str

    return render_template_string(
        _TEMPLATE,
        s=snap,
        m={
            "spot": context["spot"],
            "kalshi": context["kalshi"],
            "series": context["series"],
            "feed_error": context["feed_error"],
        },
        lead=context["lead"],
        acct=acct,
        _market_title=_market_title,
        _format_date=_format_date,
    )


_SNAPSHOT_CACHE: Dict[str, Any] = {"at": 0.0, "payload": None}
# 15s memo: one rebuild walks up to 42 serial Kalshi calls (balance +
# positions + 20 fills pages + 20 settlements pages at 30s timeout each), so
# a 2s TTL under several polling clients piled every gthread onto the same
# walk and every request died a 499. Correctness is kept (the walk still
# runs to the end) and freshness loss is 15s of dashboard numbers.
_SNAPSHOT_TTL = 15.0
# Single-flight: only one thread rebuilds at a time. The rest serve the
# last-known payload instead of starting a second 42-call walk.
_SNAPSHOT_LOCK = threading.Lock()
# Time budget for the fills/settlements history walk inside one snapshot.
# Balance + positions (fetched first) still stand on timeout; realized P&L
# degrades to "unknown" instead of hanging the page.
_KALSHI_HISTORY_BUDGET_SEC = 20.0


def build_snapshot_cached() -> Dict[str, Any]:
    """build_snapshot, memoised for a couple of seconds and never allowed to raise.

    Two failure modes made the page look broken rather than busy:

    - `/api/snapshot` returned **500 with an empty body** when a query lost its
      race for the database lock. The client had nothing to render, so every
      figure on the page went to zero - the "blanked to zero" report. A page
      showing slightly stale numbers is worth far more than a blank one.
    - `/api/strategies` took **27 seconds**, which is the 30s busy timeout
      nearly expiring on a blocked read. Buttons looked dead and clicks looked
      ignored, so strategies appeared to "not turn on".

    So: a short TTL so a burst of polls costs one query rather than six, a read
    timeout short enough that a blocked read fails in seconds instead of hanging
    the page, and a last-known-good payload if the rebuild fails outright.

    Single-flight on top: the first thread to miss the cache rebuilds while
    every other thread serves the stale payload instead of starting its own
    Kalshi walk (that pileup was the 499 storm: 32 threads × 42 serial
    calls each).
    """
    now = time.time()
    cached = _SNAPSHOT_CACHE.get("payload")
    ttl = 0.0 if app.config.get("TESTING") else _SNAPSHOT_TTL
    if cached is not None and now - float(_SNAPSHOT_CACHE.get("at") or 0) < ttl:
        return cached
    if not _SNAPSHOT_LOCK.acquire(blocking=False):
        # A rebuild is already in flight: serve stale, never pile up. Cold
        # start (nothing cached yet) waits for the builder instead.
        if cached is not None:
            return cached
        _SNAPSHOT_LOCK.acquire()
    try:
        # Re-check: the builder may have finished while waiting on the lock.
        now = time.time()
        cached = _SNAPSHOT_CACHE.get("payload")
        if cached is not None and now - float(_SNAPSHOT_CACHE.get("at") or 0) < ttl:
            return cached
        try:
            payload = build_snapshot()
        except Exception as exc:  # noqa: BLE001 - never blank the page
            _push_error(f"Snapshot rebuild failed, serving last known: {exc}")
            if cached is not None:
                return cached
            return {
                "mode": _safe_mode_payload(),
                "error": str(exc),
                "positions": [],
                "strategy_cards": [],
                "by_strategy": [],
                "trades": {
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "realized_pnl": 0.0,
                    "win_rate": 0.0,
                    "best_trade": 0.0,
                    "worst_trade": 0.0,
                    "avg_pnl": 0.0,
                },
                "open": {"positions": 0, "capital": 0.0, "paper": 0, "live": 0},
                "kalshi": None,
                "market_titles": {},
                "errors": [],
                "funding": {"connected": False, "balance": 0.0, "can_fund": False, "reason": ""},
            }
    finally:
        _SNAPSHOT_LOCK.release()
    _SNAPSHOT_CACHE["at"] = now
    _SNAPSHOT_CACHE["payload"] = payload
    return payload


@app.route("/api/snapshot")
def api_snapshot():
    """The same payload the page was rendered from, for client-side refreshes."""
    return jsonify(build_snapshot_cached())


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


@app.route("/api/live/journal")
def api_live_journal():
    """Read-only LIVE book journal: local mode='live' rows plus Kalshi truth.

    /api/positions answers from legacy live-flagged rows and falls back to
    Kalshi truth, so the ladder's own mode='live' rows (inserted with
    live=False) are invisible there. This endpoint shows them exactly:
    every open mode='live' row with id/entry/qty/strategy, the 20 most
    recent mode='live' closes with fee_net PnL, and both counts side by
    side. Every fill must leave a visible row -- this is where to check.
    """
    try:
        import aiosqlite

        db = _db()

        async def _read():
            async with aiosqlite.connect(db.db_path) as conn:
                conn.row_factory = aiosqlite.Row
                cur = await conn.execute(
                    "SELECT id, market_id, side, entry_price, quantity,"
                    " strategy, status, mode, timestamp FROM positions"
                    " WHERE COALESCE(NULLIF(mode, ''), 'dry') = 'live'"
                    " AND status = 'open' ORDER BY id DESC LIMIT 100"
                )
                opens = [dict(r) for r in await cur.fetchall()]
                cur = await conn.execute(
                    "SELECT market_id, side, entry_price, exit_price, quantity,"
                    " pnl, fee_paid, exit_reason, strategy, exit_timestamp"
                    " FROM trade_logs"
                    " WHERE COALESCE(NULLIF(mode, ''), 'dry') = 'live'"
                    " ORDER BY COALESCE(exit_timestamp, '') DESC LIMIT 20"
                )
                closes = [dict(r) for r in await cur.fetchall()]
                # Item 7: the weekly one-number check. Totals over EVERY live
                # close, fee-net -- this is the figure to hold against Kalshi's
                # own settlements when judging the book, never a partial sum
                # over the 20 shown rows.
                cur = await conn.execute(
                    "SELECT COUNT(*) AS n,"
                    " COALESCE(SUM(pnl), 0.0) AS net,"
                    " COALESCE(SUM(fee_paid), 0.0) AS fees,"
                    " COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins"
                    " FROM trade_logs"
                    " WHERE COALESCE(NULLIF(mode, ''), 'dry') = 'live'"
                )
                totals = dict(await cur.fetchone() or {})
                return opens, closes, totals

        opens, closes, totals = _run_async(_read())
        for r in opens:
            r["timestamp"] = str(r.get("timestamp") or "")[:19]
            r["notional"] = round(
                float(r.get("entry_price") or 0.0) * int(r.get("quantity") or 0), 2
            )
        for r in closes:
            r["exit_timestamp"] = str(r.get("exit_timestamp") or "")[:19]
        # The reaper's latest pass: what it killed and what Kalshi still holds.
        try:
            import json as _json

            # Anchored to the DB this process already reads, not to an env
            # default: the trader writes the summary beside its own db_path and
            # the two processes do not necessarily share DB_PATH.
            import os as _os

            from src.jobs.live_reaper import _summary_path, last_summary

            _sp = _summary_path(db.db_path)
            try:
                with open(_sp, "r", encoding="utf-8") as _fh:
                    reaper = _json.load(_fh)
            except Exception:  # noqa: BLE001 - no pass yet
                reaper = last_summary()
        except Exception:  # noqa: BLE001 - visibility only
            reaper = {}
        return jsonify(
            {
                "book": "live",
                "open": opens,
                "open_count": len(opens),
                "open_notional": round(sum(r["notional"] for r in opens), 2),
                "recent_closes": closes,
                "closed_count": len(closes),
                "closed_total": int(totals.get("n") or 0),
                "net_pnl": round(float(totals.get("net") or 0.0), 2),
                "fee_total": round(float(totals.get("fees") or 0.0), 2),
                "wins_total": int(totals.get("wins") or 0),
                "reaper": reaper,
            }
        )
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
            last_ping = time.time()
            while True:
                try:
                    msg = q.popleft()
                    yield msg
                    last_ping = time.time()
                except IndexError:
                    # Keep idle connections alive through proxies that reap
                    # quiet streams; a comment is ignored by EventSource.
                    if time.time() - last_ping >= 25.0:
                        yield ":ping\n\n"
                        last_ping = time.time()
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


def _runtime_mode() -> Optional[str]:
    """The runtime store's mode word for the book currently in force.

    DRY and LIVE are separate books with separate runtime rows. Every read and
    write against strategy_runtime names ONE book: 'paper' while the page
    reads DRY, 'live' while it reads LIVE. This is what makes Stop on the LIVE
    page touch only the LIVE row - the DRY book keeps its own state.
    None means the book could not be read; callers must not act on a guess.
    """
    book = _current_book_mode()
    if book is None:
        return None
    return "live" if book == "live" else "paper"


def _runtime_key(name: str) -> Optional[str]:
    from src.utils.strategy_runtime import key as _rt_key

    mode = _runtime_mode()
    return None if mode is None else _rt_key(name, mode)


def _recorded_state() -> Dict[str, Dict[str, Any]]:
    """Persisted strategy state, reconciled against the OS.

    This is the source of truth for the Start/Stop pills. In-memory
    `strategy_state` alone produced a page that said "stopped" for strategies
    that were running, because the flag was written by whichever request started
    the process and never learned what happened afterwards.

    The state shown is THIS BOOK's state only: each (strategy, book) pair has
    its own runtime row, so the LIVE page reports the LIVE row and never reads
    a stop the operator gave to the DRY book.

    Liveness is decided by `owns_process`, not by a bare pid check. A pid recorded
    before a redeploy names some other process now, and treating that stranger as
    a live strategy made the dashboard report running bots that did not exist and
    sent Stop's SIGTERM to an innocent process.
    """
    from src.utils.strategy_runtime import INSTANCE, owns_process

    recorded = _run_async(_runtime_store().snapshot())
    for name, st in strategy_state.items():
        row = recorded.get(_runtime_key(name)) or {}
        # Constant live proof: a heartbeat is stamped by the strategy process
        # itself every cycle. "running" is not a pid that exists - a recycled
        # pid, or a zombie, has a pid - it is a process whose own loop executed
        # recently. A strategy that stopped heartbeating while its pid still
        # exists is stuck, and is reported as such.
        hb = row.get("heartbeat_at")
        heartbeat_age: Optional[float] = None
        if hb:
            try:
                heartbeat_age = (datetime.now() - datetime.fromisoformat(str(hb))).total_seconds()
            except Exception:  # noqa: BLE001 - a malformed stamp is treated as none
                heartbeat_age = None
        st["heartbeat_at"] = hb
        st["heartbeat_age_sec"] = round(heartbeat_age, 1) if heartbeat_age is not None else None

        if row.get("pid") and not _pid_alive(row["pid"], row):
            # Died without us being told. Say what actually happened rather than
            # implying it chose to leave: a pid from a previous instance of the
            # app is stale by definition, not a strategy that exited on its own.
            if (row.get("instance") or "") != INSTANCE:
                reason = "stopped by app restart"
            else:
                reason = "exited on its own"
            # A stop is a WRITE into one book's row; an unknown book is never
            # written. Skip this pass rather than guess.
            _book = _runtime_mode()
            if _book is not None:
                _run_async(
                    _runtime_store().record_stop(name, reason, _book, clear_desired=False)
                )
            st["pid"] = None
            st["running"] = False
            st["stuck"] = False
            st["stop_reason"] = reason
            continue
        pid_ok = bool(row.get("pid"))
        # Strategies that do not write heartbeats yet keep the plain pid test.
        hb_stale = heartbeat_age is not None and heartbeat_age > _HEARTBEAT_STALE_SECONDS
        st["pid"] = row.get("pid") if row.get("pid") else None
        st["running"] = pid_ok and not hb_stale
        st["stuck"] = pid_ok and hb_stale
        st["started_at"] = row.get("started_at")
        st["command"] = row.get("command")
        st["stop_reason"] = row.get("stop_reason") or ""
    return cast(Dict[str, Dict[str, Any]], recorded)


def _pid_alive(pid, row=None):
    """True if the strategy process recorded in `row` is still running.

    Delegates to the runtime store so the check is platform-safe: on Windows
    os.kill(pid, 0) would terminate the process being probed, and the Popen
    handle only exists in the process that spawned it. When the originating row
    is supplied the check is exact - pid plus the instance and kernel start-time
    that were recorded with it - so a recycled pid cannot pass as a live bot.
    """
    from src.utils.strategy_runtime import owns_process
    from src.utils.strategy_runtime import pid_alive as safe_pid_alive

    if not pid:
        return False
    proc = _child_procs.get(pid)
    if proc is not None:
        # Authoritative when we own the handle.
        return proc.poll() is None
    if row is not None:
        return owns_process(row)
    return safe_pid_alive(pid)


def _running_strategies():
    """Names of strategies whose process is actually alive."""
    _recorded_state()
    return [
        name
        for name, st in strategy_state.items()
        if st.get("running") and _pid_alive(st.get("pid"))
    ]


@app.route("/api/llm/usage", methods=["GET"])
def api_llm_usage():
    """Which consumers are eating the OpenRouter key, itemised.

    Read-only. Answers from llm_queries - the forever log of every LLM call -
    grouped by (strategy, query_type) with counts, tokens and tracked cost, so
    the burn is attributable instead of one mystery number.
    """
    rows = _db_rows(_SQL_LLM_USAGE)
    total_cost = round(sum(_as_float(r.get("cost")) for r in rows), 4)
    total_tokens = int(sum(int(r.get("tokens") or 0) for r in rows))
    total_calls = int(sum(int(r.get("n") or 0) for r in rows))
    out = []
    for r in rows:
        out.append(
            {
                "strategy": r.get("strategy") or "unknown",
                "query_type": r.get("query_type") or "unknown",
                "calls": int(r.get("n") or 0),
                "tokens": int(r.get("tokens") or 0),
                "cost": round(_as_float(r.get("cost")), 4),
                "last_at": str(r.get("last_at") or "")[:19],
            }
        )
    return jsonify(
        {
            "total_calls": total_calls,
            "total_tokens": total_tokens,
            "total_cost": total_cost,
            "consumers": out,
        }
    )


@app.route("/api/analysis", methods=["GET"])
def api_analysis():
    """Stats and recommendations over EVERY closed trade in the current book.

    Read-only and unauthenticated. The trade log is the forever database, so
    this answers from the account's complete history, not a window.
    """
    from src.utils.trade_analytics import analyze_trades

    book = _current_book_mode()
    rows = _db_rows(_SQL_ANALYSIS.format(book=_book_filter(book)))
    return jsonify(analyze_trades(rows, name=None))


@app.route("/api/strategy/<name>/analysis", methods=["GET"])
def api_strategy_analysis(name):
    """Stats and recommendations over one strategy's entire closed history."""
    from src.utils.trade_analytics import analyze_trades

    if name not in strategy_state:
        return jsonify({"error": f"Unknown strategy: {name}"}), 404
    book = _current_book_mode()
    rows = _db_rows(_SQL_ANALYSIS_STRAT.format(book=_book_filter(book)), (name,))
    return jsonify(analyze_trades(rows, name=name))


@app.route("/api/equity-report", methods=["GET"])
def api_equity_report():
    """DRY vs LIVE equity curves and divergence alerts.
    
    Recommendation #10: Real-time equity tracking across both books with
    divergence detection. Returns equity history, current balances, and alerts.
    """
    try:
        from src.jobs.equity_tracker import EquityTracker
        
        tracker = EquityTracker(_db_conn())
        dry_equity = tracker.get_equity_curve("DRY")
        live_equity = tracker.get_equity_curve("LIVE")
        alerts = tracker.get_divergence_alerts()
        
        return jsonify({
            "dry_curve": dry_equity,
            "live_curve": live_equity,
            "alerts": alerts,
            "timestamp": datetime.utcnow().isoformat()
        })
    except Exception as e:
        return jsonify({"error": str(e), "alert": "Equity tracker not yet initialized"}), 200


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

    # Same LIVE truth rule as the main page: only rows Kalshi still holds.
    detail_positions = _in_book(_mine(pos_r))
    if book == "live":
        detail_positions = _truth_positions_for_live(
            [
                {
                    "market_id": r.get("market_id"),
                    "side": r.get("side"),
                    "entry_price": r.get("entry_price"),
                    "quantity": r.get("quantity"),
                    "stop_loss": r.get("stop_loss_price"),
                    "take_profit": r.get("take_profit_price"),
                    "timestamp": r.get("timestamp"),
                }
                for r in detail_positions
            ],
            _kalshi_account(),
        )

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
                    "stop_loss": r.get("stop_loss"),
                    "take_profit": r.get("take_profit"),
                    "opened": str(r.get("timestamp") or "")[:19],
                }
                for r in detail_positions
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

    # LAW + cost guard: heavy API abusers stay OFF until the operator

    #
    # PERMANENT GUARD: THE OPERATOR'S BUTTON IS LAW. DRY and LIVE are separate
    # forever. If the book mode read fails, refuse the operation rather than
    # guess. A guessed mode is THE EXACT BUG that armed LIVE when DRY was
    # pushed. Every other strategy start/stop must obey this same law.
    #
    book_mode_read = _current_book_mode()
    if book_mode_read is None:
        return (
            jsonify(
                {
                    "error": (
                        "CRITICAL: Cannot determine which book (DRY/LIVE) you are in right now. "
                        "The mode read failed. Refusing the toggle to prevent the LIVE strategy "
                        "from being armed by a DRY button push. Retry in a moment."
                    )
                }
            ),
            503,
        )
    # deliberately re-enables them in code. The page button already pops an
    # explanation; the API enforces the same rule so a direct POST or bulk
    # Start cannot resurrect the lane behind the operator's back. Stops are
    # always allowed.
    if name in HEAVY_API_ABUSERS:
        try:
            _rec = _run_async(_runtime_store().snapshot())
            _row = _rec.get(_runtime_key(name)) or {}
            _pid = _row.get("pid")
            _running = bool(_pid) and _pid_alive(_pid, _row)
        except Exception:  # noqa: BLE001
            _running = False
        if not _running:
            return (
                jsonify(
                    {
                        "error": (
                            f"{name} is locked OFF as a heavy API abuser: "
                            f"{HEAVY_API_ABUSERS.get(name, '')} Re-enable it in "
                            f"code if you accept the LLM burn."
                        )
                    }
                ),
                403,
            )

    st = strategy_state[name]
    # The persisted book mode is the SOURCE OF TRUTH for which book we are in.
    # A button pressed while the switch reads DRY cannot spawn a LIVE process,
    # and vice versa.
    #
    # Two vocabularies meet here and they are NOT the same words. The book is
    # "dry" or "live"; the strategy runtime store records "paper" or "live".
    # The UI sends the runtime word ("paper"). Comparing the request against the
    # book word directly meant `requested="paper"` never equalled
    # `persisted="dry"`, so EVERY DRY start was refused with a 400 mode
    # mismatch - which is why no strategy would ever start on the DRY page.
    # Both sides are therefore normalised onto the runtime vocabulary first.
    book_mode = _current_book_mode()
    if book_mode is None:
        # THE LAW: never act on a guessed book. A failed mode read used to
        # fall back to 'dry' - or worse, race the real value - and a start
        # meant for one book could arm the other. Refuse instead.
        return (
            jsonify(
                {
                    "error": (
                        "Trading book is unknown right now (mode read failed). "
                        "Refusing to start/stop so the wrong book can never be "
                        "armed - retry in a moment."
                    )
                }
            ),
            503,
        )
    runtime_mode = "live" if book_mode == "live" else "paper"

    raw_requested = (request.json or {}).get("mode")
    requested_mode: Optional[str] = None
    if raw_requested is not None:
        requested_mode = str(raw_requested).strip().lower()
        # Accept the book word as a synonym so both callers work.
        if requested_mode == "dry":
            requested_mode = "paper"
        elif requested_mode not in ("paper", "live"):
            return jsonify({"error": "mode must be 'paper' or 'live'"}), 400

    # With no mode from the caller, follow the book we are actually in.
    mode = requested_mode or runtime_mode
    if mode == "live" and not os.environ.get("KALSHI_API_KEY"):
        return jsonify({"error": "Live mode needs KALSHI_API_KEY configured"}), 400

    store = _runtime_store()

    # ===== TRIPLE VERIFICATION BEFORE ANY START/STOP ACTION =====
    # Verification 1: Book mode read must succeed (already checked above)
    # Verification 2: Check recorded mode matches current book if row exists
    recorded = _run_async(store.snapshot())
    this_runtime_key = _runtime_key(name)
    recorded_row = recorded.get(this_runtime_key) or {}
    recorded_mode = recorded_row.get("mode")
    
    # If a row exists, it MUST match the current book mode
    if recorded_row and recorded_mode:
        expected_runtime_mode = "live" if book_mode == "live" else "paper"
        if recorded_mode != expected_runtime_mode:
            return (
                jsonify({
                    "error": (
                        f"BOOK MISMATCH DETECTED: {name} is recorded as {recorded_mode} "
                        f"but you are in {book_mode}. This prevents the cross-contamination "
                        f"bug. Please try again or contact support."
                    )
                }),
                409,
            )
    
    # Verification 3: If running, verify it's in the right book
    db_pid = recorded_row.get("pid")
    db_mode = recorded_row.get("mode")
    if db_pid and _pid_alive(db_pid, recorded_row):
        runtime_book = "live" if db_mode == "live" else "dry"
        if runtime_book != book_mode:
            return (
                jsonify({
                    "error": (
                        f"RUNNING IN WRONG BOOK: {name} is alive in {runtime_book} "
                        f"but you clicked the button in {book_mode}. Killing it now to prevent contamination."
                    )
                }),
                409,
            )

    # Check the DATABASE for the current state — not just the in-memory dict.
    # strategy_state is per-process and can be stale across gunicorn workers.
    # The database is the shared source of truth. THE LOOKUP IS BOOK-SCOPED:
    # the LIVE page reads the LIVE row and the DRY page reads the DRY row, so
    # Start/Stop here can never touch the other book's process or intent.
    recorded = _run_async(store.snapshot())
    db_row = recorded.get(_runtime_key(name)) or {}
    db_pid = db_row.get("pid")
    db_mode = db_row.get("mode")
    # Exact check, not a bare pid: see `_pid_alive`. Without this a pid left over
    # from a previous instance of the app reads as a live strategy, and Stop then
    # signals an unrelated process instead of a bot.
    db_running = bool(db_pid) and _pid_alive(db_pid, db_row)

    # RUNNING IN THIS BOOK = a deliberate Stop, scoped to THIS BOOK. The other
    # book's row - its pid, its desired flag - is never touched.
    if db_running:
        code = _stop_child({"pid": db_pid, "running": True})
        _run_async(store.record_stop(name, "stopped by operator", mode))
        _run_async(store.set_desired(name, False, mode))
        _recorded_state()
        _broadcast("strategy", {"name": name, "action": "stopped"})
        return jsonify(
            {
                "name": name,
                "running": False,
                "exit_code": code,
                "stopped_from_book": book_mode,
                "was_running_in": db_mode,
            }
        )

    # LAW: the operator's last button push is permanent truth. This point is
    # reached only when the DB says the lane is NOT running, so this is a
    # START request. Never clear intent here: a Start that fails validation
    # (mode mismatch, missing creds, spawn error) must leave the previous
    # intent exactly as it was, not flip a lane the operator turned ON into
    # OFF-forever. _spawn_strategy records desired=True on success; the Stop
    # path above (db_running) is the only place desired=False is written.

    if requested_mode is not None and requested_mode != runtime_mode:
        # Starting is gated on the book: the page controls the book it shows.
        return (
            jsonify(
                {
                    "error": f"Mode mismatch: request asked for '{requested_mode}' but the "
                    f"current book is '{book_mode}'. Cannot start a strategy in a different book."
                }
            ),
            400,
        )

    # With no mode supplied by the caller, follow the book we are actually in.
    mode = requested_mode or runtime_mode

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

    try:
        result = _spawn_strategy(name, mode)
        # PERMANENT ASSERTION: The book we read must match the mode we spawned.
        # If they differ, it means a guessed/stale book mode snuck through and the
        # wrong strategy is now running. This is THE BUG the operator complained about.
        actual_book = _current_book_mode()
        expected_book = "dry" if mode == "paper" else mode
        if actual_book != expected_book:
            _stop_child({"pid": result["pid"], "running": True})
            _push_error(
                f"CRITICAL BUG PREVENTED: Spawned {name} in {mode} but book reads as "
                f"{actual_book} now! Killing the process immediately. This is the exact "
                f"DRY/LIVE cross-contamination bug - refusing it by assertion."
            )
            return (
                jsonify(
                    {
                        "error": (
                            "CRITICAL: Book mode changed between reading it and spawning. "
                            "The strategy was NOT started to prevent the LIVE strategy from "
                            "being armed by a DRY button push. Retry in a moment."
                        )
                    }
                ),
                503,
            )
    except Exception as e:  # noqa: BLE001
        _push_error(f"Strategy start ({name}): {e}")
        return jsonify({"error": f"Failed to start: {e}"}), 500

    _broadcast("strategy", {"name": name, "action": "started", "pid": result["pid"]})
    return jsonify(result)


def _spawn_strategy(name: str, mode: str) -> Dict[str, Any]:
    """Launch one strategy process and record it. Raises on failure.

    Shared by the Start button and the supervisor so both paths produce an
    identical child: same command, same environment, same persisted record.
    """
    store = _runtime_store()

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
    
    # CRITICAL: Isolate book mode environment variables to prevent cross-contamination.
    # Remove any stale TRADING_MODE or LIVE_TRADING_ENABLED from parent process.
    # This ensures child inherits only the mode we explicitly set.
    child_env.pop("TRADING_MODE", None)
    child_env.pop("LIVE_TRADING_ENABLED", None)
    # Set explicit mode for this process (redundant but belt-and-suspenders)
    child_env["STRATEGY_BOOK_MODE"] = mode

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = open(LOG_DIR / f"strategy_{name}.log", "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=child_env,
            start_new_session=True,
        )
    finally:
        out.close()

    _child_procs[proc.pid] = proc
    st = strategy_state.setdefault(name, {"running": False, "pid": None, "mode": mode})
    st["running"] = True
    st["pid"] = proc.pid
    st["mode"] = mode
    st["stop_reason"] = ""
    # Persist before answering: the next request may be served by a different
    # process, and a button that reports "started" for a process nobody can see
    # is exactly the lie this store exists to remove.
    #
    # This used to be fire-and-forget. If the write failed - and under WAL with
    # six strategies plus the dashboard writing, "database is locked" was
    # routine - the exception escaped with the child already running and
    # unrecorded. The dashboard then read the *previous* row, saw a pid from a
    # previous instance, and reported "stopped by app restart" for a strategy
    # that was very much alive and trading. Pressing Start again would then
    # spawn a second copy of the same strategy.
    #
    # So: retry briefly, and if it still cannot be recorded, kill the child. An
    # untracked trading process is worse than no process.
    last_error: Optional[Exception] = None
    for attempt in range(4):
        try:
            _run_async(store.record_start(name, proc.pid, mode, " ".join(cmd[1:]), desired=True))
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.4 * (attempt + 1))
    if last_error is not None:
        _child_procs.pop(proc.pid, None)
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        raise RuntimeError(
            f"started {name} but could not record it ({last_error}); "
            f"the process was stopped rather than left untracked"
        ) from last_error
    return {"name": name, "running": True, "pid": proc.pid}


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
        # A dead feed used to be indistinguishable from a feed that had simply
        # not ticked yet: both rendered "connecting...". Surfacing the reason
        # and the age of the feed is the difference between a visible fault and
        # a silent one.
        "feed_error": dashboard_state.get("market_error") or "",
        "feed_started_at": dashboard_state.get("market_started_at"),
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
<link rel="icon" id="favicon" href="data:image/svg+xml,{{ '<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 32 32%22><rect width=%2232%22 height=%2232%22 rx=%228%22 fill=%22%23a30808%22/><text x=%2216%22 y=%2223%22 font-size=%2219%22 font-weight=%22bold%22 text-anchor=%22middle%22 fill=%22%23000%22>L</text></svg>' if s.mode.mode == 'live' else '<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 32 32%22><rect width=%2232%22 height=%2232%22 rx=%228%22 fill=%22%232ee6a8%22/><text x=%2216%22 y=%2223%22 font-size=%2219%22 font-weight=%22bold%22 text-anchor=%22middle%22 fill=%22%23000%22>D</text></svg>' }}">
<style>
:root{
  --bg:#080b12; --panel:#0f1420; --panel2:#141b2a; --line:#1f2937; --line2:#2b3648;
  --fg:#e6edf6; --dim:#b8cce0; --faint:#5b6a80;
  --up:#2ee6a8; --down:#a10000; --live:#8a0303; --live-bg:rgba(138,3,3,.20); --blue:#4d9fff; --amber:#ffb454; --violet:#a78bfa;
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

/* ---------- header ----------
   ONE row, left to right: logo + wordmark | mode flag + DRY/LIVE | host | stamp.
   The stamp is pushed hard right with margin-left:auto so it stays the last
   thing on the line even when the middle cells resize. The mode control sits
   immediately right of the wordmark rather than in its own stacked column, so
   the name, the current state and the control that changes it read as one
   unit. */
header{
  display:flex;flex-wrap:nowrap;align-items:center;gap:18px;
  margin-bottom:22px;min-width:0;
}
.brand{display:flex;align-items:center;gap:12px;min-width:0;flex:0 1 auto}
.brandtext{min-width:0}
.logo{
  width:42px;height:42px;border-radius:12px;display:grid;place-items:center;flex:none;
  background:linear-gradient(160deg,#1c0707,#0c0303 70%);
  border:1px solid rgba(255,59,31,.35);
  box-shadow:0 0 0 1px rgba(0,0,0,.55), 0 6px 20px rgba(161,0,0,.35), inset 0 0 14px rgba(161,0,0,.22);
}
h1{font-size:20px;font-weight:650;letter-spacing:-.2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
h1 span{color:var(--dim);font-weight:400}
.sub{color:var(--faint);font-size:12px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* Flag and switch on one row, flag to the left of the buttons. */
.moderow{display:flex;align-items:center;gap:10px;flex:none}
.hostcell{flex:none;min-width:0}
.stamp{margin-left:auto;flex:none;font-size:11px;color:var(--faint);white-space:nowrap}

/* Narrow viewports cannot hold seven things on one line without truncating the
   mode control, which is the one element that must stay fully readable. Below
   this width the header is allowed to wrap, but it wraps into rows rather than
   columns so the wordmark keeps its content. */
@media (max-width:1180px){
  header{flex-wrap:wrap;row-gap:12px}
  .stamp{margin-left:0}
}

/* DRY / LIVE switch - the highest-stakes control on the page, so it is the
   most prominent thing in the header and colour-coded rather than subtle. */
.modeswitch{display:flex;gap:0;border-radius:10px;overflow:hidden;border:1px solid var(--line2)}
.modebtn{
  background:var(--panel);color:var(--faint);border:0;border-radius:0;
  padding:7px 18px;font-size:12px;font-weight:700;letter-spacing:.09em;cursor:pointer;
}
.modebtn:hover{background:rgba(255,255,255,.07)}
.modebtn.on{background:rgba(46,230,168,.16);color:var(--up);box-shadow:inset 0 -2px 0 var(--up)}
.modebtn.live.on{background:rgba(168,10,10,.18);color:var(--down);box-shadow:inset 0 -2px 0 var(--down)}
.modebtn:disabled{opacity:.5;cursor:not-allowed}
.note-box.live{background:rgba(168,10,10,.08);border-color:rgba(168,10,10,.28)}
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
  --mode-color:var(--down);--mode-glow:rgba(168,10,10,.55);
  animation:livepulse 1.4s ease-in-out infinite;
}
@keyframes livepulse{
  0%,100%{box-shadow:inset 0 0 22px rgba(168,10,10,.55)}
  50%    {box-shadow:inset 0 0 46px rgba(168,10,10,1)}
}
/* Redundant, non-colour signal so the state survives colour-blindness. */
body[data-mode="live"] .brand .logo{border-color:rgba(255,214,0,.45);box-shadow:0 0 0 1px rgba(0,0,0,.55), 0 6px 20px rgba(161,0,0,.45), 0 0 18px rgba(255,214,0,.15), inset 0 0 14px rgba(161,0,0,.28)}
body[data-mode="dry"]  .brand .logo{border-color:rgba(77,159,255,.4);box-shadow:0 0 0 1px rgba(0,0,0,.55), 0 6px 20px rgba(43,108,255,.35)}

.modeflag{
  position:relative;
  display:inline-flex;align-items:center;gap:9px;
  /* A darker red than the P&L red needs heavier lettering to hold its own
     against the panel: heavier weight, more size, slightly tighter tracking. */
  font-size:14px;font-weight:900;letter-spacing:.10em;
  padding:6px 15px;border-radius:10px;border:2px solid currentColor;
  box-shadow:0 0 0 1px rgba(0,0,0,.35), 0 4px 14px rgba(0,0,0,.35);
}
.modeflag.dry{color:var(--up);background:rgba(46,230,168,.12)}
.modeflag.dry {color:var(--up);background:rgba(46,230,168,.10)}
.modeflag.live{color:var(--live);background:var(--live-bg)}
.modeflag .dot{width:9px;height:9px}
/* LIVE flag: no blinking, no orbiting beacon. A brighter red fill, WHITE
   text, a thin yellow border, and the silver shimmer sweeping across. */
body[data-mode="live"] .modeflag.live{
  animation:none;
  border:1px solid #ffd600;color:#ffffff;
  background:linear-gradient(150deg,#ff1414,#c80000 55%,#8a0000);
  box-shadow:0 0 0 1px rgba(0,0,0,.5), 0 0 18px rgba(255,20,20,.65), 0 4px 16px rgba(0,0,0,.4);
}
body[data-mode="live"] .modeflag.live .dot{display:none}
.orbitring{display:none}
body[data-mode="live"] .modeflag.live::after{
  content:'';position:absolute;inset:0;border-radius:8px;pointer-events:none;overflow:hidden;
  background:linear-gradient(115deg,transparent 32%,rgba(235,240,250,.28) 47%,rgba(255,255,255,.5) 52%,transparent 68%);
  background-size:230% 100%;
  animation:shimmer 3s linear infinite;
}
@keyframes shimmer{0%{background-position:190% 0}100%{background-position:-40% 0}}
.url{
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:var(--blue);
  background:rgba(77,159,255,.09);border:1px solid rgba(77,159,255,.25);
  padding:3px 9px;border-radius:7px;cursor:pointer;transition:.15s;
}
.url:hover{background:rgba(77,159,255,.18)}
/* .stamp's layout properties (margin-left:auto, flex:none, white-space) are set
   with the rest of the header row rules further up; this block only keeps the
   type styling so the two definitions cannot fight. */
.stamp{color:var(--faint)}

/* ---------- tiles ---------- */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:10px;margin-bottom:18px}
.tile{
  background:linear-gradient(180deg,var(--panel) 0%,var(--panel2) 100%);
  border:1px solid var(--line);border-radius:var(--r);padding:14px 15px;box-shadow:var(--shadow);
  position:relative;overflow:hidden;
}
.tile::after{content:"";position:absolute;inset:0 0 auto 0;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.09),transparent)}
.tile .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;color:#ffdd00;margin-bottom:7px}
.tile .v{font-size:25px;font-weight:660;letter-spacing:-.6px;font-variant-numeric:tabular-nums;line-height:1.05}
.tile .s{font-size:12px;color:var(--dim);margin-top:4px}
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
.ph .note{font-size:11px;color:#ffdd00;font-weight:400;text-transform:none;letter-spacing:0}
.pb{padding:14px 16px;flex:1}

/* ---------- bits ---------- */
.pill{display:inline-flex;align-items:center;gap:5px;padding:2.5px 9px;border-radius:999px;font-size:10.5px;font-weight:620;letter-spacing:.02em}
.pill.ok{background:rgba(46,230,168,.13);color:var(--up)}
.pill.no{background:rgba(168,10,10,.13);color:var(--down)}
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
button.danger{background:rgba(168,10,10,.12);color:var(--down);border-color:rgba(168,10,10,.28)}
button.danger:hover{background:rgba(168,10,10,.24)}
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
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:12px}
.row.three{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px}
.card{
  background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:11px 12px;
  cursor:pointer;transition:border-color .15s,transform .15s;
}
.card:hover{border-color:var(--blue);transform:translateY(-1px)}
.card.hot{border-color:rgba(46,230,168,.45)}
.ctop{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.clabel{font-size:15px;font-weight:650}
.cname{font-size:11.5px;color:var(--dim);margin-top:1px}
.cproof{font-size:10px;color:var(--dim);margin-top:4px;min-height:12px}
.recs{list-style:none;padding:0;margin:0}
.recs li{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:7px 10px;margin:6px 0;font-size:12.5px}
.cchart{height:54px;margin:8px 0 6px}
/* Strategy cards get a taller curve than the compact feed cards - the per-card
   P&L line is the reason to open a card, and at 54px it was unreadable. */
.cards .cchart{height:104px;margin:10px 0 8px}
.cstats{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;text-align:center}
.cstats b{display:block;font-size:16px;font-weight:640}
.cstats span{font-size:10.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.04em}
.cfoot{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:9px;padding-top:8px;border-top:1px solid var(--line)}
.cfoot button{padding:3px 9px;font-size:11px}
code{background:var(--bg);border:1px solid var(--line2);border-radius:5px;padding:1px 5px;font-size:11px;font-family:ui-monospace,monospace}
footer{margin-top:22px;text-align:center;color:var(--faint);font-size:11px}

/* Strategy button loading animation: silvery circular effect */
@keyframes borderSpin {
  0% { border-image: conic-gradient(from 0deg, #c0c0c0 0deg, #e8e8e8 90deg, #c0c0c0 180deg, #a0a0a0 270deg, #c0c0c0 360deg) 1; }
  100% { border-image: conic-gradient(from 360deg, #c0c0c0 0deg, #e8e8e8 90deg, #c0c0c0 180deg, #a0a0a0 270deg, #c0c0c0 360deg) 1; }
}
button.loading {
  border: 2px solid;
  border-image-source: conic-gradient(from 0deg, #c0c0c0, #e8e8e8, #c0c0c0, #a0a0a0, #c0c0c0);
  border-image-slice: 1;
  animation: borderSpin 1.2s linear infinite;
  position: relative;
}
button.loading::after {
  content: '';
  position: absolute;
  top: -2px;
  left: -2px;
  right: -2px;
  bottom: -2px;
  border-radius: 3px;
  background: conic-gradient(from 0deg, rgba(192,192,192,0.3), rgba(232,232,232,0.6), transparent);
  animation: borderSpin 1.2s linear infinite;
  pointer-events: none;
  z-index: -1;
}
</style>
</head>
<body data-mode="{{ s.mode.mode }}">
<div class="wrap">

<header>
  <!-- One row: logo + wordmark, then the mode flag + DRY/LIVE switch, then the
       host, then the render stamp hard right. Everything used to sit in a
       right-hand column stacked three deep, which pushed the mode control away
       from the name and made the header two visual bands on a wide screen. -->
  <div class="brand">
    <div class="logo" aria-hidden="true">
      <img src="/static/logo.png" alt="Frigo" width="34" height="34" style="display:block">
    </div>
    <div class="brandtext">
      <h1>Kalshi-Frigo <span>&middot; {{ 'LIVE' if s.mode.mode == 'live' else 'DRY' }} trading dashboard</span></h1>
      <div class="sub">LLM-driven Kalshi automation &middot; paper &amp; live &middot; multi-strategy</div>
    </div>
  </div>
  <!-- State flag and switch share a row: the flag sits to the LEFT of the
       buttons, so the mode and the control that changes it read together
       instead of the flag floating above them. -->
  <div class="moderow">
    <!-- Unmistakable state flag. LIVE: brighter red fill, white text, thin
         yellow border, silver shimmer. No blinking, no orbiting beacon. -->
    <div id="modeFlag" class="modeflag {{ 'live' if s.mode.mode == 'live' else 'dry' }}">
      <span class="dot"></span>
      <span id="modeFlagText">{{ 'LIVE MODE' if s.mode.mode == 'live' else 'DRY MODE' }}</span>
    </div>
    <!-- DRY / LIVE switch -->
    <div class="modeswitch">
      <button id="modeDry"  class="modebtn {{ 'on' if s.mode.mode != 'live' else '' }}" onclick="setMode('dry')">DRY</button>
      <button id="modeLive" class="modebtn live {{ 'on' if s.mode.mode == 'live' else '' }}" onclick="setMode('live')">LIVE</button>
    </div>
  </div>
  <div class="hostcell">
    <div class="url" onclick="navigator.clipboard.writeText(location.href)" title="Click to copy this URL">{{ s.public_domain or 'localhost' }}</div>
  </div>
  <div class="stamp">Rendered {{ s.generated_at }}</div>
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
    <div class="s">live Kalshi account <span id="tBalanceAge">{% if s.kalshi_age_sec is not none and s.kalshi_age_sec >= 0 %}{% if s.kalshi_age_sec > 180 %}· STALE ({{ s.kalshi_age_sec // 60 }}m old){% else %}· updated {{ s.kalshi_age_sec }}s ago{% endif %}{% else %}· syncing…{% endif %}</span></div>
  </div>
  <div class="tile">
    <div class="k">Live positions</div>
    {#- Counts rows that actually hold contracts. Kalshi keeps a zero-share row
        per ticker the account ever touched, so the raw row count (31) is not a
        position count and reporting it as one made the account look like it had
        31 open bets when 3 were held. The zero-share rows are named below. -#}
    <div class="v" id="tLivePos">{{ s.kalshi.held_count if s.kalshi else 0 }}</div>
    <div class="s" id="tLivePosNote">{% if s.kalshi %}{{ s.kalshi.held_count }} held &middot; {{ s.kalshi.ghost_count }} flat rows on Kalshi's side (no shares, cannot be deleted){% else %}&mdash;{% endif %}</div>
  </div>
  <div class="tile">
    <div class="k">Real exposure</div>
    <div class="v" id="tExposure">{{ '$%.2f'|format(s.kalshi.exposure) if s.kalshi else '$0.00' }}</div>
    <div class="s" id="tExposureNote">{% if s.kalshi and s.kalshi.realized_known and s.kalshi.cost_basis_known %}mark-to-market &middot; cost {{ '$%.2f'|format(s.kalshi.cost_basis) }}{% elif s.kalshi and s.kalshi.realized_known %}mark-to-market &middot; cost unknown ({{ s.kalshi.cost_basis_missing }} holding{{ '' if s.kalshi.cost_basis_missing == 1 else 's' }} predate fill history){% else %}mark-to-market &middot; cost n/a{% endif %}</div>
  </div>
  <div class="tile">
    <div class="k">Real realized P&amp;L</div>
    {#- An unavailable total reads "n/a", never "$0.00". A zero here is
        indistinguishable from a break-even account, which is the one thing a
        realized-P&L tile must never be. -#}
    <div class="v {{ 'up' if s.kalshi and s.kalshi.realized_known and s.kalshi.realized > 0 else ('down' if s.kalshi and s.kalshi.realized_known and s.kalshi.realized < 0 else 'flat') }}" id="tKalshiPnl">{{ ('$%.2f'|format(s.kalshi.realized)) if s.kalshi and s.kalshi.realized_known else 'n/a' }}</div>
    {#- The breakdown is on the tile, not behind a click. A single -$329.58 with
        "fees $50.01" underneath invites the obvious suspicion that the number is
        wrong; showing that it is +$162.71 of sales against -$492.29 of
        settlements over Aug 1 - Oct 4 is what makes it checkable. -#}
    <div class="s" id="tKalshiPnlNote">{% if s.kalshi and s.kalshi.realized_known %}sales {{ '$%.2f'|format(s.kalshi.ledger.get('sales_realized', 0.0)) }} &middot; settled {{ '$%.2f'|format(s.kalshi.ledger.get('settlement_realized', 0.0)) }} &middot; fees {{ '$%.2f'|format(s.kalshi.fees) }}{% if s.kalshi.ledger.get('window_start') %} &middot; since {{ _format_date(s.kalshi.ledger.get('window_start')[:10]) }}{% endif %}{% else %}fill history unavailable{% endif %}</div>
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
    <div class="s">{{ s.trades.trades if s.trades else 0 }} closed trades &middot; bot log only, not account history</div>
  </div>
  <div class="tile">
    <div class="k">Win rate</div>
    <div class="v" id="tWinRate">{{ s.trades.win_rate if s.trades else 0 }}%</div>
    <div class="s" id="tWinRateNote">{{ s.trades.wins if s.trades else 0 }}W / {{ s.trades.losses if s.trades else 0 }}L</div>
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

<!-- ============ openrouter usage ============ -->
<div class="panel" style="margin-bottom:12px">
  <div class="ph" style="cursor:pointer" onclick="toggleLlmUsage()">
    <h2>OpenRouter key usage &mdash; what is actually consuming it</h2>
    <span class="note"><span id="llmToggle" style="cursor:pointer;font-weight:600">show</span> &middot; lifetime, never reset</span>
  </div>
  <div class="pb" id="llmUsageBodyWrap" style="display:none">
    <div class="cstats" style="grid-template-columns:repeat(auto-fit,minmax(100px,1fr));margin:0 0 12px">
      <div><b id="llmCalls">&hellip;</b><span>calls</span></div>
      <div><b id="llmTokens">&hellip;</b><span>tokens</span></div>
      <div><b id="llmCost" class="down">&hellip;</b><span>tracked cost</span></div>
      <div><b id="llmConsumers">&hellip;</b><span>consumers</span></div>
    </div>
    <div class="scroll">
    <table><thead><tr><th>Strategy</th><th>Query type</th><th class="num">Calls</th><th class="num">Tokens</th><th class="num">Tracked cost</th><th>Last call</th></tr></thead><tbody id="llmUsageBody">
      <tr><td colspan="6" class="empty">Loading&hellip;</td></tr>
    </tbody></table>
    </div>
    <p class="note" style="margin-top:10px;color:var(--faint);font-size:11px">
      "Tracked cost" is the cost the client computes per call; the "AI spend
      today" tile above resets every deploy and under-reports. This table is
      the lifetime log and does not reset.
    </p>
  </div>
</div>

<!-- ============ strategy cards ============ -->
<div class="panel" style="margin-bottom:12px">
  <div class="ph">
    <h2>Strategies</h2>
    <span class="note">click a card for everything about that strategy</span>
    <span class="bar">
      <button id="startAllBtn" onclick="startAll()">{{ 'Start all in LIVE' if s.mode.mode == 'live' else 'Start all in DRY' }}</button>
      <button onclick="stopAll()">Stop all</button>
      {%- if s.mode.mode == 'live' %}
      <button class="danger" onclick="stopAll()">Kill all LIVE</button>
      {%- endif %}
    </span>
  </div>
  {%- if s.mode.mode == 'live' %}
  <div class="pb" style="padding-top:0">
    <p class="note" style="font-size:12px;color:var(--live)">
      Switching the mode switch never starts a strategy &mdash; that places no
      order. Each book remembers YOUR LAST CLICK forever: a strategy you
      stopped stays stopped across redeploys and logins until you press Start
      here again, and a strategy you started keeps coming back from crashes
      automatically. Start/Stop is welded to your buttons, per book, for good.
    </p>
  </div>
  {%- endif %}
  <div class="pb">
    <div class="cards">
      {%- for c in s.strategy_cards %}
      <div class="card{{ ' hot' if c.running else '' }}" id="card-{{ c.name }}" onclick="openStrategy('{{ c.name }}')">
        <div class="ctop">
          <div>
            <div class="clabel">{{ c.label }}{% if c.heavy_api_abuser %} <span class="pill no" title="{{ c.heavy_api_abuser_reason }}">HEAVY API ABUSER</span>{% endif %}</div>
            <div class="mono cname">{{ c.name }}</div>
          </div>
          <span class="pill {{ 'warn' if c.stuck else ('ok' if c.running else 'no') }}" id="pill-{{ c.name }}">{% if c.stuck %}<span class="dot"></span>stuck{% elif c.running %}<span class="dot pulse"></span>running{% elif c.stop_reason %}stopped{% else %}not started{% endif %}</span>
        </div>
        <div class="cproof mono" id="hb-{{ c.name }}">{% if c.running and c.heartbeat_age_sec is not none %}proof: cycle {{ c.heartbeat_age_sec }}s ago{% elif c.stuck %}stuck: last cycle {{ c.heartbeat_age_sec }}s ago{% endif %}</div>
        <div class="cchart"><canvas id="spark-{{ c.name }}" height="104"></canvas></div>
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
            <button onclick="showAnalysis('{{ c.name }}')">ANALYSIS OF</button>
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

<!-- ============ feeds + account ============ -->
<!-- Sits directly under the strategy cards and above the DRY account. The feed
     tells you what the market is doing right now; the account tells you what the
     book did about it. That order reads better than burying the live picture
     under three panels of history. Collapsible because the numbers keep ticking
     and the panel is tall. -->
<div class="panel" id="feedsPanel" style="margin-bottom:12px">
    <div class="ph" style="cursor:pointer" onclick="toggleFeeds()">
      <h2>{{ 'LIVE feeds &amp; account — real money' if s.mode.mode == 'live' else 'DRY feeds &amp; account — simulated' }}</h2>
      <span class="note">
        <span id="feedToggle" style="cursor:pointer;font-weight:600">hide</span>
        &middot; {% if s.last_update %}synced {{ s.last_update }}{% else %}not synced yet{% endif %}
      </span>
    </div>
  <div id="feedsBody">
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
        never spent while the switch reads DRY.
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

{% if s.errors %}
<div class="panel" style="margin-bottom:12px">
  <div class="ph"><h2>Recent errors</h2><span class="note">{{ s.errors|length }} most recent</span></div>
  <div class="pb"><div class="bar">
    {%- for e in s.errors[-6:]|reverse %}<span class="pill warn"><span class="mono">{{ e.time }}</span> {{ e.error }}</span>{% endfor %}
  </div></div>
</div>
{% endif %}

{%- if live %}
<!-- ============ Kalshi account ============ -->
<!-- LIVE ONLY. This panel is real money: real positions, real P&L, real fees.
     Rendering it while the switch reads DRY put the production account's
     positions in the middle of a page whose every other number was simulated,
     which is precisely the DRY/LIVE blur this page is supposed to make
     impossible. A DRY page now contains no real-account figures at all. -->
<div class="panel" style="margin-bottom:12px">
    <div class="ph">
      <h2>Kalshi account — real, and being traded</h2>
      <span class="note">{% if s.last_update %}synced {{ s.last_update }}{% else %}not synced yet{% endif %}</span>
    </div>
  {%- if s.kalshi and s.kalshi.connected %}
  <div class="row two" style="padding:14px 16px 0;margin:0">
    <div class="pb" style="padding:0">
      <table><thead><tr><th>Market</th><th>Title</th><th class="num">Shares</th><th class="num">Exposure</th><th class="num">Traded</th><th class="num">Realized</th><th class="num">Fees</th></tr></thead><tbody>
      <tbody id="kalshiMarkets">
      {%- for r in s.kalshi.markets %}
        <tr>
          <td class="mono">{{ r.ticker }}</td>
          <td>{{ _market_title(r.ticker) }}</td>
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
      <table><thead><tr><th>Event</th><th>Title</th><th class="num">Shares</th><th class="num">Cost</th><th class="num">Exposure</th><th class="num">Realized</th><th class="num">Fees</th></tr></thead><tbody>
      <tbody id="kalshiEvents">
      {%- for r in s.kalshi.events %}
        <tr>
          <td class="mono">{{ r.ticker }}</td>
          <td>{{ _market_title(r.ticker) }}</td>
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
{%- endif %}

<!-- ============ positions + equity ============ -->
<div class="row two">
  <div class="panel">
    <div class="ph"><h2>Open positions</h2><span class="note">{{ s.positions|length }} row{{ '' if s.positions|length == 1 else 's' }}{% if live and s.stale_live_rows %} &middot; {{ s.stale_live_rows }} stale local row{{ '' if s.stale_live_rows == 1 else 's' }} settled on Kalshi, not shown{% endif %}</span></div>
    <div class="scroll">
      {%- if s.positions %}
      <table><thead><tr><th>Market</th><th>Title</th><th>Side</th><th class="num">Entry</th><th class="num">Qty</th><th>Strategy</th><th class="num">SL</th><th class="num">TP</th></tr></thead><tbody id="posBody">
      {%- for p in s.positions %}
        <tr>
          <td class="mono" title="{{ p.market_id }}">{{ p.market_id[:26] }}</td>
          <td>{{ _market_title(p.market_id) }}</td>
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
    <div class="ph"><h2>Recent closed trades</h2><span class="note">{{ s.recent_trades|length }} most recent &middot; every close is kept forever</span><span class="bar"><button onclick="showAnalysis()">ANALYSIS OF</button></span></div>
    {%- if s.recent_trades %}
    <div class="scroll">
    <table><thead><tr><th>Market</th><th>Title</th><th>Side</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">Qty</th><th class="num">P&amp;L</th><th>Strategy</th><th>Exited</th></tr></thead><tbody>
    {%- for t in s.recent_trades %}
      <tr>
        <td class="mono" title="{{ t.market_id }}">{{ t.market_id[:24] }}</td>
          <td>{{ _market_title(t.market_id) }}</td>
        <td>{{ t.side }}</td>
        <td class="num">{{ '%.3f'|format(t.entry_price) }}</td>
        <td class="num">{{ '%.3f'|format(t.exit_price) }}</td>
        <td class="num">{{ t.quantity }}</td>
        <td class="num {{ 'up' if t.pnl > 0 else ('down' if t.pnl < 0 else 'flat') }}">{{ t.pnl }}</td>
        <td><span class="tag">{{ t.strategy }}</span></td>
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

<!-- ============ trade analysis ============ -->
<div class="panel" id="analysisPanel" style="margin-bottom:12px;display:none">
  <div class="ph">
    <h2 id="analysisTitle">ANALYSIS OF</h2>
    <span class="note" id="analysisScope"></span>
    <span class="bar"><button onclick="closeAnalysis()">Close</button></span>
  </div>
  <div class="pb" id="analysisBody"><div class="empty">Loading analysis&hellip;</div></div>
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
// Event-driven tally: the server pushes a snapshot event the moment the
// Kalshi numbers move, so the page refetches exactly when there is
// something new. The interval below is fallback only.
es.addEventListener('snapshot', () => { refresh(); });
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

function mktTitle(ticker) {
  return (SNAPSHOT.market_titles || {})[ticker] || ticker || '';
}

function paint(s) {
  if (!s) return;
  const k = s.kalshi || {};

  const mk = k.markets || [], ev = k.events || [];
  const mkBody = document.getElementById('kalshiMarkets');
  const evBody = document.getElementById('kalshiEvents');
  if (mkBody) {
    mkBody.innerHTML = rows(mk.map(r =>
      '<td class="mono">' + esc(r.ticker) + '</td>' +
      '<td>' + esc(mktTitle(r.ticker)) + '</td>' +
      '<td class="num">' + num(r.shares) + '</td>' +
      '<td class="num">' + num(r.exposure) + '</td>' +
      '<td class="num">' + num(r.traded) + '</td>' +
      '<td class="num ' + sgn(r.realized) + '">' + num(r.realized) + '</td>' +
      '<td class="num" style="color:var(--faint)">' + num(r.fees) + '</td>'
    ), 'No market positions.', 7);
  }
  if (evBody) {
    evBody.innerHTML = rows(ev.map(r =>
      '<td class="mono">' + esc(r.event || r.ticker) + '</td>' +
      '<td>' + esc(mktTitle(r.event || r.ticker)) + '</td>' +
      '<td class="num">' + num(r.shares) + '</td>' +
      '<td class="num">' + num(r.cost) + '</td>' +
      '<td class="num">' + num(r.exposure) + '</td>' +
      '<td class="num ' + sgn(r.realized) + '">' + num(r.realized) + '</td>' +
      '<td class="num" style="color:var(--faint)">' + num(r.fees) + '</td>'
    ), 'No event positions.', 7);
  }

  const posBody = document.getElementById('posBody');
  if (posBody) {
    posBody.innerHTML = rows((s.positions || []).map(p =>
      '<td class="mono">' + esc(String(p.market_id || '?').slice(0, 26)) + '</td>' +
      '<td>' + esc(mktTitle(p.market_id)) + '</td>' +
      '<td><span class="tag">' + esc(p.side) + '</span></td>' +
      '<td class="num">' + (p.entry_price == null ? '-' : Number(p.entry_price).toFixed(3)) + '</td>' +
      '<td class="num">' + num(p.quantity) + '</td>' +
      '<td><span class="tag">' + esc(p.strategy) + '</span></td>' +
      '<td class="num">' + num(p.stop_loss) + '</td>' +
      '<td class="num">' + num(p.take_profit) + '</td>'
    ), 'No open positions.', 8);
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
    // Freshness truth under the balance: a number with no age is how a dead
    // $0.29 reads as the live truth forever. Over 3 minutes old means the
    // Kalshi sync is failing, and the errors panel says why.
    var ageEl = $('tBalanceAge');
    if (ageEl) {
      var age = s.kalshi_age_sec;
      if (age == null || age < 0) { ageEl.textContent = '· syncing…'; ageEl.style.color = ''; }
      else if (age > 180) { ageEl.textContent = '· STALE (' + Math.round(age / 60) + 'm old) — sync failing'; ageEl.style.color = '#ff5555'; }
      else { ageEl.textContent = '· updated ' + age + 's ago'; ageEl.style.color = ''; }
    }
    // Held contracts, not raw rows: Kalshi keeps a zero-share row per ticker the
    // account ever touched, and summing those reported 31 "positions" when 3
    // were held.
    const held = k.held_count != null
      ? k.held_count : (k.market_count || 0) + (k.event_count || 0);
    set('tLivePos', held);
    set('tLivePosNote',
          (k.held_count || 0) + ' held · ' + (k.ghost_count || 0) +
          ' flat rows on Kalshi\'s side (no shares, cannot be deleted)');
    set('tExposure', money(k.exposure));
    set('tExposureNote',
      'mark-to-market · ' + (k.cost_basis_known
        ? 'cost ' + money(k.cost_basis)
        : (k.realized_known
          ? 'cost unknown (' + (k.cost_basis_missing || 0) + ' holding' +
            (k.cost_basis_missing === 1 ? '' : 's') + ' predate fill history)'
          : 'cost n/a')));
    set('tKalshiPnl', money(k.realized));
    const kr = document.getElementById('tKalshiPnl');
    if (kr) kr.className = 'v ' + sgn(k.realized || 0);
    const led = k.ledger || {};
    set('tKalshiPnlNote',
      'sales ' + money(led.sales_realized) +
      ' · settled ' + money(led.settlement_realized) +
      ' · fees ' + money(k.fees) +
      (led.window_start ? ' · since ' + String(led.window_start).slice(0, 10) : ''));
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
  set('tWinRateNote', (t.wins || 0) + 'W / ' + (t.losses || 0) + 'L');
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
      // Constant live proof: running means the strategy's own process stamped
      // a heartbeat this cycle - not merely that a pid exists. A stuck
      // strategy (pid alive, no recent heartbeat) is named as such.
      if (c.stuck) {
        pill.className = 'pill warn';
        pill.innerHTML = '<span class="dot"></span>stuck';
      } else {
        pill.className = 'pill ' + (c.running ? 'ok' : 'no');
        pill.innerHTML = c.running
          ? '<span class="dot pulse"></span>running'
          : (c.stop_reason ? 'stopped' : 'not started');
      }
    }
    const hb = $('hb-' + c.name);
    if (hb) {
      hb.textContent = c.running && c.heartbeat_age_sec != null
        ? ('proof: cycle ' + c.heartbeat_age_sec + 's ago')
        : (c.stuck ? ('stuck: last cycle ' + c.heartbeat_age_sec + 's ago') : '');
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
  try { refreshLlmUsage(); } catch (_) {}
}

// ---------------------------------------------------------------------------
// OpenRouter usage - who is eating the key, itemised from the lifetime log
// ---------------------------------------------------------------------------
let llmUsageReq = 0;

function toggleLlmUsage() {
  const wrap = $('llmUsageBodyWrap');
  const t = $('llmToggle');
  if (!wrap || !t) return;
  if (wrap.style.display === 'none') {
    wrap.style.display = '';
    t.textContent = 'hide';
    refreshLlmUsage();
  } else {
    wrap.style.display = 'none';
    t.textContent = 'show';
  }
}

async function refreshLlmUsage() {
  const wrap = $('llmUsageBodyWrap');
  if (wrap && wrap.style.display === 'none') return;
  const req = ++llmUsageReq;
  const r = await fetch('/api/llm/usage');
  const d = await r.json().catch(() => null);
  if (!d || req !== llmUsageReq) return;
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set('llmCalls', d.total_calls.toLocaleString());
  set('llmTokens', d.total_tokens.toLocaleString());
  const c = $('llmCost');
  if (c) c.textContent = '$' + d.total_cost.toFixed(2);
  set('llmConsumers', (d.consumers || []).length);
  const body = $('llmUsageBody');
  if (!body) return;
  if (!d.consumers || !d.consumers.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty">No LLM calls logged yet.</td></tr>';
    return;
  }
  body.innerHTML = d.consumers.map(x =>
    '<tr><td><span class="tag">' + esc(x.strategy) + '</span></td>'
    + '<td class="mono" style="color:var(--dim)">' + esc(x.query_type) + '</td>'
    + '<td class="num">' + x.calls.toLocaleString() + '</td>'
    + '<td class="num">' + x.tokens.toLocaleString() + '</td>'
    + '<td class="num down">$' + x.cost.toFixed(2) + '</td>'
    + '<td class="mono" style="color:var(--faint)">' + esc(x.last_at) + '</td></tr>'
  ).join('');
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
      // The request may have failed *after* the mode was already persisted, so
      // never repaint from the snapshot this page booted with - that stale object
      // says DRY, and trusting it is what threw the operator back to DRY after a
      // switch that had actually taken. Ask the server what mode is in force.
      note('mode switch blocked: ' + d.error);
      await syncModeFromServer('switch refused: ' + d.error);
      return;
    }
    note('trading mode is now ' + String(d.mode).toUpperCase());
    paintMode(d);
    flashBanner(d.mode === 'live'
      ? 'LIVE MODE - REAL ORDERS ARE NOW LEAVING THE ACCOUNT'
      : 'DRY MODE - simulated fills only, no real money', d.mode === 'live');
  } catch (e) {
    note('mode switch failed: ' + e.message);
    await syncModeFromServer('switch failed: ' + e.message);
  }
}

// Re-read the authoritative mode after any ambiguous switch attempt. SNAPSHOT is
// a rendering of the page as it was served; it cannot describe a mode change that
// happened after that.
async function syncModeFromServer(why) {
  try {
    const d = await fetch('/api/mode').then(r => r.json());
    const m = d.mode || 'dry';
    SNAPSHOT.mode = d;
    paintMode(d);
    flashBanner(
      'MODE IS ' + String(m).toUpperCase() + (why ? ' - ' + why : ''),
      m === 'live');
  } catch (_) {
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
  el.style.background = isLive ? '#a30808' : '#2ee6a8';
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
    const bg = live ? '%23a30808' : '%232ee6a8';
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
  const btn = document.getElementById('btn-' + name);
  const body = {}; // no mode: the server uses whatever the DRY/LIVE switch says
  
  // Add loading animation to button
  if (btn) btn.classList.add('loading');
  
  try {
    if (card && card.running) {
      await fetch('/api/strategy/' + encodeURIComponent(name) + '/toggle', {
        method: 'POST', headers: authHeaders(), body: JSON.stringify(body),
      });
      if (!quiet) note(name + ': stopped');
    } else {
      // HEAVY API ABUSER gate: quick_flip's Start is blocked with a popup so the
      // #1 OpenRouter spender stays off, in both books. The popup is the
      // reminder - it says exactly why the button does nothing.
      if (card && card.heavy_api_abuser) {
        if (!quiet) {
          alert('HEAVY API ABUSER\n\n' + (card.heavy_api_abuser_reason || '') +
            '\n\nThis strategy stays OFF. Not started.');
        }
        note(name + ': left off (HEAVY API ABUSER)');
        return;
      }
      const r = await fetch('/api/strategy/' + encodeURIComponent(name) + '/toggle', {
        method: 'POST', headers: authHeaders(), body: JSON.stringify(body),
      });
      const d = await r.json().catch(() => ({}));
      if (!quiet) note(d.error ? name + ': ' + d.error : name + ': ' + (d.running ? 'started' : 'stopped'));
    }
  } finally {
    // Remove loading animation after response returns
    if (btn) btn.classList.remove('loading');
    refresh();
  }
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
  if (typeof Chart === 'undefined') return;
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
      plugins: { legend: { labels: { color: '#e6edf6', boxWidth: 12, font: { size: 12 } } } },
      scales: {
        x: { ticks: { color: '#8494ab', maxTicksLimit: 5, font: { size: 11 } }, grid: { color: 'rgba(255,255,255,.06)' } },
        y: { ticks: { color: '#8494ab', font: { size: 11 } }, grid: { color: 'rgba(255,255,255,.06)' } },
      },
    }, opts || {}),
  };
  if (typeof Chart === 'undefined') return;
  if (feedCharts[id]) { feedCharts[id].destroy(); }
  feedCharts[id] = new Chart(ctx, cfg);
}
function hhmmss(t) { return new Date(t * 1000).toLocaleTimeString(); }

function paintMarket(md) {
  if (!md || !md.spot) return;
  var ctx_feed_error = {{ (m.feed_error | tojson) }};
  const spot = md.spot || {};

  // The two charts answer different questions, so they get different windows:
  // spot is a day of context ("where is BTC today"), the 15-minute contract is
  // a quarter-hour ("where is it versus this contract's target"). One shared
  // window made both useless - the contract chart spent most of its width on
  // history belonging to contracts that had already settled.
  const SPOT_WINDOW = 86400;   // 1 day
  const K15_WINDOW = 900;      // 15 minutes
  const nowS = Date.now() / 1000;
  const within = function (pts, secs) {
    return (pts || []).filter(function (p) { return nowS - p.t <= secs; });
  };
  // Green when the visible window closed higher, red when lower. This is the
  // convention every price chart uses; a fixed amber line made direction
  // unreadable at a glance, which is the one thing a price chart must convey.
  const trend = function (vals) {
    const clean = (vals || []).filter(function (v) { return v != null; });
    if (clean.length < 2) return { line: '#8fa3bd', fill: 'rgba(143,163,189,.10)' };
    const rising = clean[clean.length - 1] >= clean[0];
    return rising
      ? { line: '#2ee6a8', fill: 'rgba(46,230,168,.12)' }
      : { line: '#a10000', fill: 'rgba(255,45,32,.12)' };
  };

  const pts = within(spot.points, SPOT_WINDOW);
  $('spotSource').textContent = spot.source || 'connecting...';
  $('spotAge').textContent = spot.price
    ? '$' + Number(spot.price).toLocaleString() + '  ·  ' + (spot.age_sec != null ? spot.age_sec.toFixed(1) + 's old' : '')
    : 'no tick yet';
  const pill = $('spotPill');
  if (pill) {
    pill.className = 'pill ' + (spot.fresh ? 'ok' : 'warn');
    pill.textContent = spot.fresh ? 'live' : (spot.price ? 'stale' : 'waiting');
  }
  const spotTrend = trend(pts.map(function (p) { return p.p; }));
  drawFeed('spotChart', pts.map(p => hhmmss(p.t)), [{
    label: 'BTC-USD spot · 1 day', borderColor: spotTrend.line, backgroundColor: spotTrend.fill,
    data: pts.map(p => p.p), tension: 0.2, fill: true, pointRadius: 0, borderWidth: 2,
  }]);

  const k = md.kalshi || {};
  const kpts = within(md.series || [], K15_WINDOW);
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
  if (!contract && ctx_feed_error) {
    $('k15Detail').textContent = 'feed offline — ' + ctx_feed_error;
  }
  const kTrend = trend(kpts.map(function (p) { return p.spot; }));
  drawFeed('k15Chart', kpts.map(p => hhmmss(p.t)), [
    {
      label: 'spot · 15 min', borderColor: kTrend.line, backgroundColor: kTrend.fill,
      data: kpts.map(p => p.spot), tension: 0.2, pointRadius: 0, borderWidth: 2, fill: true,
    },
    {
      label: 'target', borderColor: '#8fa3bd', borderDash: [3, 3],
      data: kpts.map(p => p.target), tension: 0, pointRadius: 0, borderWidth: 1.2,
    },
  ], { scales: { x: { ticks: { color: '#8494ab', maxTicksLimit: 4, font: { size: 11 } } },
                  y: { ticks: { color: '#8494ab', font: { size: 11 } },
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
    borderColor: (a.today && a.today.pnl > 0) ? '#2ee6a8' : ((a.today && a.today.pnl < 0) ? '#a30808' : '#4d9fff'),
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
    if (typeof Chart === 'undefined') return;
    if (sparks[c.name]) sparks[c.name].destroy();
    const flat = !(c.equity && c.equity.pnl.length);
    sparks[c.name] = new Chart(ctx, {
      type: 'line',
      data: {
        labels: flat ? ['no closed trades yet'] : c.equity.labels,
        datasets: [{
          label: 'Cumulative P&L ($)',
          data: flat ? [0] : c.equity.pnl,
          borderColor: c.realized > 0 ? '#2ee6a8' : (c.realized < 0 ? '#a30808' : '#4d9fff'),
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
      y: { ticks: { color: '#8494ab', maxTicksLimit: 3, font: { size: 9 } }, grid: { color: 'rgba(255,255,255,.06)' } },
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
    ? '<table><thead><tr><th>Market</th><th>Title</th><th>Side</th><th>Entry</th><th>Qty</th><th>Stop</th><th>Target</th><th>Opened</th></tr></thead><tbody>'
      + d.positions.map(p => '<tr>' + cell('', p.market_id, 'mono') + cell('', mktTitle(p.market_id)) + cell('', p.side) + cell('', money(p.entry_price))
        + cell('', p.quantity) + cell('', p.stop_loss == null ? '-' : money(p.stop_loss))
        + cell('', p.take_profit == null ? '-' : money(p.take_profit)) + cell('', p.opened, 'mono') + '</tr>').join('')
      + '</tbody></table>'
    : '<div class="empty">No open positions in this book.</div>';

  html += '<h3 style="font-size:12px;margin:14px 0 6px">Closed trades</h3>';
  html += d.trades.length
    ? '<table><thead><tr><th>Market</th><th>Title</th><th>Side</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&amp;L</th><th>Closed</th></tr></thead><tbody>'
      + d.trades.map(t => '<tr>' + cell('', t.market_id, 'mono') + cell('', mktTitle(t.market_id)) + cell('', t.side) + cell('', money(t.entry_price))
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
          borderColor: s.realized > 0 ? '#2ee6a8' : (s.realized < 0 ? '#a30808' : '#4d9fff'),
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

// ---------------------------------------------------------------------------
// ANALYSIS OF - stats and recommendations over the forever trade log
// ---------------------------------------------------------------------------
const analysisChart = { current: null };
let analysisReq = 0;

function closeAnalysis() {
  $('analysisPanel').style.display = 'none';
  if (analysisChart.current) { analysisChart.current.destroy(); analysisChart.current = null; }
}

function _anStat(label, value, cls) {
  return '<div><b class="' + (cls || '') + '">' + value + '</b><span>' + label + '</span></div>';
}

function _anTable(heads, rows) {
  if (!rows || !rows.length) return '';
  let h = '<table><thead><tr>' + heads.map(x => '<th>' + esc(x) + '</th>').join('') + '</tr></thead><tbody>';
  for (const r of rows) {
    h += '<tr>' + r.map(c => '<td' + (c.num ? ' class="num"' : '') + '>' + (c.raw ? c.v : esc(c.v)) + '</td>').join('') + '</tr>';
  }
  return h + '</tbody></table>';
}

async function showAnalysis(name) {
  const req = ++analysisReq;
  const panel = $('analysisPanel');
  panel.style.display = '';
  $('analysisTitle').textContent = 'ANALYSIS OF';
  $('analysisScope').textContent = name
    ? (name + ' - every closed trade, forever')
    : ('all strategies - every closed trade, forever');
  $('analysisBody').innerHTML = '<div class="empty">Loading analysis&hellip;</div>';
  panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  const url = name
    ? '/api/strategy/' + encodeURIComponent(name) + '/analysis'
    : '/api/analysis';
  const r = await fetch(url);
  const a = await r.json().catch(() => ({}));
  if (req !== analysisReq) return; // a newer analysis replaced this one
  if (a.error) {
    $('analysisBody').innerHTML = '<div class="empty">' + esc(a.error) + '</div>';
    return;
  }
  const pnlCls = a.total_pnl > 0 ? 'up' : (a.total_pnl < 0 ? 'down' : 'flat');
  let html = '<div class="cstats" style="grid-template-columns:repeat(auto-fit,minmax(92px,1fr))">'
    + _anStat('closed trades', a.trades)
    + _anStat('win rate', a.win_rate + '%')
    + _anStat('total P&L', money(a.total_pnl), pnlCls)
    + _anStat('avg P&L', money(a.avg_pnl), a.avg_pnl > 0 ? 'up' : (a.avg_pnl < 0 ? 'down' : 'flat'))
    + _anStat('best', money(a.best), 'up')
    + _anStat('worst', money(a.worst), 'down')
    + '</div>';

  html += '<h3 style="font-size:12px;margin:14px 0 6px">Recommendations</h3><ul class="recs">'
    + a.recommendations.map(x => '<li>' + esc(x) + '</li>').join('') + '</ul>';

  html += '<h3 style="font-size:12px;margin:14px 0 6px">By strategy</h3>'
    + _anTable(['Strategy', 'Trades', 'Win rate', 'P&L'],
      (a.by_strategy || []).map(r => [
        { v: r.strategy }, { v: r.trades, num: true },
        { v: r.win_rate + '%', num: true },
        { v: money(r.pnl), num: true, raw: '<span class="' + (r.pnl > 0 ? 'up' : (r.pnl < 0 ? 'down' : 'flat')) + '">' + money(r.pnl) + '</span>' },
      ]));

  html += '<div class="row two" style="padding:0;margin-top:8px">'
    + '<div><h3 style="font-size:12px;margin:0 0 6px">By side</h3>'
    + _anTable(['Side', 'Trades', 'Win rate', 'P&L'],
      (a.by_side || []).map(r => [
        { v: r.side }, { v: r.trades, num: true },
        { v: r.win_rate + '%', num: true },
        { v: money(r.pnl), num: true },
      ])) + '</div>'
    + '<div><h3 style="font-size:12px;margin:0 0 6px">By entry price</h3>'
    + _anTable(['Band', 'Trades', 'Win rate', 'P&L'],
      (a.by_price_band || []).map(r => [
        { v: r.band }, { v: r.trades, num: true },
        { v: r.win_rate + '%', num: true },
        { v: money(r.pnl), num: true },
      ])) + '</div></div>';

  if (a.by_hour && a.by_hour.length) {
    html += '<h3 style="font-size:12px;margin:14px 0 6px">By hour of exit</h3>'
      + _anTable(['Hour', 'Trades', 'Win rate', 'P&L'],
        a.by_hour.map(r => [
          { v: r.hour }, { v: r.trades, num: true },
          { v: r.win_rate + '%', num: true },
          { v: money(r.pnl), num: true },
        ]));
  }
  if (a.by_exit_reason && a.by_exit_reason.length) {
    html += '<h3 style="font-size:12px;margin:14px 0 6px">By exit reason</h3>'
      + _anTable(['Reason', 'Trades'], a.by_exit_reason.map(r => [{ v: r.reason }, { v: r.trades, num: true }]));
  }
  if (a.hold_seconds_avg_win || a.hold_seconds_avg_loss) {
    html += '<p class="note" style="margin-top:10px;color:var(--dim)">Average hold: winners '
      + Math.round(a.hold_seconds_avg_win / 60) + 'm, losers '
      + Math.round(a.hold_seconds_avg_loss / 60) + 'm.</p>';
  }
  $('analysisBody').innerHTML = html;
}

async function startAll() {
  const names = (SNAPSHOT.strategy_cards || []).map(c => c.name);
  const btn = document.getElementById('startAllBtn');
  if (btn) btn.classList.add('loading');
  
  try {
    if ((SNAPSHOT.mode && SNAPSHOT.mode.mode) === 'live') {
      // Arming six real-money strategies is not a thing to do by muscle memory.
      if (!confirm('LIVE MODE\n\nThis starts REAL-MONEY trading in all '
        + names.length + ' strategies against your Kalshi account.\n\n'
        + 'Prefer arming them one at a time. Continue?')) {
        note('live bulk start cancelled');
        return;
      }
    }
    for (const n of names) await toggleStrategy(n, true);
    note('started: ' + names.join(', '));
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}
async function stopAll() {
  const names = (SNAPSHOT.strategy_cards || []).filter(c => c.running).map(c => c.name);
  const stopAllButtons = document.querySelectorAll('button:contains("Stop all")');
  stopAllButtons.forEach(btn => btn.classList.add('loading'));
  
  try {
    for (const n of names) await toggleStrategy(n, true);
    note('stopped: ' + (names.join(', ') || 'nothing was running'));
  } finally {
    stopAllButtons.forEach(btn => btn.classList.remove('loading'));
  }
}

// Collapse the live feed panel. It is tall and its numbers tick continuously,
// so it earns the right to be folded away once you have read it.
function toggleFeeds() {
  const body = $('feedsBody');
  const label = $('feedToggle');
  if (!body) return;
  const nowHidden = body.style.display === 'none';
  body.style.display = nowHidden ? '' : 'none';
  if (label) label.textContent = nowHidden ? 'hide' : 'show';
  try { localStorage.setItem('feedsHidden', nowHidden ? '0' : '1'); } catch (e) {}
}
(function restoreFeeds() {
  let hidden = null;
  try { hidden = localStorage.getItem('feedsHidden'); } catch (e) {}
  if (hidden === '1') {
    const body = $('feedsBody');
    const label = $('feedToggle');
    if (body) body.style.display = 'none';
    if (label) label.textContent = 'show';
  }
})();

// --- init ---
// Live data first, and never let a chart library take the page down with it.
//
// Chart.js comes from a CDN. If that request is slow, blocked by a proxy or an
// ad blocker, or simply offline, `Chart` is undefined and the first `new Chart`
// threw - which aborted this whole script *before* the feed polling below was
// registered. The result was a page whose prices stayed on their hardcoded
// "connecting..." / "--" defaults forever even though /api/marketdata was
// serving live data the whole time.
//
// So: register the pollers first, make every chart call conditional, and run
// the renderers independently so one failure cannot silence the rest.
const CHARTS_OK = typeof Chart !== 'undefined';
if (!CHARTS_OK) {
  console.warn('Chart.js unavailable - charts disabled, live data still updating.');
}
function safeDraw(fn, label) {
  if (!CHARTS_OK) return;
  try { fn(); } catch (e) { console.warn('chart ' + label + ' failed', e); }
}
function safeRun(fn, label) {
  try { fn(); } catch (e) { console.warn(label + ' failed', e); }
}

// Pollers are registered before anything can throw.
setInterval(refresh, 30000);
setInterval(loadLogs, 30000);
setInterval(refreshFeeds, 5000);
setInterval(tickClock, 500);

safeDraw(drawChart, 'account');
safeDraw(drawSparks, 'sparks');
safeRun(() => paintMarket({{ (m | tojson) }}), 'paintMarket');
safeRun(() => paintAccount({{ (acct | tojson) }}), 'paintAccount');
safeRun(loadLogs, 'loadLogs');
safeRun(() => paintMode({ mode: SNAPSHOT.mode.mode }), 'paintMode');
safeRun(refresh, 'refresh');
safeRun(refreshFeeds, 'refreshFeeds');
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
    for target in (
        _monitor_loop,
        _log_tail_loop,
        _backup_loop,
        _market_data_loop,
        _strategy_supervisor_loop,
    ):
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
