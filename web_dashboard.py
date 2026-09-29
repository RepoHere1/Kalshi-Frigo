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
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from collections import deque
from typing import Any, Deque, Dict

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


async def _fetch_kalshi_data():
    """Fetch balance and positions from Kalshi API."""
    from src.clients.kalshi_client import KalshiClient

    client = KalshiClient()
    try:
        await client.initialize()
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


def _monitor_loop():
    """Background thread: credentials, Kalshi connect, DB init."""
    dashboard_state["status"] = "online"

    kalshi_key = os.environ.get("KALSHI_API_KEY", "")
    kalshi_private = os.environ.get("KALSHI_PRIVATE_KEY", "") or os.environ.get(
        "KALSHI_PRIVATE_KEY_PATH", ""
    )
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")

    dashboard_state["has_kalshi_creds"] = bool(kalshi_key and kalshi_private)
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
            dashboard_state["positions"] = positions.get("event_positions", [])
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
# Routes — HTML
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    return render_template_string(_HTML)


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
_HTML = r"""<!doctype html>
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
button.success { background:var(--green); color:#000; }
input, textarea { background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:4px 8px; font-size:.8rem; width:100%; }
input[type=checkbox] { width:auto; margin:0 6px 0 0; padding:0; vertical-align:middle; accent-color:var(--blue); }
pre { background:var(--bg); border:1px solid var(--border); border-radius:6px; padding:8px; font-size:.7rem; max-height:300px; overflow:auto; white-space:pre-wrap; }
.two-col { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.three-col { display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px; }
.flex-row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.tag { display:inline-block; background:#21262d; color:var(--blue); padding:1px 6px; border-radius:8px; font-size:.6rem; margin-right:3px; }
.err { color:var(--muted); text-align:center; padding:8px; }
canvas { max-height:250px; }
</style>
</head>
<body>
<div class="container">
<h1>🧠 Kalshi-Frigo Dashboard</h1>
<p class="subtitle">AI Trading Bot — 8 features · live + paper · multi-strategy</p>

<div class="grid" id="statsGrid">
  <div class="stat"><div class="stat-value" id="sStatus">--</div><div class="stat-label">Status</div></div>
  <div class="stat"><div class="stat-value" id="sBalance">-</div><div class="stat-label">Balance</div></div>
  <div class="stat"><div class="stat-value" id="sPositions">0</div><div class="stat-label">Positions</div></div>
  <div class="stat"><div class="stat-value" id="sKalshi">--</div><div class="stat-label">Kalshi</div></div>
  <div class="stat"><div class="stat-value" id="sOpenRouter">--</div><div class="stat-label">OpenRouter</div></div>
  <div class="stat"><div class="stat-value" id="sStrategies">0</div><div class="stat-label">Running</div></div>
  <div class="stat"><div class="stat-value" id="sUptime">0m</div><div class="stat-label">Uptime</div></div>
</div>

<div class="card">
  <h2>⚠️ Recent Errors</h2>
  <div id="sErrors" class="flex-row"><span class="badge no">loading</span></div>
</div>

<div class="two-col">
<!-- Feature 1: Live Position Table -->
<div class="card">
  <h2>📍 Live Positions</h2>
  <table id="posTable"><thead><tr><th>Market</th><th>Side</th><th>Price</th><th>Qty</th><th>Strategy</th><th>SL</th><th>TP</th></tr></thead><tbody></tbody></table>
</div>

<!-- Feature 4: P&L Chart -->
<div class="card">
  <h2>📈 P&L Chart</h2>
  <canvas id="pnlChart"></canvas>
</div>
</div>

<div class="two-col">
<!-- Feature 3: Strategy Control -->
<div class="card">
  <h2>🎮 Strategy Control</h2>
  <div id="strategyButtons"></div>
</div>

<!-- Feature 5: Alerting -->
<div class="card">
  <h2>🔔 Alerts</h2>
  <label>Telegram URL <input id="tgUrl" style="width:80%"></label><br><br>
  <label>Discord URL <input id="dcUrl" style="width:80%"></label><br><br>
  <label><input type="checkbox" id="alertEnabled"> Enable alerts</label><br><br>
  <button onclick="saveAlerts()">Save</button>
  <span id="alertStatus"></span>
</div>
</div>

<div class="two-col">
<!-- Feature 6: Config Editor -->
<div class="card">
  <h2>⚙️ Config Editor</h2>
  <div id="configEditor"></div>
  <button onclick="saveConfig()" style="margin-top:8px">Save Config</button>
  <span id="configStatus" style="font-size:.7rem;color:var(--muted);margin-left:6px"></span>
</div>

<!-- Feature 7: Log Viewer -->
<div class="card">
  <h2>📜 Log Viewer</h2>
  <div class="flex-row" style="margin-bottom:6px">
    <input id="logLines" value="100" style="width:80px">
    <button onclick="loadLogs()">Load</button>
  </div>
  <pre id="logOutput" style="max-height:250px"></pre>
</div>
</div>

<!-- Feature 2: Trade Feed -->
<div class="card">
  <h2>📡 Real-time Trade Feed</h2>
  <pre id="tradeFeed" style="max-height:200px">Waiting for events...</pre>
</div>

<!-- Feature 8: Multi-Bot -->
<div class="card">
  <h2>🤖 Multi-Bot Manager</h2>
  <table id="botTable"><thead><tr><th>Bot</th><th>PID</th><th>Mode</th><th>Running</th><th>Actions</th></tr></thead><tbody></tbody></table>
</div>

<footer style="margin-top:16px;text-align:center;color:var(--muted);font-size:.7rem">
Kalshi-Frigo · Not financial advice
</footer>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
const $ = id => document.getElementById(id);
const esc = v => String(v == null ? '' : v);
const num = (v, d=2) => (v == null || v === '' ? '-' : Number(v).toFixed(d));

// --- SSE Trade Feed ---
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

// --- Stats ---
async function loadStatus() {
  try {
    const r = await fetch('/api/status').then(r=>r.json());
    $('sStatus').innerHTML = r.status==='online'?'<span class="badge ok">ONLINE</span>':'<span class="badge no">OFF</span>';
    $('sBalance').textContent = r.balance != null ? '$' + num(r.balance) : '-';
    $('sPositions').textContent = r.positions_count||0;
    $('sKalshi').innerHTML = r.has_kalshi_creds?'<span class="badge ok">OK</span>':'<span class="badge no">No</span>';
    $('sOpenRouter').innerHTML = r.has_openrouter_creds?'<span class="badge ok">OK</span>':'<span class="badge no">No</span>';
    $('sStrategies').textContent = r.running_strategies||0;
    $('sUptime').textContent = Math.floor((r.uptime_sec||0)/60) + 'm';
    $('sErrors').innerHTML = (r.errors||[]).slice(-5).reverse()
      .map(e => `<div class="tag">${esc(e.time)} ${esc(e.error)}</div>`).join('') || '<span class="badge ok">none</span>';
  } catch (e) { $('sStatus').innerHTML = '<span class="badge warn">ERR</span>'; }
}

// --- Positions ---
async function loadPositions() {
  const tbody = document.querySelector('#posTable tbody');
  try {
    const r = await fetch('/api/positions').then(r=>r.json());
    if (r.error) { tbody.innerHTML = `<tr><td colspan="7" class="err">${esc(r.error)}</td></tr>`; return; }
    const rows = r.positions||[];
    tbody.innerHTML = rows.length ? rows.map(p => `<tr>
      <td>${esc(String(p.market_id||'?').slice(0,20))}</td><td>${esc(p.side)}</td><td>${num(p.entry_price)}</td>
      <td>${esc(p.quantity)}</td><td><span class="tag">${esc(p.strategy)}</span></td>
      <td>${p.stop_loss!=null?num(p.stop_loss):'-'}</td><td>${p.take_profit!=null?num(p.take_profit):'-'}</td></tr>`).join('')
      : '<tr><td colspan="7" class="err">No open positions</td></tr>';
  } catch (e) { tbody.innerHTML = `<tr><td colspan="7" class="err">${esc(e.message)}</td></tr>`; }
}

// --- Strategies ---
async function loadStrategies() {
  const r = await fetch('/api/strategies').then(r=>r.json());
  $('strategyButtons').innerHTML = Object.entries(r).map(([name,s]) =>
    `<div style="margin:4px 0"><span class="tag">${esc(name)}</span> ${s.running?'<span class="badge ok">RUN</span>':'<span class="badge no">STOP</span>'} ${s.pid?'PID:'+esc(s.pid):''}
    <button onclick="toggleStrategy('${name}')">${s.running?'Stop':'Start'}</button></div>`
  ).join('');
}
async function toggleStrategy(name) {
  const r = await fetch('/api/strategy/'+encodeURIComponent(name)+'/toggle',
    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:'paper'})});
  const d = await r.json().catch(()=>({}));
  if (d.error) pushFeed(name + ': ' + d.error);
  loadStrategies(); loadBots();
}

// --- Alerts ---
async function loadAlerts() {
  const r = await fetch('/api/alerts').then(r=>r.json());
  $('tgUrl').value = r.telegram_url||'';
  $('dcUrl').value = r.discord_url||'';
  $('alertEnabled').checked = !!r.enabled;
}
async function saveAlerts() {
  const r = await fetch('/api/alerts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    telegram_url: $('tgUrl').value, discord_url: $('dcUrl').value, enabled: $('alertEnabled').checked
  })});
  const d = await r.json();
  $('alertStatus').textContent = d.ok?'Saved':'Error';
}

// --- Config ---
// Send each value typed from the current value, so booleans stay booleans.
const cfgTypes = {};
async function loadConfig() {
  const r = await fetch('/api/config').then(r=>r.json());
  Object.entries(r).forEach(([k,v]) => { cfgTypes[k] = typeof v; });
  $('configEditor').innerHTML = Object.entries(r).map(([k,v]) => {
    if (typeof v === 'boolean') {
      return `<label style="display:flex;gap:6px;align-items:center;margin:4px 0;font-size:.75rem">
        <input type="checkbox" data-key="${k}" data-type="boolean" ${v?'checked':''}> ${k}</label>`;
    }
    const step = typeof v === 'number' ? ' step="any"' : '';
    return `<label style="display:block;margin:4px 0;font-size:.75rem">${esc(k)}
      <input data-key="${k}" data-type="${typeof v}" value="${esc(v)}"${step} style="width:100%"></label>`;
  }).join('');
}
async function saveConfig() {
  const obj = {};
  document.querySelectorAll('#configEditor input').forEach(i => {
    const t = i.dataset.type;
    if (t === 'boolean') obj[i.dataset.key] = i.checked;
    else if (t === 'number') obj[i.dataset.key] = i.value === '' ? null : Number(i.value);
    else obj[i.dataset.key] = i.value;
  });
  const r = await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)});
  const d = await r.json().catch(()=>({}));
  $('configStatus').textContent = d.ok ? 'Saved: ' + (d.updated||[]).length + ' field(s)' : 'Error';
  loadConfig();
}

// --- Logs ---
async function loadLogs() {
  const n = $('logLines').value || 100;
  const r = await fetch('/api/logs?lines='+encodeURIComponent(n)).then(r=>r.json());
  $('logOutput').textContent = (r.lines||[]).join('\n') || '(no log files yet — start a strategy to generate logs)';
}

// --- Bots ---
async function loadBots() {
  const r = await fetch('/api/bots').then(r=>r.json());
  if (!Array.isArray(r)) { return; }
  document.querySelector('#botTable tbody').innerHTML = r.map(b => `<tr>
    <td>${esc(b.name)}</td><td>${b.pid?esc(b.pid):'-'}</td><td>${esc(b.mode)}</td>
    <td>${b.running?'<span class="badge ok">RUN</span>':'<span class="badge no">STOP</span>'}</td>
    <td><button class="danger" onclick="killBot('${b.name}')">Stop</button></td></tr>`).join('');
}
async function killBot(name) {
  await fetch('/api/bot/'+encodeURIComponent(name)+'/kill',{method:'POST'});
  loadBots(); loadStrategies();
}

// --- Charts ---
let pnlChart;
async function loadCharts() {
  if (typeof Chart === 'undefined') return;
  const r = await fetch('/api/chart/pnl').then(r=>r.json());
  if (r.error) return;
  const ctx = $('pnlChart');
  if (pnlChart) pnlChart.destroy();
  pnlChart = new Chart(ctx, {
    type: 'line',
    data: { labels: r.labels, datasets: [{ label: 'Cumulative P&L ($)', data: r.pnl, borderColor: '#3fb950', backgroundColor: 'rgba(63,185,80,0.1)', tension: 0.3, fill: true, pointRadius: 2 }] },
    options: { responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { labels: { color: '#c9d1d9' } } },
      scales: { x: { ticks: { color: '#8b949e', maxTicksLimit: 8 } }, y: { ticks: { color: '#8b949e' } } } }
  });
}

// --- Init ---
async function init() {
  await Promise.allSettled([loadStatus(), loadPositions(), loadStrategies(), loadAlerts(),
                             loadConfig(), loadLogs(), loadBots(), loadCharts()]);
  setInterval(loadStatus, 5000);
  setInterval(loadPositions, 10000);
  setInterval(loadStrategies, 10000);
  setInterval(loadBots, 10000);
  setInterval(loadCharts, 30000);
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
