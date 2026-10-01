"""Tests for the Railway-served web dashboard.

These cover regressions that made the deployed dashboard non-functional:
DB-backed endpoints returning 500, the SSE feed silently dropping every
listener, and config saves corrupting typed settings.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import aiosqlite
import pytest

import web_dashboard as wd

TEST_TOKEN = "test-dashboard-token"


@pytest.fixture
def client(monkeypatch, tmp_path):
    """A Flask test client with the DB and log directory redirected to tmp_path."""
    monkeypatch.setattr(wd, "DB_PATH", str(tmp_path / "trading_system.db"))
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setitem(wd.dashboard_state, "errors", [])
    monkeypatch.setitem(wd.dashboard_state, "positions", [])
    monkeypatch.setenv("DASHBOARD_TOKEN", TEST_TOKEN)
    with wd.app.test_client() as c:
        yield c


@pytest.fixture
def auth():
    """Headers that satisfy the write token gate."""
    return {"X-Auth-Token": TEST_TOKEN}


@pytest.fixture
def unlocked(monkeypatch, tmp_path):
    """A client whose token gate is locked because DASHBOARD_TOKEN is unset."""
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.setattr(wd, "DB_PATH", str(tmp_path / "locked.db"))
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path / "logs")
    with wd.app.test_client() as c:
        yield c


def _db():
    return wd._db()


# ---------------------------------------------------------------------------
# Health / status
# ---------------------------------------------------------------------------
def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Kalshi-Frigo" in r.data


# ---------------------------------------------------------------------------
# Regression: / used to return a static shell whose values were all "--" or
# "loading" until client-side JS ran. The page must now ship the real numbers
# in the HTML itself, so view-source and non-JS clients see actual data.
# ---------------------------------------------------------------------------
def _seed_activity(client):
    """Write a position and some closed trades, then return the rendered page."""
    import aiosqlite

    _db()  # create the schema outside the event loop used by the routes

    async def seed():
        async with aiosqlite.connect(wd.DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO positions (market_id, side, entry_price, quantity,"
                " timestamp, live, status, strategy, stop_loss_price, take_profit_price)"
                " VALUES ('KXTESTMKT', 'YES', 0.42, 10, '2026-01-01T00:00:00', 1,"
                " 'open', 'ai_directional', 0.35, 0.58)"
            )
            # Third trade has no strategy tag: track.py never sets one, so the
            # totals must not silently drop it.
            for pnl, strategy in (
                (1.5, "ai_directional"),
                (-0.75, "safe_compounder"),
                (2.25, None),
            ):
                await conn.execute(
                    "INSERT INTO trade_logs (market_id, side, entry_price, exit_price,"
                    " quantity, pnl, entry_timestamp, exit_timestamp, rationale, strategy)"
                    " VALUES (?, 'yes', 0.40, 0.45, 5, ?, '2026-01-01T00:00:00',"
                    " '2026-01-01T01:00:00', 'test', ?)",
                    (f"KXTRADE{int(pnl * 100)}", pnl, strategy),
                )
            await conn.commit()

    asyncio.run(seed())
    return client.get("/").get_data(as_text=True)


def test_index_is_server_rendered_with_positions(client):
    html = _seed_activity(client)
    assert "KXTESTMKT" in html, "open position missing from server-rendered HTML"
    assert "0.35" in html and "0.58" in html, "stop-loss/take-profit not rendered"


def test_index_is_server_rendered_with_trade_totals(client):
    html = _seed_activity(client)
    # 1.5 - 0.75 + 2.25 = 3.0 realized, 2 wins of 3.
    assert "3.0" in html, "realized P&L not rendered"
    assert "66.7%" in html, "win rate not rendered"


def test_index_shows_unattributed_trades(client):
    """A NULL strategy must still be counted, not dropped by the aggregate."""
    html = _seed_activity(client)
    assert "unattributed" in html


def test_index_documents_the_system(client):
    """The page explains what the bot does, not just that it is up."""
    html = client.get("/").get_data(as_text=True)
    for needle in ("What this system does", "Ingest", "Decide", "Execute", "LLM directional"):
        assert needle in html, f"missing {needle!r}"


def test_index_renders_config_values(client):
    html = client.get("/").get_data(as_text=True)
    assert "max_position_size_pct" in html
    assert "0.45" in html, "min_confidence_to_trade value not rendered"


def test_snapshot_endpoint_matches_page(client):
    """The poller and the server-rendered page read the same payload."""
    _seed_activity(client)
    snap = client.get("/api/snapshot").get_json()
    assert snap["trades"]["trades"] == 3
    assert snap["trades"]["realized_pnl"] == 3.0
    assert snap["trades"]["win_rate"] == 66.7
    assert snap["open"]["positions"] == 1
    assert snap["open"]["capital"] == 4.2
    assert len(snap["positions"]) == 1
    assert len(snap["equity"]["pnl"]) == 3
    assert {r["strategy"] for r in snap["by_strategy"]} == {
        "ai_directional",
        "safe_compounder",
        "unattributed",
    }


def test_equity_curve_is_oldest_first(client):
    _seed_activity(client)
    labels = client.get("/api/snapshot").get_json()["equity"]["labels"]
    assert labels == sorted(labels)


# ---------------------------------------------------------------------------
# Kalshi portfolio payload.
# The endpoint returns two shapes and neither has `side`/`position`/
# `last_entry_price`; every money field is a decimal *string*. The old fallback
# looked up those missing keys, so every row rendered "?" and 0.
# ---------------------------------------------------------------------------
KALSHI_MARKET_POSITION = {
    "exchange_index": 1,
    "fees_paid_dollars": "0.005000",
    "last_updated_ts": "2026-08-27T12:38:10.888325Z",
    "market_exposure_dollars": "1.850000",
    "position_fp": "4.00",
    "realized_pnl_dollars": "0.330000",
    "ticker": "KXPRES-26-BIDEN",
    "total_traded_dollars": "9.400000",
}
KALSHI_EVENT_POSITION = {
    "event_exposure_dollars": "0.000000",
    "event_ticker": "KXMLB-26",
    "fees_paid_dollars": "0.050600",
    "realized_pnl_dollars": "-0.026000",
    "total_cost_dollars": "7.026000",
    "total_cost_shares_fp": "14.00",
}


def _load_kalshi(client):
    """Install a realistic Kalshi payload for the duration of a request."""
    wd.dashboard_state["positions"] = [
        dict(KALSHI_MARKET_POSITION),
        dict(KALSHI_EVENT_POSITION),
    ]
    try:
        yield
    finally:
        wd.dashboard_state["positions"] = []


def test_kalshi_account_normalises_both_shapes(client):
    gen = _load_kalshi(client)
    next(gen)
    try:
        snap = client.get("/api/snapshot").get_json()
    finally:
        gen.close()
    k = snap["kalshi"]
    assert k["connected"] is True
    assert k["market_count"] == 1
    assert k["event_count"] == 1
    # Decimal strings must become floats, not stay as text.
    assert k["exposure"] == 1.85
    assert k["cost_basis"] == 7.03
    assert k["realized"] == 0.30  # 0.33 - 0.026
    assert k["fees"] == 0.06  # 0.005 + 0.0506
    assert k["traded"] == 9.4


def test_kalshi_rows_carry_real_values(client):
    gen = _load_kalshi(client)
    next(gen)
    try:
        html = client.get("/").get_data(as_text=True)
    finally:
        gen.close()
    assert "KXPRES-26-BIDEN" in html
    assert "KXMLB-26" in html
    # The old code emitted a literal "?" for the missing `side` field.
    assert ">?<" not in html


def test_dry_headline_shows_the_simulated_book_not_real_money(client):
    """In DRY the headline must be the $300 simulated account.

    It used to lead with the real Kalshi balance, real position count and real
    realized P&L, so a DRY page contradicted its own DRY MODE flag.
    """
    gen = _load_kalshi(client)
    next(gen)
    try:
        html = client.get("/").get_data(as_text=True)
    finally:
        gen.close()
    tiles = html.split('<div class="tiles">', 1)[1].split("<!-- ============ readiness", 1)[0]
    assert "DRY cash" in tiles
    assert "DRY positions" in tiles
    assert "DRY deployed" in tiles
    assert "DRY realized P&amp;L" in tiles
    # The real account's money must not appear in the DRY headline.
    assert "$300.00" in tiles
    assert "Kalshi balance" not in tiles
    assert "Live positions" not in tiles


def test_dry_page_labels_the_real_account_as_not_being_traded(client):
    gen = _load_kalshi(client)
    next(gen)
    try:
        html = client.get("/").get_data(as_text=True)
    finally:
        gen.close()
    assert "Real Kalshi account — NOT what DRY is trading" in html
    assert "managed outside this bot" in html
    assert "none of these figures describe the bot's results" in html


def test_live_headline_shows_the_real_account(client, auth, monkeypatch):
    """LIVE flips the headline back to real money."""
    from src.utils.mode import TradingMode, run

    async def funded(_self, private_key_path=None):
        return {
            "connected": True,
            "balance": 5000.0,
            "balance_cents": 500000,
            "can_fund": True,
            "reason": "",
        }

    monkeypatch.setattr(TradingMode, "funding", funded)
    client.post("/api/mode", json={"mode": "live", "confirm": True}, headers=auth)

    html = client.get("/").get_data(as_text=True)
    tiles = html.split('<div class="tiles">', 1)[1].split("<!-- ============ readiness", 1)[0]
    assert "Real balance" in tiles
    assert "Live positions" in tiles
    assert "Real exposure" in tiles
    assert "DRY cash" not in tiles


def test_dry_open_positions_are_counted_separately(client):
    """DRY positions are live=0, so the simulated book has its own count."""
    snap = client.get("/api/snapshot").get_json()
    assert "open_dry" in snap
    assert snap["open_dry"]["positions"] == 0
    assert snap["open_dry"]["capital"] == 0.0


def test_kalshi_positions_stand_in_for_empty_db(client):
    """With an empty DB the Kalshi account is the only source of positions."""
    gen = _load_kalshi(client)
    next(gen)
    try:
        snap = client.get("/api/snapshot").get_json()
    finally:
        gen.close()
    assert snap["open"]["positions"] == 0  # nothing in SQLite
    assert len(snap["positions"]) == 2  # but two live from Kalshi
    assert all(p["strategy"] == "kalshi_api" for p in snap["positions"])


def test_as_float_handles_none_and_garbage():
    assert wd._as_float(None) == 0.0
    assert wd._as_float("") == 0.0
    assert wd._as_float("not-a-number") == 0.0
    assert wd._as_float("-0.012000") == -0.012


def test_kalshi_configured_false_without_credentials(client, monkeypatch):
    for var in (
        "KALSHI_API_KEY",
        "KALSHI_PRIVATE_KEY",
        "KALSHI_PRIVATE_KEY_PATH",
        "DB_PATH",
        "RAILWAY_VOLUME_MOUNT_PATH",
    ):
        monkeypatch.delenv(var, raising=False)
    assert wd.kalshi_configured() is False


def test_kalshi_configured_true_with_api_key_and_pem(client, monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY", "kid")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nabc\n")
    assert wd.kalshi_configured() is True


def test_private_key_env_var_is_written_to_a_readable_file(client, monkeypatch):
    """KalshiClient reads a path, but Railway can only inject env vars."""
    pem = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", pem)
    path = wd.materialize_private_key()
    assert path and wd.Path(path).exists()
    assert wd.Path(path).read_text().strip() == pem.strip()


def test_fetch_kalshi_data_skips_network_without_credentials(client, monkeypatch):
    """Must not build a KalshiClient (it raises without a key) or hit the API."""
    for var in ("KALSHI_API_KEY", "KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(var, raising=False)

    def explode(*a, **kw):
        raise AssertionError("must not construct KalshiClient without credentials")

    import src.clients.kalshi_client as kc

    monkeypatch.setattr(kc, "KalshiClient", explode)
    assert wd._run_async(wd._fetch_kalshi_data()) == (None, None)


def test_status_reports_uptime_and_db(client):
    body = client.get("/api/status").get_json()
    assert "uptime_sec" in body
    assert "db_path" in body
    assert "running_strategies" in body


# ---------------------------------------------------------------------------
# Regression: these three endpoints used to raise
# "'module' object is not callable" and return 500
# ---------------------------------------------------------------------------
def test_positions_endpoint_returns_200(client):
    r = client.get("/api/positions")
    assert r.status_code == 200
    assert "positions" in r.get_json()


def test_pnl_chart_returns_200(client):
    r = client.get("/api/chart/pnl")
    assert r.status_code == 200
    body = r.get_json()
    assert body["labels"] and body["pnl"]


def test_performance_endpoint_returns_200(client):
    r = client.get("/api/chart/performance")
    assert r.status_code == 200


def test_pnl_chart_survives_rows(client):
    """A populated trade_logs table must not break the chart query."""
    import aiosqlite

    _db()  # create schema outside the loop below

    async def seed():
        async with aiosqlite.connect(wd.DB_PATH) as conn:
            for i in range(3):
                await conn.execute(
                    "INSERT INTO trade_logs (market_id, side, entry_price, exit_price,"
                    " quantity, pnl, entry_timestamp, exit_timestamp, rationale)"
                    " VALUES (?, 'yes', 0.4, 0.5, 1, ?, '2026-01-01T00:00:00',"
                    " '2026-01-01T0%d:00:00', 'test')" % i,
                    (f"MKT{i}", float(i)),
                )
            await conn.commit()

    asyncio.run(seed())
    body = client.get("/api/chart/pnl").get_json()
    assert len(body["labels"]) == 3
    assert body["pnl"] == [0.0, 1.0, 3.0]


# ---------------------------------------------------------------------------
# Regression: _broadcast called deque.put_nowait(), which does not exist on a
# deque, so the first event removed every listener and the feed went dead.
# ---------------------------------------------------------------------------
def test_broadcast_keeps_listeners():
    from collections import deque

    listeners = wd.dashboard_state["sse_listeners"]
    original = list(listeners)
    listeners.clear()
    try:
        q = deque(maxlen=5)
        listeners.append(q)
        wd._broadcast("strategy", {"action": "started"})
        assert listeners == [q], "listener was dropped by _broadcast"
        assert len(q) == 1
    finally:
        listeners.clear()
        listeners.extend(original)


def test_stream_emits_connected_frame(client):
    resp = client.get("/api/stream", buffered=False)
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    it = resp.response
    first = next(it).decode()
    assert "connected" in first
    # Must be an unnamed event so the browser's onmessage handler fires.
    assert not first.startswith("event:")


# ---------------------------------------------------------------------------
# Log viewer
# ---------------------------------------------------------------------------
def test_logs_reads_newest_log_file(client, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "trading_system_20260101_000000.log").write_text("old line\n")
    (logs / "latest.log").write_text("new line\n")
    body = client.get("/api/logs?lines=50").get_json()
    assert "new line" in body["lines"]


def test_logs_handles_bad_line_count(client):
    assert client.get("/api/logs?lines=abc").status_code == 200
    assert client.get("/api/logs?lines=99999999").status_code == 200


# ---------------------------------------------------------------------------
# Config editor typing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "current,raw,expected",
    [
        (True, "false", False),
        (False, "true", True),
        (True, "1", True),
        (10, "7", 7),
        (60, "90.5", 90),
        (3.0, "2.5", 2.5),
    ],
)
def test_coerce_like(current, raw, expected):
    assert wd._coerce_like(current, raw) == expected


def test_coerce_like_keeps_current_on_bad_input():
    assert wd._coerce_like(10, "abc") == 10
    assert wd._coerce_like(1.5, "xyz") == 1.5


def test_config_post_preserves_types(client, auth):
    from src.config.settings import settings

    before_max = settings.trading.max_positions
    before_bool = settings.trading.live_trading_enabled
    try:
        r = client.post(
            "/api/config",
            json={"max_positions": "7", "live_trading_enabled": "true", "bogus": "x"},
            headers=auth,
        )
        body = r.get_json()
        assert r.status_code == 200
        assert body["ok"] is True
        assert "bogus" in body["skipped"]
        assert isinstance(settings.trading.max_positions, int)
        assert settings.trading.max_positions == 7
        assert settings.trading.live_trading_enabled is True
    finally:
        settings.trading.max_positions = before_max
        settings.trading.live_trading_enabled = before_bool


def test_config_rejects_unknown_fields(client, auth):
    from src.config.settings import settings

    assert not hasattr(settings.trading, "definitely_not_a_field")
    r = client.post("/api/config", json={"definitely_not_a_field": 1}, headers=auth)
    assert r.get_json()["skipped"] == ["definitely_not_a_field"]


# ---------------------------------------------------------------------------
# Strategy control
# ---------------------------------------------------------------------------
def test_toggle_unknown_strategy_404(client, auth):
    r = client.post("/api/strategy/nope/toggle", json={"mode": "paper"}, headers=auth)
    assert r.status_code == 404


def test_toggle_rejects_live_without_credentials(client, auth, monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY", raising=False)
    r = client.post("/api/strategy/ai_directional/toggle", json={"mode": "live"}, headers=auth)
    assert r.status_code == 400


def test_toggle_rejects_invalid_mode(client, auth):
    r = client.post("/api/strategy/ai_directional/toggle", json={"mode": "yolo"}, headers=auth)
    assert r.status_code == 400


def test_toggle_refuses_without_kalshi_credentials(client, auth, monkeypatch):
    """Paper mode still boots a KalshiClient and dies without credentials.

    The button must not report success for a process that is about to exit.
    """
    monkeypatch.delenv("KALSHI_API_KEY", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    r = client.post("/api/strategy/safe_compounder/toggle", json={"mode": "paper"}, headers=auth)
    assert r.status_code == 400
    assert "credentials" in r.get_json()["error"].lower()
    assert not any(s["pid"] for s in client.get("/api/bots").get_json())


def test_toggle_refuses_when_only_api_key_present(client, auth, monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY", "kid")
    monkeypatch.delenv("KALSHI_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    r = client.post("/api/strategy/safe_compounder/toggle", json={"mode": "paper"}, headers=auth)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Liveness reconciliation
# ---------------------------------------------------------------------------
def test_dead_child_is_not_reported_running(client):
    """A crashed subprocess must stop being reported as running."""
    st = wd.strategy_state["safe_compounder"]
    st["running"] = True
    st["pid"] = 424_242  # not a live process
    try:
        bots = {b["name"]: b for b in client.get("/api/bots").get_json()}
        assert bots["safe_compounder"]["running"] is False
        assert bots["safe_compounder"]["pid"] is None
        # /api/status must agree with /api/bots.
        assert client.get("/api/status").get_json()["running_strategies"] == 0
    finally:
        st["running"] = False
        st["pid"] = None


def test_exited_popen_detected_via_poll(client):
    """A Popen whose child already exited is dead even if the pid is reused."""
    import subprocess as sp
    import time as _t

    proc = sp.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    wd._child_procs[proc.pid] = proc
    st = wd.strategy_state["beast_mode"]
    st["running"] = True
    st["pid"] = proc.pid
    try:
        assert wd._pid_alive(proc.pid) is False
        assert "beast_mode" not in wd._running_strategies()
    finally:
        wd._child_procs.pop(proc.pid, None)
        st["running"] = False
        st["pid"] = None
    del _t


def test_status_and_bots_agree(client):
    """The Running tile and the bot table must never disagree."""
    for st in wd.strategy_state.values():
        st["running"] = False
        st["pid"] = None
    bots = {b["name"]: b["running"] for b in client.get("/api/bots").get_json()}
    status = client.get("/api/status").get_json()
    assert status["running_strategies"] == sum(1 for v in bots.values() if v)


def test_toggle_starts_process_when_credentials_present(client, auth, monkeypatch, tmp_path):
    """The new credential gate must not block a legitimate start."""
    monkeypatch.setenv("KALSHI_API_KEY", "kid")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----")
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path / "logs")
    spawned = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            spawned["cmd"] = cmd
            spawned.update(kw)
            self.pid = 31337
            self._rc = None

        def poll(self):
            return self._rc

        def terminate(self):
            self._rc = 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self._rc = -9

    monkeypatch.setattr(wd.subprocess, "Popen", FakePopen)
    st = wd.strategy_state["safe_compounder"]
    try:
        r = client.post(
            "/api/strategy/safe_compounder/toggle", json={"mode": "paper"}, headers=auth
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["running"] is True
        assert body["pid"] == 31337
        # Must use this interpreter, not whatever `python` resolves to on PATH.
        assert spawned["cmd"][0] == sys.executable
        assert "--safe-compounder" in spawned["cmd"]
        assert "--paper" in spawned["cmd"]
        # Output must not go to an undrained PIPE (that deadlocks the child
        # once the ~64KB buffer fills); stderr folds into the same log file.
        assert spawned["stderr"] == wd.subprocess.STDOUT
        assert spawned["stdin"] == wd.subprocess.DEVNULL
        assert spawned["stdout"] is not wd.subprocess.PIPE

        bots = {b["name"]: b for b in client.get("/api/bots").get_json()}
        assert bots["safe_compounder"]["running"] is True

        r2 = client.post(
            "/api/strategy/safe_compounder/toggle", json={"mode": "paper"}, headers=auth
        )
        assert r2.get_json()["running"] is False
    finally:
        st["running"] = False
        st["pid"] = None
        wd._child_procs.pop(31337, None)


def test_kill_when_not_running_is_noop(client, auth):
    r = client.post("/api/bot/ai_directional/kill", headers=auth)
    assert r.status_code == 200
    assert r.get_json()["killed"] is False


def test_bots_listing_shape(client):
    body = client.get("/api/bots").get_json()
    assert isinstance(body, list)
    assert {"name", "pid", "mode", "running"} <= set(body[0])


def test_stop_child_handles_missing_process():
    """A stale/absent pid must not raise (no SIGKILL on Windows)."""
    wd._stop_child({"pid": 999_999_99, "running": True})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def test_error_buffer_is_bounded():
    errors = []
    saved = wd.dashboard_state["errors"]
    wd.dashboard_state["errors"] = errors
    try:
        for _ in range(wd.MAX_ERRORS + 25):
            wd._push_error("boom")
        assert len(errors) == wd.MAX_ERRORS
    finally:
        wd.dashboard_state["errors"] = saved


def test_run_async_closes_loop():
    async def coro():
        return 42

    assert wd._run_async(coro()) == 42
    # A closed loop cannot be reused; a fresh one must be created per call.
    assert wd._run_async(coro()) == 42


def test_sse_generator_unregisters_listener():
    """The generator must clean up its listener when the client disconnects."""
    from collections import deque

    q = deque(maxlen=5)
    listeners = wd.dashboard_state["sse_listeners"]
    original = list(listeners)
    listeners.clear()
    listeners.append(q)
    try:
        gen = wd.api_stream().response
        next(gen)  # registers q and emits the connected frame
        assert any(x is q for x in listeners)
        gen.close()
        # Identity check: two empty deques compare equal, so `not in` is wrong.
        assert not any(x is q for x in listeners)
    finally:
        listeners.clear()
        listeners.extend(original)


def test_run_async_rejects_nested_loop():
    """A clear error beats 'Cannot run the event loop while another loop is running'."""

    async def outer():
        async def inner():
            return 1

        with pytest.raises(RuntimeError, match="inside a running event loop"):
            wd._run_async(inner())

    asyncio.run(outer())


def test_healthcheck_is_fast_and_offline(client, monkeypatch):
    """The healthcheck must never reach the network or the DB."""
    monkeypatch.setattr(
        wd, "_fetch_kalshi_data", lambda: (_ for _ in ()).throw(AssertionError("network"))
    )
    start = time.time()
    assert client.get("/health").status_code == 200
    assert time.time() - start < 2.0


# ---------------------------------------------------------------------------
# Token gate.
# The dashboard is public. Every mutating route used to be an open POST, which
# with credentials present meant anyone could spawn a trading process.
# ---------------------------------------------------------------------------
MUTATING_ROUTES = [
    ("/api/mode", {"mode": "dry"}),
    ("/api/dry/reset", {}),
    ("/api/strategy/ai_directional/toggle", {"mode": "paper"}),
    ("/api/bot/ai_directional/kill", {}),
    ("/api/config", {"max_positions": 5}),
    ("/api/alerts", {"enabled": True}),
]


@pytest.mark.parametrize("path,body", MUTATING_ROUTES)
def test_mutating_routes_reject_missing_token(client, path, body):
    assert client.post(path, json=body).status_code == 401


@pytest.mark.parametrize("path,body", MUTATING_ROUTES)
def test_mutating_routes_reject_wrong_token(client, path, body):
    r = client.post(path, json=body, headers={"X-Auth-Token": "nope"})
    assert r.status_code == 401
    assert "Unauthorized" in r.get_json()["error"]


@pytest.mark.parametrize("path,body", MUTATING_ROUTES)
def test_mutating_routes_fail_closed_without_configured_token(unlocked, path, body):
    """No DASHBOARD_TOKEN must lock writes, not open them."""
    r = unlocked.post(path, json=body)
    assert r.status_code == 503, "writes must fail closed"
    assert "DASHBOARD_TOKEN" in r.get_json()["error"]


def test_reads_stay_public(client):
    """The token gates writes only - the dashboard must remain viewable."""
    for path in ("/", "/health", "/api/status", "/api/snapshot", "/api/mode"):
        assert client.get(path).status_code == 200


def test_token_accepted_from_header(client, auth):
    r = client.post("/api/dry/reset", headers=auth)
    assert r.status_code == 200


def test_token_accepted_from_json_body(client):
    r = client.post("/api/dry/reset", json={"token": TEST_TOKEN})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# DRY / LIVE mode
# ---------------------------------------------------------------------------
def test_mode_defaults_to_dry(client):
    assert client.get("/api/mode").get_json()["mode"] == "dry"


def test_dry_account_starts_at_300(client):
    dry = client.get("/api/mode").get_json()["dry"]
    assert dry["starting_balance"] == 300.0
    assert dry["cash"] == 300.0
    assert dry["equity"] == 300.0
    assert dry["total_pnl"] == 0.0


def test_mode_rejects_unknown_value(client, auth):
    r = client.post("/api/mode", json={"mode": "yolo"}, headers=auth)
    assert r.status_code == 400


def test_going_live_requires_explicit_confirmation(client, auth):
    r = client.post("/api/mode", json={"mode": "live"}, headers=auth)
    assert r.status_code == 400
    assert "real money" in r.get_json()["error"]
    assert client.get("/api/mode").get_json()["mode"] == "dry"


def test_going_live_blocked_when_unfunded(client, auth, monkeypatch):
    """A $0 balance must not be switchable to LIVE."""
    from src.utils.mode import TradingMode

    async def unfunded(_self, private_key_path=None):
        return {
            "connected": True,
            "balance": 0.0,
            "balance_cents": 0,
            "can_fund": False,
            "reason": "balance $0.00 is below Kalshi's $1.00 minimum",
        }

    monkeypatch.setattr(TradingMode, "funding", unfunded)
    r = client.post("/api/mode", json={"mode": "live", "confirm": True}, headers=auth)
    assert r.status_code == 400
    assert "Cannot go LIVE" in r.get_json()["error"]


def test_dry_to_live_and_back(client, auth, monkeypatch):
    from src.utils.mode import TradingMode

    async def funded(_self, private_key_path=None):
        return {
            "connected": True,
            "balance": 5000.0,
            "balance_cents": 500000,
            "can_fund": True,
            "reason": "",
        }

    monkeypatch.setattr(TradingMode, "funding", funded)
    r = client.post("/api/mode", json={"mode": "live", "confirm": True}, headers=auth)
    assert r.status_code == 200
    assert r.get_json()["mode"] == "live"

    r2 = client.post("/api/mode", json={"mode": "dry", "confirm": True}, headers=auth)
    assert r2.status_code == 200
    assert r2.get_json()["mode"] == "dry"


def test_mode_is_persisted_across_manager_instances(client, auth, monkeypatch):
    """Mode lives in the DB, not process memory, so a redeploy cannot revert it."""
    from src.utils.mode import TradingMode, run

    async def funded(_self, private_key_path=None):
        return {
            "connected": True,
            "balance": 5000.0,
            "balance_cents": 500000,
            "can_fund": True,
            "reason": "",
        }

    monkeypatch.setattr(TradingMode, "funding", funded)
    client.post("/api/mode", json={"mode": "live", "confirm": True}, headers=auth)
    # A brand-new manager against the same file must agree.
    fresh = TradingMode(db_path=wd.DB_PATH)
    assert run(fresh.current()) == "live"


def test_dry_reset_restores_starting_balance(client, auth):
    from src.utils.mode import TradingMode, run

    mgr = TradingMode(db_path=wd.DB_PATH)
    run(mgr.set_dry_cash(12.5))
    assert client.get("/api/mode").get_json()["dry"]["cash"] == 12.5
    r = client.post("/api/dry/reset", headers=auth)
    assert r.status_code == 200
    assert r.get_json()["dry"]["cash"] == 300.0


# ---------------------------------------------------------------------------
# DRY cash ledger
# ---------------------------------------------------------------------------
def test_ledger_debits_and_credits(client):
    from src.utils.mode import TradingMode, run

    mgr = TradingMode(db_path=wd.DB_PATH)
    run(mgr.record_fill(market_id="KXTEST", side="YES", action="buy", quantity=10, price=0.5))
    assert mgr and run(mgr.dry_account())["cash"] == 295.0
    run(mgr.record_fill(market_id="KXTEST", side="YES", action="sell", quantity=10, price=0.65))
    assert run(mgr.dry_account())["cash"] == 301.5
    assert run(mgr.ledger(10))[0]["action"] == "sell"


def test_ledger_refuses_overspend(client):
    from src.utils.mode import ModeError, TradingMode, run

    mgr = TradingMode(db_path=wd.DB_PATH)
    try:
        # 1000 x $0.90 = $900, comfortably past the $300 DRY balance.
        run(mgr.record_fill(market_id="KXTEST", side="YES", action="buy", quantity=1000, price=0.9))
        raise AssertionError("overspend must be rejected")
    except ModeError as exc:
        assert "Insufficient simulated funds" in str(exc)
    assert run(mgr.dry_account())["cash"] == 300.0


def test_ledger_rejects_bad_action(client):
    from src.utils.mode import ModeError, TradingMode, run

    mgr = TradingMode(db_path=wd.DB_PATH)
    try:
        run(mgr.record_fill(market_id="KXTEST", side="YES", action="short", quantity=1, price=0.5))
        raise AssertionError("bad action must be rejected")
    except ModeError:
        pass


def test_dry_account_tolerates_missing_tables(tmp_path):
    """The mode store can be the first thing to touch a brand-new database."""
    from src.utils.mode import TradingMode, run

    mgr = TradingMode(db_path=str(tmp_path / "brand_new.db"))
    acct = run(mgr.dry_account())
    assert acct["cash"] == 300.0
    assert acct["closed_trades"] == 0


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_page_renders_mode_switch_and_dry_account(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="modeDry"' in html
    assert 'id="modeLive"' in html
    assert "DRY account" in html
    assert "$300.00" in html, "DRY starting balance not rendered"
    assert "Funding source" in html


def test_page_shows_write_lock_notice_when_token_unset(unlocked):
    html = unlocked.get("/").get_data(as_text=True)
    assert "Write actions are locked" in html


# ---------------------------------------------------------------------------
# Mode must be unmistakable.
# Clicking LIVE and being refused (or cancelling) used to look identical to
# actually being in LIVE, so the page now carries the state in several
# independent places: a full-viewport frame, a body attribute, a text flag,
# the document title and the favicon.
# ---------------------------------------------------------------------------
def test_page_carries_mode_in_body_attribute(client):
    html = client.get("/").get_data(as_text=True)
    assert '<body data-mode="dry">' in html


def test_page_has_full_viewport_mode_frame(client):
    html = client.get("/").get_data(as_text=True)
    assert "position:fixed;inset:0;z-index:9999" in html
    assert "@keyframes livepulse" in html, "LIVE needs an animation, not just a colour"


def test_page_shows_a_text_mode_flag(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="modeFlag"' in html
    assert 'id="modeFlagText"' in html
    assert "DRY MODE" in html


def test_page_title_and_favicon_track_mode(client):
    """The browser tab itself shows which mode is active."""
    html = client.get("/").get_data(as_text=True)
    assert "DRY Trading Dashboard" in html
    assert 'id="favicon"' in html
    # Both colours are present: the template picks per mode.
    assert "%232ee6a8" in html and "%23ff5c7a" in html


def test_page_embeds_write_token_so_controls_do_not_prompt(client):
    html = client.get("/").get_data(as_text=True)
    assert f'const WRITE_TOKEN = "{TEST_TOKEN}"' in html
    assert "ensureToken" not in html, "the prompt path should be gone"


def test_snapshot_endpoint_does_not_leak_the_token(client):
    """The token goes to the HTML only - /api/snapshot is an open GET."""
    body = client.get("/api/snapshot").get_json()
    assert "token" not in body
    assert TEST_TOKEN not in str(body)


def test_mode_payload_does_not_leak_the_token(client):
    body = client.get("/api/mode").get_json()
    assert "token" not in body
    assert body.get("token_set") is True
    assert TEST_TOKEN not in str(body)


def test_live_mode_renders_red_indicators(client, auth, monkeypatch):
    """When the persisted mode is LIVE the page must say so unambiguously."""
    from src.utils.mode import TradingMode, run

    async def funded(_self, private_key_path=None):
        return {
            "connected": True,
            "balance": 5000.0,
            "balance_cents": 500000,
            "can_fund": True,
            "reason": "",
        }

    monkeypatch.setattr(TradingMode, "funding", funded)
    r = client.post("/api/mode", json={"mode": "live", "confirm": True}, headers=auth)
    assert r.get_json()["mode"] == "live"

    html = client.get("/").get_data(as_text=True)
    assert '<body data-mode="live">' in html
    assert '<span id="modeFlagText">LIVE MODE</span>' in html
    assert "LIVE Trading Dashboard" in html
    assert "REAL ORDERS, REAL MONEY" in html or "real orders" in html.lower()


def test_refused_live_switch_leaves_the_page_in_dry(client, auth):
    """A refused switch must not leave any trace of LIVE on the page."""
    r = client.post("/api/mode", json={"mode": "live", "confirm": True}, headers=auth)
    assert r.status_code == 400, "balance is $0 in tests, so LIVE must refuse"
    html = client.get("/").get_data(as_text=True)
    assert '<body data-mode="dry">' in html
    # Check the rendered flag element, not the JS source, which mentions both.
    assert '<span id="modeFlagText">DRY MODE</span>' in html
    assert '<span id="modeFlagText">LIVE MODE</span>' not in html


# ---------------------------------------------------------------------------
# Regression: the column map.
#
# get_open_positions() built Position from positional indexes against
# `SELECT *`. The positions table has 15 columns, so every index from 10 on was
# shifted by one and `stop_loss_price` came back holding the `strategy` column -
# a TEXT string. Every exit that used those levels would have fired at a
# strategy name. Positional access is now banned; named access is used.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_position_columns_map_by_name(tmp_path):
    from datetime import datetime

    from src.utils.database import DatabaseManager, Position

    db_path = str(tmp_path / "colmap.db")
    db = DatabaseManager(db_path=db_path)
    await db.initialize()
    pos = Position(
        market_id="KXCOLMAP-26",
        side="YES",
        entry_price=0.40,
        quantity=7,
        timestamp=datetime(2026, 1, 1),
        rationale="r",
        strategy="safe_compounder",
        live=True,
        stop_loss_price=0.30,
        take_profit_price=0.55,
        max_hold_hours=48,
        target_confidence_change=0.25,
    )
    await db.add_position(pos)

    rows = await db.get_open_positions()
    assert len(rows) == 1
    got = rows[0]
    # Every field that was previously shifted.
    assert got.stop_loss_price == 0.30
    assert got.take_profit_price == 0.55
    assert got.max_hold_hours == 48
    assert got.target_confidence_change == 0.25
    assert got.strategy == "safe_compounder"
    assert got.market_id == "KXCOLMAP-26"
    assert got.quantity == 7
    assert got.entry_price == 0.40


@pytest.mark.asyncio
async def test_stop_loss_is_never_a_strategy_string(tmp_path):
    """The exact failure: stop_loss_price holding 'ai_directional'."""
    from datetime import datetime

    from src.utils.database import DatabaseManager, Position

    db_path = str(tmp_path / "sltest.db")
    db = DatabaseManager(db_path=db_path)
    await db.initialize()
    await db.add_position(
        Position(
            market_id="KXBAD-26",
            side="NO",
            entry_price=0.60,
            quantity=3,
            timestamp=datetime(2026, 1, 1),
            rationale="r",
            strategy="ai_directional",
            live=True,
            stop_loss_price=0.45,
            take_profit_price=0.80,
        )
    )
    got = (await db.get_open_positions())[0]
    assert isinstance(got.stop_loss_price, float)
    assert got.stop_loss_price == 0.45
    assert got.stop_loss_price != "ai_directional"


@pytest.mark.asyncio
async def test_migrations_add_mode_and_blocked_trades(tmp_path):
    """cli.py queries blocked_trades unguarded; it must exist after initialize()."""
    import sqlite3

    from src.utils.database import DatabaseManager

    db_path = str(tmp_path / "mig.db")
    db = DatabaseManager(db_path=db_path)
    await db.initialize()

    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    pos_cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)")}
    log_cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_logs)")}
    conn.close()

    assert "blocked_trades" in tables
    assert {"mode", "strategy", "stop_loss_price", "take_profit_price"} <= pos_cols
    assert {"strategy", "exit_reason", "mode"} <= log_cols


@pytest.mark.asyncio
async def test_position_is_stamped_with_a_mode(tmp_path):
    from datetime import datetime

    from src.utils.database import DatabaseManager, Position

    db_path = str(tmp_path / "stamp.db")
    db = DatabaseManager(db_path=db_path)
    await db.initialize()
    await db.add_position(
        Position(
            market_id="KXSTAMP-26",
            side="YES",
            entry_price=0.5,
            quantity=1,
            timestamp=datetime(2026, 1, 1),
            rationale="r",
            live=False,
        )
    )
    got = (await db.get_open_positions())[0]
    assert got.mode == "dry", "a fresh database resolves to DRY"


@pytest.mark.asyncio
async def test_trackers_see_dry_positions(tmp_path):
    """DRY positions must be visible to exit management.

    They used to be filtered out by `live = 1`, which meant DRY opened trades
    that were never exited.
    """
    from datetime import datetime

    from src.utils.database import DatabaseManager, Position

    db_path = str(tmp_path / "vis.db")
    db = DatabaseManager(db_path=db_path)
    await db.initialize()
    await db.add_position(
        Position(
            market_id="KXVIS-26",
            side="YES",
            entry_price=0.5,
            quantity=2,
            timestamp=datetime(2026, 1, 1),
            rationale="r",
            live=False,
            mode="dry",
        )
    )
    visible = await db.get_open_live_positions()
    assert len(visible) == 1
    assert visible[0].market_id == "KXVIS-26"
    assert await db.get_open_non_live_positions() != []


@pytest.mark.asyncio
async def test_tradelog_records_strategy_exit_reason_and_mode(tmp_path):
    """track.py omitted all three, so P&L was unattributable."""
    from datetime import datetime

    from src.utils.database import DatabaseManager, TradeLog

    db_path = str(tmp_path / "tl.db")
    db = DatabaseManager(db_path=db_path)
    await db.initialize()
    await db.add_trade_log(
        TradeLog(
            market_id="KXLOG-26",
            side="YES",
            entry_price=0.4,
            exit_price=0.55,
            quantity=10,
            pnl=1.5,
            entry_timestamp=datetime(2026, 1, 1),
            exit_timestamp=datetime(2026, 1, 2),
            rationale="r",
            strategy="ai_directional",
            exit_reason="take_profit",
            mode="dry",
        )
    )
    got = (await db.get_all_trade_logs())[0]
    assert got.strategy == "ai_directional"
    assert got.exit_reason == "take_profit"
    assert got.mode == "dry"


@pytest.mark.asyncio
async def test_migration_column_added_to_old_database(tmp_path):
    """An existing database gains the new columns, it is not recreated."""
    import sqlite3

    from src.utils.database import DatabaseManager

    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    # The pre-mode schema, 14 columns and no mode.
    conn.execute(
        """CREATE TABLE positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT NOT NULL, side TEXT NOT NULL,
        entry_price REAL NOT NULL, quantity INTEGER NOT NULL, timestamp TEXT NOT NULL,
        rationale TEXT, confidence REAL, live BOOLEAN NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'open', strategy TEXT, stop_loss_price REAL,
        take_profit_price REAL)"""
    )
    conn.execute(
        "INSERT INTO positions (market_id, side, entry_price, quantity, timestamp, live)"
        " VALUES ('KXBARE-26','YES',0.5,4,'2026-01-01T00:00:00',0)"
    )
    conn.commit()
    conn.close()

    db = DatabaseManager(db_path=db_path)
    await db.initialize()

    rows = await db.get_open_positions()
    assert len(rows) == 1, "the existing row must survive the migration"
    assert rows[0].mode == "dry"


# ---------------------------------------------------------------------------
# #9 authentication
# ---------------------------------------------------------------------------
def test_write_accepts_basic_auth(client, monkeypatch):
    import base64

    monkeypatch.setenv("DASHBOARD_USER", "operator")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    creds = base64.b64encode(b"operator:hunter2").decode()
    r = client.post("/api/dry/reset", headers={"Authorization": f"Basic {creds}"})
    assert r.status_code == 200


def test_write_rejects_wrong_basic_auth(client, monkeypatch):
    import base64

    monkeypatch.setenv("DASHBOARD_USER", "operator")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    creds = base64.b64encode(b"operator:wrong").decode()
    r = client.post("/api/dry/reset", headers={"Authorization": f"Basic {creds}"})
    assert r.status_code == 401


def test_basic_auth_ignored_when_unconfigured(client):
    import base64

    monkeypatch_user = None
    creds = base64.b64encode(b"anyone:anything").decode()
    r = client.post("/api/dry/reset", headers={"Authorization": f"Basic {creds}"})
    assert r.status_code == 401, monkeypatch_user


# ---------------------------------------------------------------------------
# #10 backups
# ---------------------------------------------------------------------------
def test_backup_creates_a_snapshot(client):
    _db()  # create the database file; there is nothing to copy before this
    dest = wd.backup_database()
    assert dest and Path(dest).exists()
    assert Path(dest).parent.name == wd.BACKUP_DIR_NAME


def test_backup_prunes_old_snapshots(client):
    _db()
    out_dir = Path(wd.DB_PATH).parent / wd.BACKUP_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    # Deliberately old names, so they sort behind the ones just written.
    stale = [out_dir / f"trading_system_2000010{i}000000.db" for i in range(5)]
    for f in stale:
        f.write_bytes(b"old")
    wd.backup_database()  # one recent snapshot
    wd.BACKUP_KEEP = 2
    wd._prune_backups(out_dir)
    remaining = sorted(p.name for p in out_dir.glob("trading_system_*.db"))
    # Exactly the 2 newest survive: the fresh snapshot plus one old one.
    assert len(remaining) == 2, f"expected 2 kept, got {remaining}"
    survivors = [f.name for f in stale if f.exists()]
    assert len(survivors) == 1, f"only the newest stale file should remain: {survivors}"
    assert survivors == ["trading_system_20000104000000.db"]


def test_backup_status_counts_snapshots(client):
    _db()
    wd.backup_database()
    assert wd._backup_status()["count"] >= 1


# ---------------------------------------------------------------------------
# #3 never-run banner
# ---------------------------------------------------------------------------
def test_page_warns_when_the_pipeline_has_never_run(client):
    html = client.get("/").get_data(as_text=True)
    assert "The pipeline has never run" in html


def test_never_run_clears_once_markets_are_ingested(client):
    import asyncio

    _db()  # ensure schema
    asyncio.run(
        _db_many_insert(
            "INSERT INTO markets (market_id, title, yes_price, no_price, volume,"
            " expiration_ts, category, status, last_updated, has_position)"
            " VALUES ('KX1','t',0.5,0.5,10,'2026-12-31','Politics','open',"
            "'2026-01-01T00:00:00',0)"
        )
    )
    snap = client.get("/api/snapshot").get_json()
    assert snap["never_run"] is False
    assert "The pipeline has never run" not in client.get("/").get_data(as_text=True)


async def _db_many_insert(sql: str) -> None:
    async with aiosqlite.connect(wd.DB_PATH) as conn:
        await conn.execute(sql)
        await conn.commit()
