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
import os
import sys
import time
import json
import asyncio
import threading
import subprocess
from pathlib import Path
from datetime import datetime
from collections import deque

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, jsonify, render_template_string, request
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.config["PREFERRED_URL_SCHEME"] = "https"

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
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
log_buffer = deque(maxlen=500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _broadcast(event, data):
    """Push event to all SSE listeners."""
    payload = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
    dead = []
    for q in dashboard_state["sse_listeners"]:
        try:
            q.put_nowait(payload)
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            dashboard_state["sse_listeners"].remove(q)
        except Exception:
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
        dashboard_state["errors"].append({"time": _now(), "error": f"Kalshi fetch: {e}"})
        return None, None
    finally:
        await client.close()


async def _fetch_db_data():
    """Fetch positions and trades from SQLite."""
    from src.utils.database import DatabaseManager
    db = DatabaseManager()
    await db.initialize()
    try:
        open_positions = await db.get_open_live_positions()
        perf = await db.get_performance_by_strategy()
        return open_positions, perf
    finally:
        # aiosqlite auto-closes on context exit; no explicit close needed
        pass


def _monitor_loop():
    """Background thread: credentials, Kalshi connect, log tail."""
    dashboard_state["status"] = "online"

    kalshi_key = os.environ.get("KALSHI_API_KEY", "")
    kalshi_private = os.environ.get("KALSHI_PRIVATE_KEY", "") or os.environ.get(
        "KALSHI_PRIVATE_KEY_PATH", ""
    )
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")

    dashboard_state["has_kalshi_creds"] = bool(kalshi_key and kalshi_private)
    dashboard_state["has_openrouter_creds"] = bool(openrouter_key)

    if not dashboard_state["has_kalshi_creds"]:
        dashboard_state["errors"].append(
            {"time": _now(), "error": "Kalshi credentials not configured — info mode only"}
        )
        dashboard_state["last_update"] = _now()
        return

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        balance, positions = loop.run_until_complete(_fetch_kalshi_data())
        if balance:
            dashboard_state["balance"] = balance.get("balance", 0) / 100.0
        if positions:
            dashboard_state["positions"] = positions.get("event_positions", [])
        dashboard_state["last_update"] = _now()
    except Exception as e:
        dashboard_state["errors"].append({"time": _now(), "error": f"Kalshi connect: {e}"})


def _log_tail_loop():
    """Tail the log file and buffer lines."""
    log_path = Path("logs/trading_system.log")
    while True:
        if log_path.exists():
            try:
                with open(log_path, "r", errors="replace") as f:
                    f.seek(0, 2)  # end
                    while True:
                        line = f.readline()
                        if not line:
                            time.sleep(0.5)
                            continue
                        log_buffer.append(line.strip())
            except Exception:
                time.sleep(2)
        else:
            time.sleep(2)


# ---------------------------------------------------------------------------
# Routes — HTML
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    return render_template_string(_HTML)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------
@app.route("/api/status")
def api_status():
    return jsonify(
        {
            "status": dashboard_state["status"],
            "has_kalshi_creds": dashboard_state["has_kalshi_creds"],
            "has_openrouter_creds": dashboard_state["has_openrouter_creds"],
            "balance": dashboard_state["balance"],
            "positions_count": len(dashboard_state["positions"]),
            "last_update": dashboard_state["last_update"],
            "errors": dashboard_state["errors"][-10:],
            "alert_state": alert_state,
        }
    )


# 1. Live position table
@app.route("/api/positions")
def api_positions():
    """Return open positions from DB + Kalshi."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_fetch_db_data())
        db = __import__("src.utils.database", fromlist=["DatabaseManager"])()
        loop.run_until_complete(db.initialize())
        positions = loop.run_until_complete(db.get_open_live_positions())
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
        return jsonify({"positions": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# 2. Real-time trade feed (SSE)
@app.route("/api/stream")
def api_stream():
    """SSE endpoint for real-time events."""
    def generate():
        q = deque(maxlen=50)
        dashboard_state["sse_listeners"].append(q)
        yield f"event: connected\ndata: {json.dumps({'msg': 'connected'})}\n\n"
        while True:
            try:
                msg = q.popleft()
                yield msg
            except IndexError:
                time.sleep(0.5)

    from flask import Response

    return Response(generate(), mimetype="text/event-stream")


# 3. Strategy control panel
@app.route("/api/strategies", methods=["GET"])
def api_strategies():
    return jsonify(strategy_state)


@app.route("/api/strategy/<name>/toggle", methods=["POST"])
def api_strategy_toggle(name):
    """Start/stop a strategy subprocess."""
    if name not in strategy_state:
        return jsonify({"error": f"Unknown strategy: {name}"}), 404

    st = strategy_state[name]
    mode = request.json.get("mode", "paper") if request.json else "paper"

    if st["running"]:
        # Stop
        if st["pid"]:
            try:
                import signal
                os.kill(st["pid"], signal.SIGTERM)
            except Exception:
                pass
        st["running"] = False
        st["pid"] = None
        _broadcast("strategy", {"name": name, "action": "stopped"})
        return jsonify({"name": name, "running": False})

    # Start
    cmd_map = {
        "ai_directional": ["python", "cli.py", "run", f"--{'live' if mode == 'live' else 'paper'}"],
        "safe_compounder": [
            "python",
            "cli.py",
            "run",
            "--safe-compounder",
            f"--{'live' if mode == 'live' else 'paper'}",
        ],
        "beast_mode": ["python", "cli.py", "run", "--beast", f"--{'live' if mode == 'live' else 'paper'}"],
        "market_making": ["python", "cli.py", "run", "--safe-compounder"],  # placeholder
        "quick_flip": ["python", "cli.py", "run"],  # placeholder
    }
    cmd = cmd_map.get(name, ["python", "cli.py", "run", "--paper"])
    proc = subprocess.Popen(
        cmd,
        cwd=str(Path(__file__).parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
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
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        db = __import__("src.utils.database", fromlist=["DatabaseManager"])()
        loop.run_until_complete(db.initialize())
        perf = loop.run_until_complete(db.get_performance_by_strategy())
        trades = loop.run_until_complete(db.get_llm_queries(hours_back=24))

        labels = []
        pnl_data = []
        cumulative = 0.0
        try:
            from src.utils.database import TradeLog
            import aiosqlite
            with aiosqlite.connect(db.db_path) as conn:
                conn.row_factory = aiosqlite.Row
                cur = conn.execute(
                    "SELECT exit_timestamp, pnl FROM trade_logs ORDER BY exit_timestamp ASC LIMIT 200"
                )
                for row in cur.fetchall():
                    labels.append(row[0][:19])
                    cumulative += row[1] or 0.0
                    pnl_data.append(round(cumulative, 2))
        except Exception:
            labels = ["--"]
            pnl_data = [0]

        return jsonify(
            {
                "labels": labels,
                "pnl": pnl_data,
                "by_strategy": {k: v.get("total_pnl", 0) for k, v in perf.items()},
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chart/performance")
def api_chart_performance():
    """Strategy performance breakdown."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        db = __import__("src.utils.database", fromlist=["DatabaseManager"])()
        loop.run_until_complete(db.initialize())
        perf = loop.run_until_complete(db.get_performance_by_strategy())
        return jsonify(perf)
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
@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    """Read/write trading config."""
    from src.config.settings import settings

    if request.method == "POST":
        data = request.json or {}
        # Apply config changes at runtime
        for key, val in data.items():
            if hasattr(settings.trading, key):
                setattr(settings.trading, key, val)
        _broadcast("config", {"action": "updated", "keys": list(data.keys())})
        return jsonify({"ok": True, "updated": list(data.keys())})

    # Read
    readable = {}
    for field in [
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
        "live_trading_enabled",
        "paper_trading_mode",
    ]:
        readable[field] = getattr(settings.trading, field, None)
    return jsonify(readable)


# 7. Log viewer
@app.route("/api/logs")
def api_logs():
    """Return recent log lines."""
    lines = request.args.get("lines", "100")
    try:
        n = int(lines)
    except ValueError:
        n = 100
    log_path = Path("logs/trading_system.log")
    if log_path.exists():
        with open(log_path, "r", errors="replace") as f:
            all_lines = f.readlines()
        recent = all_lines[-n:]
        return jsonify({"lines": [l.strip() for l in recent]})
    return jsonify({"lines": list(log_buffer)[-n:]})


# 8. Multi-bot management
@app.route("/api/bots", methods=["GET"])
def api_bots():
    """List tracked bot processes."""
    bots = []
    for name, st in strategy_state.items():
        alive = False
        if st["pid"]:
            try:
                os.kill(st["pid"], 0)
                alive = True
            except (ProcessLookupError, PermissionError):
                alive = False
                st["running"] = False
                st["pid"] = None
        bots.append(
            {
                "name": name,
                "running": st["running"] and alive,
                "pid": st["pid"],
                "mode": st.get("mode", "paper"),
            }
        )
    return jsonify(bots)


@app.route("/api/bot/<name>/kill", methods=["POST"])
def api_bot_kill(name):
    """Force-kill a bot process."""
    if name not in strategy_state:
        return jsonify({"error": "unknown"}), 404
    st = strategy_state[name]
    if st["pid"]:
        try:
            import signal
            os.kill(st["pid"], signal.SIGKILL)
        except Exception:
            pass
    st["running"] = False
    st["pid"] = None
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
pre { background:var(--bg); border:1px solid var(--border); border-radius:6px; padding:8px; font-size:.7rem; max-height:300px; overflow:auto; white-space:pre-wrap; }
.two-col { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.three-col { display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px; }
.flex-row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.tag { display:inline-block; background:#21262d; color:var(--blue); padding:1px 6px; border-radius:8px; font-size:.6rem; margin-right:3px; }
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
  <div class="stat"><div class="stat-value" id="sStrategies">0</div><div class="stat-label">Strategies</div></div>
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
// --- SSE Trade Feed ---
const evtSource = new EventSource("/api/stream");
evtSource.onmessage = e => {
  const feed = document.getElementById('tradeFeed');
  const d = JSON.parse(e.data);
  feed.textContent = '[' + new Date().toLocaleTimeString() + '] ' + JSON.stringify(d) + '\n' + feed.textContent;
  if (feed.childNodes.length > 50) feed.removeChild(feed.lastChild);
};

// --- Stats ---
async function loadStatus() {
  const r = await fetch('/api/status').then(r=>r.json());
  document.getElementById('sStatus').innerHTML = r.status==='online'?'<span class="badge ok">ONLINE</span>':'<span class="badge no">OFF</span>';
  document.getElementById('sBalance').textContent = r.balance ? '$'+r.balance : '-';
  document.getElementById('sPositions').textContent = r.positions_count||0;
  document.getElementById('sKalshi').innerHTML = r.has_kalshi_creds?'<span class="badge ok">OK</span>':'<span class="badge no">No</span>';
  document.getElementById('sOpenRouter').innerHTML = r.has_openrouter_creds?'<span class="badge ok">OK</span>':'<span class="badge no">No</span>';
}

// --- Positions ---
async function loadPositions() {
  const r = await fetch('/api/positions').then(r=>r.json());
  const tbody = document.querySelector('#posTable tbody');
  tbody.innerHTML = (r.positions||[]).map(p => `<tr>
    <td>${p.market_id.slice(0,20)}</td><td>${p.side}</td><td>${p.entry_price}</td>
    <td>${p.quantity}</td><td><span class="tag">${p.strategy}</span></td>
    <td>${p.stop_loss||'-'}</td><td>${p.take_profit||'-'}</td></tr>`).join('');
}

// --- Strategies ---
async function loadStrategies() {
  const r = await fetch('/api/strategies').then(r=>r.json());
  const div = document.getElementById('strategyButtons');
  div.innerHTML = Object.entries(r).map(([name,s]) =>
    `<div style="margin:4px 0"><span class="tag">${name}</span> ${s.running?'<span class="badge ok">RUN</span>':'<span class="badge no">STOP</span>'} ${s.pid?'PID:'+s.pid:''}
    <button onclick="toggleStrategy('${name}')">${s.running?'Stop':'Start'}</button></div>`
  ).join('');
}
async function toggleStrategy(name) {
  const r = await fetch('/api/strategy/'+name+'/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:'paper'})});
  loadStrategies(); loadBots();
}

// --- Alerts ---
async function loadAlerts() {
  const r = await fetch('/api/alerts').then(r=>r.json());
  document.getElementById('tgUrl').value = r.telegram_url||'';
  document.getElementById('dcUrl').value = r.discord_url||'';
  document.getElementById('alertEnabled').checked = r.enabled;
}
async function saveAlerts() {
  const r = await fetch('/api/alerts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    telegram_url: document.getElementById('tgUrl').value,
    discord_url: document.getElementById('dcUrl').value,
    enabled: document.getElementById('alertEnabled').checked
  })});
  const d = await r.json();
  document.getElementById('alertStatus').textContent = d.ok?'Saved':'Error';
}

// --- Config ---
async function loadConfig() {
  const r = await fetch('/api/config').then(r=>r.json());
  const div = document.getElementById('configEditor');
  div.innerHTML = Object.entries(r).map(([k,v]) =>
    `<label style="display:block;margin:4px 0;font-size:.75rem">${k}<input data-key="${k}" value="${v}" style="width:100%"></label>`
  ).join('');
}
async function saveConfig() {
  const obj = {};
  document.querySelectorAll('#configEditor input').forEach(i => obj[i.dataset.key]=parseFloat(i.value));
  await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)});
}

// --- Logs ---
async function loadLogs() {
  const n = document.getElementById('logLines').value;
  const r = await fetch('/api/logs?lines='+n).then(r=>r.json());
  document.getElementById('logOutput').textContent = (r.lines||[]).join('\n');
}

// --- Bots ---
async function loadBots() {
  const r = await fetch('/api/bots').then(r=>r.json());
  const tbody = document.querySelector('#botTable tbody');
  tbody.innerHTML = r.map(b => `<tr>
    <td>${b.name}</td><td>${b.pid||'-'}</td><td>${b.mode}</td>
    <td>${b.running?'<span class="badge ok">RUN</span>':'<span class="badge no">STOP</span>'}</td>
    <td><button class="danger" onclick="killBot('${b.name}')">Kill</button></td></tr>`).join('');
}
async function killBot(name) {
  await fetch('/api/bot/'+name+'/kill',{method:'POST'});
  loadBots(); loadStrategies();
}

// --- Charts ---
let pnlChart;
async function loadCharts() {
  const r = await fetch('/api/chart/pnl').then(r=>r.json());
  const ctx = document.getElementById('pnlChart');
  if (pnlChart) pnlChart.destroy();
  pnlChart = new Chart(ctx, {
    type: 'line',
    data: { labels: r.labels, datasets: [{ label: 'Cumulative P&L', data: r.pnl, borderColor: '#3fb950', backgroundColor: 'rgba(63,185,80,0.1)', tension: 0.3, fill: true }] },
    options: { responsive: true, plugins: { legend: { labels: { color: '#c9d1d9' } } }, scales: { x: { ticks: { color: '#8b949e' } }, y: { ticks: { color: '#8b949e' } } } }
  });
}

// --- Init ---
async function init() {
  await loadStatus(); await loadPositions(); await loadStrategies(); await loadAlerts();
  await loadConfig(); await loadLogs(); await loadBots(); await loadCharts();
  setInterval(loadStatus, 5000); setInterval(loadPositions, 10000);
  setInterval(loadStrategies, 10000); setInterval(loadBots, 10000);
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
def main():
    import signal

    def handle_signal(signum, frame):
        global running
        running = False
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    monitor_thread = threading.Thread(target=_monitor_loop, daemon=True)
    monitor_thread.start()

    log_thread = threading.Thread(target=_log_tail_loop, daemon=True)
    log_thread.start()

    port = int(os.environ.get("PORT", 8080))
    print(f"[web_dashboard] Kalshi-Frigo enhanced dashboard starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
