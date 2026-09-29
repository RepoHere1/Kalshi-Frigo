"""Tests for the Railway-served web dashboard.

These cover regressions that made the deployed dashboard non-functional:
DB-backed endpoints returning 500, the SSE feed silently dropping every
listener, and config saves corrupting typed settings.
"""
import asyncio
import json
import sys
import time

import pytest

import web_dashboard as wd


@pytest.fixture
def client(monkeypatch, tmp_path):
    """A Flask test client with the DB and log directory redirected to tmp_path."""
    monkeypatch.setattr(wd, "DB_PATH", str(tmp_path / "trading_system.db"))
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setitem(wd.dashboard_state, "errors", [])
    monkeypatch.setitem(wd.dashboard_state, "positions", [])
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


def test_config_post_preserves_types(client):
    from src.config.settings import settings

    before_max = settings.trading.max_positions
    before_bool = settings.trading.live_trading_enabled
    try:
        r = client.post(
            "/api/config",
            json={"max_positions": "7", "live_trading_enabled": "true", "bogus": "x"},
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


def test_config_rejects_unknown_fields(client):
    from src.config.settings import settings

    assert not hasattr(settings.trading, "definitely_not_a_field")
    r = client.post("/api/config", json={"definitely_not_a_field": 1})
    assert r.get_json()["skipped"] == ["definitely_not_a_field"]


# ---------------------------------------------------------------------------
# Strategy control
# ---------------------------------------------------------------------------
def test_toggle_unknown_strategy_404(client):
    r = client.post("/api/strategy/nope/toggle", json={"mode": "paper"})
    assert r.status_code == 404


def test_toggle_rejects_live_without_credentials(client, monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY", raising=False)
    r = client.post("/api/strategy/ai_directional/toggle", json={"mode": "live"})
    assert r.status_code == 400


def test_toggle_rejects_invalid_mode(client):
    r = client.post("/api/strategy/ai_directional/toggle", json={"mode": "yolo"})
    assert r.status_code == 400


def test_toggle_refuses_without_kalshi_credentials(client, monkeypatch):
    """Paper mode still boots a KalshiClient and dies without credentials.

    The button must not report success for a process that is about to exit.
    """
    monkeypatch.delenv("KALSHI_API_KEY", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    r = client.post("/api/strategy/safe_compounder/toggle", json={"mode": "paper"})
    assert r.status_code == 400
    assert "credentials" in r.get_json()["error"].lower()
    assert not any(s["pid"] for s in client.get("/api/bots").get_json())


def test_toggle_refuses_when_only_api_key_present(client, monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY", "kid")
    monkeypatch.delenv("KALSHI_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    r = client.post("/api/strategy/safe_compounder/toggle", json={"mode": "paper"})
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


def test_toggle_starts_process_when_credentials_present(client, monkeypatch, tmp_path):
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
        r = client.post("/api/strategy/safe_compounder/toggle", json={"mode": "paper"})
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

        r2 = client.post("/api/strategy/safe_compounder/toggle", json={"mode": "paper"})
        assert r2.get_json()["running"] is False
    finally:
        st["running"] = False
        st["pid"] = None
        wd._child_procs.pop(31337, None)


def test_kill_when_not_running_is_noop(client):
    r = client.post("/api/bot/ai_directional/kill")
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
