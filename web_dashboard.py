#!/usr/bin/env python3
"""
Kalshi-Frigo Web Dashboard — standalone Flask server.

Shows what Kalshi-Frigo is, lists strategies, components, and modules.
If Kalshi credentials are configured, it connects and shows live data.
Safe to run without credentials — falls back to info-only mode.
"""
import os
import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

dashboard_state = {
    "status": "starting",
    "has_kalshi_creds": False,
    "has_openrouter_creds": False,
    "balance": None,
    "positions": [],
    "strategies": [],
    "last_update": None,
    "errors": [],
}

running = True

DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalshi-Frigo — AI Trading Bot Dashboard</title>
<style>
:root { --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#c9d1d9;
         --muted:#8b949e; --green:#3fb950; --red:#f85149; --blue:#58a6ff;
         --yellow:#d29922; --purple:#bc8cff; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:var(--bg); color:var(--text); min-height:100vh; padding:24px; }
.container { max-width:1150px; margin:0 auto; }
h1 { font-size:1.75rem; font-weight:700; margin-bottom:.25rem; color:#fff; }
.subtitle { color:var(--muted); margin-bottom:1.5rem; font-size:.9rem; }
.card { background:var(--card); border:1px solid var(--border); border-radius:12px;
         padding:1.25rem; margin-bottom:1rem; }
.card h2 { font-size:1rem; font-weight:600; margin-bottom:.75rem; color:#fff; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
        gap:1rem; margin-bottom:1rem; }
.stat { text-align:center; background:var(--card); border:1px solid var(--border);
        border-radius:12px; padding:1rem; }
.stat-value { font-size:1.5rem; font-weight:700; }
.stat-label { font-size:.75rem; color:var(--muted); margin-top:.4rem; text-transform:uppercase; }
.green { color:var(--green); } .red { color:var(--red); }
.blue { color:var(--blue); } .yellow { color:var(--yellow); }
.purple { color:var(--purple); }
.badge { display:inline-block; padding:3px 10px; border-radius:12px; font-size:.7rem;
         font-weight:600; text-transform:uppercase; }
.badge.ok { background:#033a16; color:var(--green); }
.badge.no { background:#1c2128; color:var(--muted); }
table { width:100%; border-collapse:collapse; font-size:.85rem; }
th { text-align:left; padding:.5rem; color:var(--muted); font-size:.7rem;
     text-transform:uppercase; border-bottom:2px solid var(--border); font-weight:600; }
td { padding:.5rem; border-bottom:1px solid var(--border); }
code { background:#0d1117; padding:2px 6px; border-radius:4px; font-size:.8rem;
       color:var(--yellow); font-family:monospace; }
.strategy-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:.75rem; }
.strategy { background:#0d1117; border:1px solid var(--border); border-radius:8px;
            padding:.9rem; }
.strategy h3 { font-size:.9rem; color:#fff; margin-bottom:.35rem; }
.strategy p { font-size:.78rem; color:var(--muted); line-height:1.45; margin-bottom:.5rem; }
.strategy .tag { display:inline-block; background:#21262d; color:var(--blue); padding:1px 7px;
                 border-radius:10px; font-size:.65rem; font-weight:600; margin-right:4px; }
.component-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:.5rem; }
.component { background:#0d1117; border:1px solid var(--border); border-radius:6px;
             padding:.6rem .8rem; font-size:.8rem; }
.component strong { color:#fff; font-size:.82rem; }
.component span { color:var(--muted); display:block; font-size:.75rem; margin-top:.15rem; }
footer { margin-top:2rem; text-align:center; color:var(--muted); font-size:.75rem; }
pre { background:#0d1117; border:1px solid var(--border); border-radius:6px;
      padding:.75rem; overflow:auto; font-size:.8rem; font-family:monospace; }
</style>
</head>
<body>
<div class="container">
<h1>🧠 Kalshi-Frigo</h1>
<p class="subtitle">AI-Powered Kalshi Trading Bot — LLM-driven decisions · Paper trading · Multiple strategies · SQLite telemetry</p>

<div class="grid">
<div class="stat">
<div class="stat-value" id="statusBadge"><span class="badge ok">ONLINE</span></div>
<div class="stat-label">Dashboard</div>
</div>
<div class="stat">
<div class="stat-value" id="kalshiCreds">Checking...</div>
<div class="stat-label">Kalshi API</div>
</div>
<div class="stat">
<div class="stat-value" id="openrouterCreds">Checking...</div>
<div class="stat-label">OpenRouter</div>
</div>
<div class="stat">
<div class="stat-value" id="balance">-</div>
<div class="stat-label">Balance</div>
</div>
<div class="stat">
<div class="stat-value" id="positions">0</div>
<div class="stat-label">Open Positions</div>
</div>
<div class="stat">
<div class="stat-value" id="strategies">7</div>
<div class="stat-label">Strategies</div>
</div>
</div>

<div class="card">
<h2>📚 What Kalshi-Frigo Does</h2>
<p style="color:var(--muted); font-size:.88rem; line-height:1.6; margin-bottom:.75rem;">
Kalshi-Frigo (aka kalshi-ai-trading-bot by ryanfrigo) is a full toolkit for building automated trading
strategies on Kalshi prediction markets. It ships with a signed Kalshi API client (RSA signing),
market data ingestion, position tracking with stop-loss/take-profit, a pluggable LLM client
(OpenRouter + xAI + Anthropic), SQLite telemetry, and multiple example strategies.
</p>
<div class="component-grid">
<div class="component"><strong>📡 Kalshi Client</strong><span>REST + WebSocket, RSA signing, retries, rate limits</span></div>
<div class="component"><strong>🤖 LLM Client</strong><span>OpenRouter · xAI · Anthropic · fallback chain · cost tracking</span></div>
<div class="component"><strong>📊 Market Ingestion</strong><span>Full tradeable universe via Events API → SQLite</span></div>
<div class="component"><strong>📍 Position Tracking</strong><span>SL/TP/time/resolution exits with real Kalshi sells</span></div>
<div class="component"><strong>💾 SQLite Telemetry</strong><span>Every trade, AI decision, cost metric logged</span></div>
<div class="component"><strong>🧪 Paper Trading</strong><span>Log signals against settled markets — no orders sent</span></div>
<div class="component"><strong>🎭 Multi-Agent</strong><span>Bull/bear researchers, debate, ensemble, risk manager</span></div>
<div class="component"><strong>📈 Performance System</strong><span>Automated analysis, backtest, quick-flip scalping</span></div>
</div>
</div>

<div class="card">
<h2>🎯 Strategies</h2>
<div class="strategy-grid">
<div class="strategy">
<h3>🤖 AI Directional (default)</h3>
<p>Single-model OpenRouter call per decision with category scoring and portfolio guardrails.
Fallback chain on errors.</p>
<span class="tag">LLM</span><span class="tag">Multi-category</span>
</div>
<div class="strategy">
<h3>💰 Safe Compounder</h3>
<p>Edge-based NO-side only, no LLM needed. Conservative math-only mode for steady compounding.</p>
<span class="tag">Math-only</span><span class="tag">Conservative</span>
</div>
<div class="strategy">
<h3>⚡ Quick Flip Scalping</h3>
<p>Short-term in-and-out trades targeting small edges with high frequency on liquid markets.</p>
<span class="tag">Scalping</span><span class="tag">High-frequency</span>
</div>
<div class="strategy">
<h3>🐂 Beast Mode</h3>
<p>Original aggressive settings. No guardrails. Not recommended for live trading.</p>
<span class="tag">Aggressive</span><span class="tag">High-risk</span>
</div>
<div class="strategy">
<h3>📊 Market Making</h3>
<p>Liquidity provision strategy with spread-based quoting on both YES and NO sides.</p>
<span class="tag">Market Making</span><span class="tag">Spread</span>
</div>
<div class="strategy">
<h3>📈 Portfolio Optimization</h3>
<p>Mean-variance optimization across multiple markets for diversified exposure.</p>
<span class="tag">Portfolio</span><span class="tag">Diversified</span>
</div>
<div class="strategy">
<h3>🧠 Unified Trading System</h3>
<p>Combines all strategies with signal weighting and dynamic allocation based on performance.</p>
<span class="tag">Ensemble</span><span class="tag">Dynamic</span>
</div>
</div>
</div>

<div class="card">
<h2>📁 Key Files</h2>
<table>
<tr><th>File</th><th>Purpose</th></tr>
<tr><td><code>cli.py</code></td><td>Unified CLI: run, dashboard, status, health, scores, history, close-all</td></tr>
<tr><td><code>beast_mode_bot.py</code></td><td>Main bot orchestrator — runs strategies end-to-end</td></tr>
<tr><td><code>beast_mode_dashboard.py</code></td><td>Streamlit monitoring dashboard (local only)</td></tr>
<tr><td><code>paper_trader.py</code></td><td>Paper trading mode — simulates trades without real orders</td></tr>
<tr><td><code>src/clients/kalshi_client.py</code></td><td>Signed Kalshi REST + WebSocket client</td></tr>
<tr><td><code>src/clients/openrouter_client.py</code></td><td>OpenRouter LLM client with fallback chain</td></tr>
<tr><td><code>src/utils/database.py</code></td><td>SQLite telemetry — trades, decisions, costs</td></tr>
<tr><td><code>src/jobs/ingest.py</code></td><td>Market data ingestion job</td></tr>
<tr><td><code>src/jobs/track.py</code></td><td>Position tracking with exit conditions</td></tr>
<tr><td><code>src/jobs/decide.py</code></td><td>Trading decision engine</td></tr>
<tr><td><code>src/jobs/execute.py</code></td><td>Order execution</td></tr>
</table>
</div>

<div class="card">
<h2>🧾 Run Locally</h2>
<pre>pip install -r requirements.txt
python setup_env.py          # creates .venv, installs deps
cp env.template .env         # fill in KALSHI_API_KEY and OPENROUTER_API_KEY

python cli.py health         # verify API connections
python cli.py run --paper    # AI directional in paper mode
python cli.py run --safe-compounder   # conservative math-only mode
python cli.py dashboard      # Streamlit dashboard (local GUI)
python web_dashboard.py      # this web dashboard (HTTP, Railway-ready)
</pre>
</div>

<div class="card">
<h2>🩺 Live Status</h2>
<pre id="liveStatus">Loading...</pre>
</div>

<footer>
Kalshi-Frigo · Based on ryanfrigo/kalshi-ai-trading-bot · Not financial advice
</footer>
</div>

<script>
async function loadStatus() {
  try {
    const res = await fetch('/api/status');
    const d = await res.json();
    document.getElementById('kalshiCreds').innerHTML =
      d.has_kalshi_creds ? '<span class="badge ok">Connected</span>' : '<span class="badge no">Not set</span>';
    document.getElementById('openrouterCreds').innerHTML =
      d.has_openrouter_creds ? '<span class="badge ok">Configured</span>' : '<span class="badge no">Not set</span>';
    document.getElementById('balance').textContent = d.balance ? '$' + d.balance : '-';
    document.getElementById('positions').textContent = d.positions_count || 0;
    let statusText = 'Dashboard online.\n';
    statusText += 'Last update: ' + (d.last_update || 'never') + '\\n';
    if (d.errors && d.errors.length) {
      statusText += '\\nRecent events:\\n' + d.errors.slice(-5).map(e => '  ' + e.time + ' — ' + e.error).join('\\n');
    } else {
      statusText += '\\nNo errors. Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY for live data.';
    }
    document.getElementById('liveStatus').textContent = statusText;
  } catch(e) {
    document.getElementById('liveStatus').textContent = 'Error fetching status: ' + e;
  }
}
setInterval(loadStatus, 5000);
loadStatus();
</script>
</body>
</html>
"""


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/status")
def api_status():
    return jsonify(
        {
            "status": "online",
            "has_kalshi_creds": dashboard_state["has_kalshi_creds"],
            "has_openrouter_creds": dashboard_state["has_openrouter_creds"],
            "balance": dashboard_state["balance"],
            "positions_count": len(dashboard_state["positions"]),
            "positions": dashboard_state["positions"][:10],
            "last_update": dashboard_state["last_update"],
            "errors": dashboard_state["errors"][-10:],
        }
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


def _monitor_loop():
    """Background thread: check for credentials, optionally connect to Kalshi API."""
    global dashboard_state
    time.sleep(1)
    dashboard_state["status"] = "online"

    kalshi_key = os.environ.get("KALSHI_API_KEY", "")
    kalshi_private_key = os.environ.get("KALSHI_PRIVATE_KEY", "") or os.environ.get(
        "KALSHI_PRIVATE_KEY_PATH", ""
    )
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")

    dashboard_state["has_kalshi_creds"] = bool(kalshi_key and kalshi_private_key)
    dashboard_state["has_openrouter_creds"] = bool(openrouter_key)

    if not dashboard_state["has_kalshi_creds"]:
        dashboard_state["errors"].append(
            {
                "time": time.strftime("%H:%M:%S"),
                "error": "Kalshi credentials not configured — running in info mode",
            }
        )
        dashboard_state["last_update"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return

    import asyncio

    try:
        from src.clients.kalshi_client import KalshiClient

        async def check_balance():
            client = KalshiClient()
            try:
                await client.initialize()
                result = await client.get_balance()
                return result
            finally:
                await client.close()

        result = asyncio.run(check_balance())
        if result:
            dashboard_state["balance"] = result.get("balance", result)
            dashboard_state["errors"].append(
                {"time": time.strftime("%H:%M:%S"), "error": "Kalshi connected OK"}
            )
    except Exception as e:
        dashboard_state["errors"].append(
            {"time": time.strftime("%H:%M:%S"), "error": f"Kalshi connect failed: {e}"}
        )

    dashboard_state["last_update"] = time.strftime("%Y-%m-%d %H:%M:%S")


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

    port = int(os.environ.get("PORT", 8080))
    print(f"[web_dashboard] Kalshi-Frigo dashboard starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
