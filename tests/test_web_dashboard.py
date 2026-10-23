"""Tests for the Railway-served web dashboard.

These cover regressions that made the deployed dashboard non-functional:
DB-backed endpoints returning 500, the SSE feed silently dropping every
listener, and config saves corrupting typed settings.
"""

import asyncio
import json
import os
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
    # The fill/settlement ledger is process-level state like the positions list,
    # so a test that installs one would otherwise leak it into the next test and
    # make the realized-P&L tile appear populated at random.
    monkeypatch.setitem(wd.dashboard_state, "ledger", None)
    # The snapshot memo is process-level too, and it caches the rendered payload
    # rather than the inputs. Without this, a test that mutates dashboard_state and
    # then reads /api/snapshot gets the *previous* test's payload back, so state
    # changes appear to have no effect depending on test order.
    monkeypatch.setitem(wd._SNAPSHOT_CACHE, "payload", None)
    monkeypatch.setitem(wd._SNAPSHOT_CACHE, "at", 0.0)
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
                " timestamp, live, status, strategy, stop_loss_price,"
                " take_profit_price, mode)"
                " VALUES ('KXTESTMKT', 'YES', 0.42, 10, '2026-01-01T00:00:00', 1,"
                " 'open', 'ai_directional', 0.35, 0.58, 'dry')"
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
                    " quantity, pnl, entry_timestamp, exit_timestamp, rationale,"
                    " strategy, mode)"
                    " VALUES (?, 'yes', 0.40, 0.45, 5, ?, '2026-01-01T00:00:00',"
                    " '2026-01-01T01:00:00', 'test', ?, 'dry')",
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
    for needle in ("What this system does", "Ingest", "Decide", "Execute", "Track"):
        assert needle in html, f"missing {needle!r}"


def test_index_renders_config_values(client):
    html = client.get("/").get_data(as_text=True)
    assert "max_position_size_pct" in html
    assert "0.4" in html, "min_confidence_to_trade value not rendered"


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


def _go_live(client, auth, monkeypatch):
    """Put the persisted book into LIVE with funding mocked out."""
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
    client.post("/api/mode", json={"mode": "live", "confirm": True}, headers=auth)


def test_kalshi_account_normalises_both_shapes(client, auth, monkeypatch):
    _go_live(client, auth, monkeypatch)
    gen = _load_kalshi(client)
    next(gen)
    try:
        snap = client.get("/api/snapshot").get_json()
    finally:
        gen.close()
    k = snap["kalshi"]
    assert k["connected"] is True
    assert k["market_count"] == 1
    # The event fixture has zero exposure, so it is historical context, not a
    # held position - but its row must still be normalised and listed so the
    # event panel can show where the money went.
    assert k["event_count"] == 0
    assert k["event_rows"] == 1
    assert any(r["ticker"] == "KXMLB-26" for r in k["events"])
    # Decimal strings must become floats, not stay as text.
    assert k["exposure"] == 1.85
    assert k["traded"] == 9.4
    # With no fill/settlement history there is no account-wide total to report,
    # and the tile must say so rather than quote the positions-row sum.
    assert k["realized_known"] is False
    assert k["realized"] == 0.0
    assert k["fees"] == 0.0
    assert k["cost_basis"] == 0.0
    # The per-market figures are still correct and kept for the position tables.
    assert k["positions_realized"] == 0.30  # 0.33 - 0.026
    assert k["positions_fees"] == 0.06  # 0.005 + 0.0506


def test_zero_share_rows_are_not_counted_as_positions(client, auth, monkeypatch):
    """Kalshi keeps a row per ticker ever touched; a 0-share row is not a position.

    The production account answered with 31 rows - 15 markets at exactly zero
    shares and zero exposure, plus event rollups carrying historical cost behind
    no open contracts. Counting them reported "33 live positions" for an account
    holding three, which is the number that sent the operator looking for 31
    positions that did not exist.
    """
    _go_live(client, auth, monkeypatch)
    wd.dashboard_state["positions"] = [
        # Held.
        dict(KALSHI_MARKET_POSITION),
        dict(KALSHI_EVENT_POSITION),
        # Touched and closed: Kalshi keeps these forever.
        {
            "ticker": "KXSB-27-LV",
            "position_fp": "0.00",
            "market_exposure_dollars": "0.00",
            "total_traded_dollars": "120.00",
            "realized_pnl_dollars": "-40.00",
            "fees_paid_dollars": "2.00",
        },
        {
            "ticker": "KXMLB-26-NYY",
            "position_fp": "0.00",
            "market_exposure_dollars": "0.00",
            "total_traded_dollars": "60.00",
            "realized_pnl_dollars": "-10.00",
            "fees_paid_dollars": "1.00",
        },
        # An event rollup whose cost is all historical, with nothing open.
        {
            "event_ticker": "KXNBAGREAT-26",
            "total_cost_shares_fp": "100.00",
            "total_cost_dollars": "925.84",
            "event_exposure_dollars": "0.00",
            "realized_pnl_dollars": "0.00",
            "fees_paid_dollars": "0.00",
        },
    ]
    try:
        snap = client.get("/api/snapshot").get_json()
    finally:
        wd.dashboard_state["positions"] = []
    k = snap["kalshi"]

    assert k["market_count"] == 1
    # Neither event row holds anything (both have zero exposure), so they are
    # historical context, not open positions - the held count is the market row
    # only, and event rows are never added to it (they are rollups of the same
    # contracts, and counting both would report one position twice).
    assert k["event_count"] == 0
    assert k["held_count"] == 1
    # The raw row counts are still reported, so the difference is visible.
    assert k["market_rows"] == 3
    assert k["event_rows"] == 2
    assert k["ghost_count"] == 4
    # Exposure sums held MARKET rows only. The $925.84 event cost is not
    # exposure, and neither is the other event rollup.
    assert k["exposure"] == 1.85
    # Closed tickers must not be listed as open positions; the event rows are
    # still listed as history (cost > 0).
    assert [r["ticker"] for r in k["markets"]] == [KALSHI_MARKET_POSITION["ticker"]]
    listed_events = sorted(r["ticker"] for r in k["events"])
    assert listed_events == ["KXMLB-26", "KXNBAGREAT-26"]


def test_the_live_position_tile_counts_held_rows_not_raw_rows(client, auth, monkeypatch):
    _go_live(client, auth, monkeypatch)
    wd.dashboard_state["positions"] = [
        dict(KALSHI_MARKET_POSITION),
        {
            "ticker": "KXSB-27-LV",
            "position_fp": "0.00",
            "market_exposure_dollars": "0.00",
            "total_traded_dollars": "0.00",
            "realized_pnl_dollars": "0.00",
            "fees_paid_dollars": "0.00",
        },
    ]
    try:
        html = client.get("/").get_data(as_text=True)
    finally:
        wd.dashboard_state["positions"] = []
    # "1" held, with Kalshi's flat rows named as flat rather than silently
    # added in or dressed up as positions.
    assert "flat rows on Kalshi" in html
    assert "cannot be deleted" in html


def test_a_zero_share_row_is_not_spent_a_title_lookup(client, auth, monkeypatch):
    """Only rows the page actually renders are worth a title request."""
    _go_live(client, auth, monkeypatch)
    wd.dashboard_state["positions"] = [
        dict(KALSHI_MARKET_POSITION),
        {"ticker": "KXSB-27-LV", "position_fp": "0.00"},
        {"ticker": "KXMLB-26-NYY", "position_fp": "0.00"},
    ]
    try:
        tickers = wd._current_book_tickers()
    finally:
        wd.dashboard_state["positions"] = []
    got = {r.get("ticker") or r.get("event_ticker") for r in tickers}
    assert "KXSB-27-LV" not in got
    assert "KXMLB-26-NYY" not in got
    assert KALSHI_MARKET_POSITION["ticker"] in got


def test_live_positions_show_only_what_kalshi_still_holds(client, auth, monkeypatch):
    """Settled local rows must never render as open LIVE positions.

    The 29 BTC15M rows that showed as LIVE "open" were contracts Kalshi had
    settled hours earlier - the local book kept them open and the page printed
    them as real positions, which is a lie with money attached. The LIVE list
    is now reconciled against Kalshi: only markets Kalshi still holds appear,
    stale local rows are excluded and counted, and Kalshi-held markets the
    local book never saw are added.
    """
    import asyncio
    from datetime import datetime

    from src.utils.database import DatabaseManager, Position

    _go_live(client, auth, monkeypatch)
    db = DatabaseManager(db_path=str(wd.DB_PATH))
    asyncio.run(db.initialize())
    asyncio.run(
        db.add_position(
            Position(
                market_id="KXHELD-26",
                side="YES",
                entry_price=0.5,
                quantity=2,
                timestamp=datetime.now(),
                rationale="t",
                confidence=0.9,
                live=True,
                strategy="btc_updown",
                mode="live",
            )
        )
    )
    asyncio.run(
        db.add_position(
            Position(
                market_id="KXSETTLED-26",
                side="YES",
                entry_price=0.8,
                quantity=1,
                timestamp=datetime.now(),
                rationale="t",
                confidence=0.9,
                live=True,
                strategy="btc_updown",
                mode="live",
            )
        )
    )
    # Kalshi says only KXHELD-26 is still held.
    wd.dashboard_state["positions"] = [
        {
            "ticker": "KXHELD-26",
            "position_fp": "2.00",
            "market_exposure_dollars": "1.00",
            "total_traded_dollars": "2.00",
            "realized_pnl_dollars": "0.00",
            "fees_paid_dollars": "0.00",
        }
    ]
    try:
        snap = client.get("/api/snapshot").get_json()
    finally:
        wd.dashboard_state["positions"] = []

    ids = [p["market_id"] for p in snap["positions"]]
    assert ids == ["KXHELD-26"], "a settled market must not render as an open LIVE position"
    assert snap["stale_live_rows"] == 1


def test_titles_are_resolved_in_one_batched_request(monkeypatch):
    """One request per batch, not one per ticker.

    The loop of `get_market(ticker)` calls was 25 sequential HTTP requests every
    interval, which is what drove the markets endpoint into 429 - and the 429
    handler abandoned the entire batch, so titles never resolved at all.
    """
    calls = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def get_markets(self, tickers=None, limit=None, **k):
            calls.append(list(tickers or []))
            return {"markets": [{"ticker": t, "title": f"Title for {t}"} for t in tickers or []]}

        async def get_market(self, ticker):  # pragma: no cover - must not be called
            raise AssertionError("per-ticker lookup must not be used for a batch")

    monkeypatch.setattr(wd, "_title_client", FakeClient())
    monkeypatch.setenv("KALSHI_API_KEY", "x")
    monkeypatch.setattr(wd, "_title_last_attempt", 0.0)
    monkeypatch.setattr(wd, "_title_backoff_until", 0.0)
    wd.dashboard_state["market_titles"] = {}

    rows = [{"ticker": f"KXT{i}"} for i in range(40)]
    wd._refresh_market_titles(rows)

    assert len(calls) == 1, "a 40-ticker batch must not cost 40 requests"
    assert len(calls[0]) == 40
    assert wd.dashboard_state["market_titles"]["KXT7"] == "Title for KXT7"


def test_a_failed_title_lookup_is_retried_rather_than_cached_as_the_ticker(monkeypatch):
    """A transient failure must not permanently pin the raw ticker to the page.

    The old code wrote `cached[ticker] = ticker` on failure, and "already
    resolved?" is `ticker not in cached` - so one 404 locked the raw ticker in
    for the life of the process with no path back to the real title.
    """
    attempts = []

    class FlakyClient:
        def __init__(self, *a, **k):
            pass

        async def get_markets(self, tickers=None, limit=None, **k):
            attempts.append(list(tickers or []))
            if len(attempts) == 1:
                return {"markets": []}  # nothing resolved this pass
            return {"markets": [{"ticker": t, "title": "Recovered"} for t in tickers or []]}

    monkeypatch.setattr(wd, "_title_client", FlakyClient())
    monkeypatch.setenv("KALSHI_API_KEY", "x")
    monkeypatch.setattr(wd, "_title_last_attempt", 0.0)
    monkeypatch.setattr(wd, "_title_backoff_until", 0.0)
    wd.dashboard_state["market_titles"] = {}

    rows = [{"ticker": "KXMYSTERY-26"}]
    wd._refresh_market_titles(rows)
    # A readable fallback is shown rather than an empty cell...
    assert wd.dashboard_state["market_titles"]["KXMYSTERY-26"]
    # ...and it is not the ticker, so the next pass tries again.
    assert wd.dashboard_state["market_titles"]["KXMYSTERY-26"] != "KXMYSTERY-26"

    wd._title_last_attempt = 0.0
    wd._refresh_market_titles(rows)
    assert wd.dashboard_state["market_titles"]["KXMYSTERY-26"] == "Recovered"


def test_kalshi_rows_carry_real_values(client, auth, monkeypatch):
    _go_live(client, auth, monkeypatch)
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


def test_live_page_still_shows_the_real_account(client, auth, monkeypatch):
    """Hiding the panel in DRY must not hide it in LIVE."""
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
    gen = _load_kalshi(client)
    next(gen)
    try:
        html = client.get("/").get_data(as_text=True)
    finally:
        gen.close()
    assert "Kalshi account — real, and being traded" in html
    assert 'id="kalshiMarkets"' in html


def test_switching_to_live_is_not_blocked_by_the_balance(client, auth, monkeypatch):
    """A low balance must not trap the operator in DRY.

    The switch used to refuse whenever the real balance could not fund a $1
    order, so clicking LIVE threw the operator back to DRY and the only way out
    was to fund the account first. Affordability is enforced where it belongs -
    per order, in build_order_request, which refuses anything the funding source
    cannot cover - so the view switch no longer second-guesses it.
    """
    from src.utils.mode import TradingMode

    async def broke(_self, private_key_path=None):
        return {
            "connected": True,
            "balance": 0.0,
            "balance_cents": 0,
            "can_fund": False,
            "reason": "balance $0.00 cannot buy even one 1-cent contract",
        }

    monkeypatch.setattr(TradingMode, "funding", broke)
    r = client.post("/api/mode", json={"mode": "live", "confirm": True}, headers=auth)
    assert r.status_code == 200
    assert r.get_json()["mode"] == "live"


def test_switching_to_live_without_a_working_api_is_still_refused(client, auth, monkeypatch):
    """Unreachable Kalshi is a genuine blocker - LIVE could not function."""
    from src.utils.mode import TradingMode

    async def down(_self, private_key_path=None):
        return {
            "connected": False,
            "balance": 0.0,
            "balance_cents": 0,
            "can_fund": False,
            "reason": "Private key file not found",
        }

    monkeypatch.setattr(TradingMode, "funding", down)
    r = client.post("/api/mode", json={"mode": "live", "confirm": True}, headers=auth)
    assert r.status_code == 400


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


def test_kalshi_positions_stand_in_for_an_empty_db(client, auth, monkeypatch):
    """The Kalshi account can stand in for an empty DB - but only in LIVE.

    In DRY the fallback put the real account's positions on the DRY screen
    whenever the simulated book happened to be empty, which is precisely the
    identity leak the DRY/LIVE split exists to prevent.
    """
    gen = _load_kalshi(client)
    next(gen)
    try:
        # DRY: no fallback. The real account's positions stay off this page.
        snap = client.get("/api/snapshot").get_json()
        assert snap["open"]["positions"] == 0
        assert snap["positions"] == []
    finally:
        gen.close()
    # LIVE: the real account IS the book, so its positions are the book's own.
    # The mode switch gates on funding, so provide a funded account stub.
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
    gen2 = _load_kalshi(client)
    next(gen2)
    try:
        snap = client.get("/api/snapshot").get_json()
    finally:
        gen2.close()
    assert (
        len(snap["positions"]) == 1
    ), "only the market Kalshi still holds is an open LIVE position"
    assert all(p["strategy"] == "kalshi_api" for p in snap["positions"])
    assert snap["positions"][0]["market_id"] == "KXPRES-26-BIDEN"
    # Back to DRY for any test that follows.
    client.post("/api/mode", json={"mode": "dry", "confirm": True}, headers=auth)


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
    assert wd._run_async(wd._fetch_kalshi_data()) == (None, None, [], [])


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
                    " quantity, pnl, entry_timestamp, exit_timestamp, rationale,"
                    " strategy, mode)"
                    " VALUES (?, 'yes', 0.4, 0.5, 1, ?, '2026-01-01T00:00:00',"
                    " '2026-01-01T0%d:00:00', 'test', 'ai_directional', 'dry')" % i,
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


def test_logs_reads_a_named_log_file(client, tmp_path):
    """A crashing strategy's own log names the crash - it must be
    readable, not just the newest file."""
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "strategy_btc_updown.log").write_text("boom\nTraceback\n")
    body = client.get("/api/logs?file=strategy_btc_updown.log").get_json()
    assert "boom" in body["lines"]


def test_logs_reject_a_file_outside_the_log_dir(client, tmp_path):
    """The name is validated against the directory listing, so no
    path traversal and no file that does not exist."""
    assert client.get("/api/logs?file=../secrets.env").status_code == 404
    assert client.get("/api/logs?file=nope.log").status_code == 404


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


def test_stop_is_never_gated_on_the_book(client, auth, monkeypatch):
    """Stop is never gated on the book.

    The mode check governs *starting*: a strategy must not begin trading a book
    the operator is not in. It used to govern Stop as well, so a process started
    under an older book stamp could not be cleared from the page, and the new
    book could not be armed while it lived. Stopping now always works, whatever
    book the process carries.
    """
    monkeypatch.setenv("KALSHI_API_KEY", "kid")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nxyz\n")
    for st in wd.strategy_state.values():
        st.update({"running": False, "pid": None})

    store = wd._runtime_store()

    async def _seed():
        await store.record_start("btc_updown", 4242, "paper", "cli.py run")

    import asyncio

    asyncio.run(_seed())
    monkeypatch.setattr(wd, "_pid_alive", lambda pid, row=None: True)
    monkeypatch.setattr(wd, "_stop_child", lambda _d: 0)

    r = client.post("/api/strategy/btc_updown/toggle", json={"mode": "paper"}, headers=auth)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["running"] is False


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
    r = client.post("/api/strategy/ai_directional/toggle", json={"mode": "paper"}, headers=auth)
    assert r.status_code == 400
    assert "credentials" in r.get_json()["error"].lower()
    assert not any(s["pid"] for s in client.get("/api/bots").get_json())


def test_toggle_client_stops_polling_after_server_rejects_start():
    source = wd._TEMPLATE
    start = source.index("async function toggleStrategy")
    end = source.index("async function saveAlerts", start)
    toggle = source[start:end]

    assert "if (!r.ok || d.error)" in toggle
    assert toggle.index("if (!r.ok || d.error)") < toggle.index("let maxWait")


def test_toggle_refuses_when_only_api_key_present(client, auth, monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY", "kid")
    monkeypatch.delenv("KALSHI_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    r = client.post("/api/strategy/ai_directional/toggle", json={"mode": "paper"}, headers=auth)
    assert r.status_code == 400


def test_stopping_a_strategy_disarms_only_its_own_row(client, auth, monkeypatch, tmp_path):
    """A Stop disarms exactly the row it was asked about.

    There is one book now, so a strategy has a single runtime row. The Stop must
    clear that row and record why, and it must not reach into any other row:
    the up/down family shares one trader process, so killing the wrong row is
    how a Stop ended up leaving the process running.
    """
    import asyncio
    import subprocess as sp

    from src.utils.strategy_runtime import key as rt_key

    proc = sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        store = wd._runtime_store()
        asyncio.run(store.record_start("btc_updown", proc.pid, "paper", "cli.py run --btc-updown"))
        r = client.post("/api/strategy/btc_updown/toggle", json={}, headers=auth)
        assert r.status_code == 200
        body = r.get_json()
        assert body["running"] is False

        snap = asyncio.run(store.snapshot())
        assert snap[rt_key("btc_updown", "paper")]["pid"] is None
        assert snap[rt_key("btc_updown", "paper")]["stop_reason"] == "stopped by operator"
    finally:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass


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
    st = wd.strategy_state["ai_directional"]
    try:
        r = client.post("/api/strategy/ai_directional/toggle", json={"mode": "paper"}, headers=auth)
        assert r.status_code == 200
        body = r.get_json()
        assert body["running"] is True
        assert body["pid"] == 31337
        # Must use this interpreter, not whatever `python` resolves to on PATH.
        assert spawned["cmd"][0] == sys.executable
        assert "--directional" in spawned["cmd"]
        assert "--paper" in spawned["cmd"]
        # Output must not go to an undrained PIPE (that deadlocks the child
        # once the ~64KB buffer fills); stderr folds into the same log file.
        assert spawned["stderr"] == wd.subprocess.STDOUT
        assert spawned["stdin"] == wd.subprocess.DEVNULL
        assert spawned["stdout"] is not wd.subprocess.PIPE

        bots = {b["name"]: b for b in client.get("/api/bots").get_json()}
        # safe_compounder is locked OFF as a heavy API abuser and must not run.
        assert bots["safe_compounder"]["running"] is False

        r2 = client.post(
            "/api/strategy/safe_compounder/toggle", json={"mode": "paper"}, headers=auth
        )
        assert r2.status_code == 403
        assert "error" in r2.get_json()
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
    ("/api/mode", {"mode": "live", "confirm": True}),
    ("/api/strategies/reset-state", {}),
    ("/api/maintenance/lean", {}),
    ("/api/maintenance/backfill-fair", {}),
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


def _post_config_unchanged(client, headers=None, fields=("max_positions",)):
    """POST a config field with the value it already holds.

    /api/config is token-gated and needs no Kalshi credentials, so it proves
    the token is accepted without a live network round trip - and writing back
    the current value keeps a token proof from mutating global settings, which
    would bleed into every later test.
    """
    from src.config.settings import settings as _S

    body = {f: getattr(_S.trading, f) for f in fields}
    return client.post("/api/config", json=body, headers=headers)


def test_token_accepted_from_header(client, auth):
    r = _post_config_unchanged(client, headers=auth)
    assert r.status_code == 200


def test_token_accepted_from_json_body(client):
    r = _post_config_unchanged(client, headers={"X-Auth-Token": TEST_TOKEN})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# DRY / LIVE mode


def test_mode_rejects_unknown_value(client, auth):
    r = client.post("/api/mode", json={"mode": "yolo"}, headers=auth)
    assert r.status_code == 400


def test_going_live_requires_explicit_confirmation(client, auth):
    r = client.post("/api/mode", json={"mode": "live"}, headers=auth)
    assert r.status_code == 400
    assert "real money" in r.get_json()["error"]
    assert client.get("/api/mode").get_json()["mode"] == "dry"


def test_going_live_with_no_balance_is_reported_not_refused(client, auth, monkeypatch):
    """An unfunded account must not trap the operator in DRY.

    This used to refuse the switch outright, so clicking LIVE threw the operator
    back to DRY and the only way in was to fund the account first. Affordability
    belongs to the order, not the view: build_order_request refuses anything the
    funding source cannot cover, so an unfunded LIVE book cannot send an order it
    cannot pay for. The condition is surfaced as a warning instead.
    """
    from src.utils.mode import TradingMode

    async def unfunded(_self, private_key_path=None):
        return {
            "connected": True,
            "balance": 0.0,
            "balance_cents": 0,
            "can_fund": False,
            "reason": "balance $0.00 cannot buy even one 1-cent contract",
        }

    monkeypatch.setattr(TradingMode, "funding", unfunded)
    r = client.post("/api/mode", json={"mode": "live", "confirm": True}, headers=auth)
    assert r.status_code == 200
    assert r.get_json()["mode"] == "live"
    assert client.get("/api/mode").get_json()["mode"] == "live"


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


# ---------------------------------------------------------------------------
# DRY cash ledger


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
    # A fresh database is DRY: an unconfigured deployment must never come up
    # rendering itself as a live-money page.
    assert '<body data-mode="dry">' in html


def test_page_has_full_viewport_mode_frame(client):
    html = client.get("/").get_data(as_text=True)
    assert "position:fixed;inset:0;z-index:9999" in html
    # LIVE shares DRY's green palette and instead rings every card in red.
    assert "border:1px solid var(--down)" in html


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
    # LIVE uses the green favicon marked L.
    assert "%232ee6a8" in html


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
    assert "DRY Trading Dashboard" not in html
    assert "REAL ORDERS, REAL MONEY" in html or "real orders" in html.lower()


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
async def test_tradelog_preserves_roll_marker_and_attribution(tmp_path):
    """A closed ROLL-IQ position retains its marker for durable roll stats."""
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
            rationale="BTC_UPDOWN ROLL-IQ successor",
            strategy="btc_updown",
            exit_reason="take_profit",
            mode="dry",
        )
    )
    got = (await db.get_all_trade_logs())[0]
    assert got.strategy == "btc_updown"
    assert got.exit_reason == "take_profit"
    assert got.mode == "dry"
    assert "ROLL-IQ" in got.rationale


@pytest.mark.asyncio
async def test_migration_column_added_to_old_database(tmp_path):
    """An existing database gains the new columns, it is not recreated."""
    import sqlite3

    from src.utils.database import DatabaseManager

    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    # The pre-mode schema, 14 columns and no mode.
    conn.execute("""CREATE TABLE positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT NOT NULL, side TEXT NOT NULL,
        entry_price REAL NOT NULL, quantity INTEGER NOT NULL, timestamp TEXT NOT NULL,
        rationale TEXT, confidence REAL, live BOOLEAN NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'open', strategy TEXT, stop_loss_price REAL,
        take_profit_price REAL)""")
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
    r = _post_config_unchanged(client, headers={"Authorization": f"Basic {creds}"})
    assert r.status_code == 200


def test_write_rejects_wrong_basic_auth(client, monkeypatch):
    import base64

    monkeypatch.setenv("DASHBOARD_USER", "operator")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    creds = base64.b64encode(b"operator:wrong").decode()
    r = _post_config_unchanged(client, headers={"Authorization": f"Basic {creds}"})
    assert r.status_code == 401


def test_basic_auth_ignored_when_unconfigured(client):
    import base64

    monkeypatch_user = None
    creds = base64.b64encode(b"anyone:anything").decode()
    r = client.post(
        "/api/mode",
        json={"mode": "live", "confirm": True},
        headers={"Authorization": f"Basic {creds}"},
    )
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


# ---------------------------------------------------------------------------
# Strategy cards
#
# Every strategy must have a card, its own persistent curve, and a detail view.
# The alias map matters because strategies write their own names into
# trade_logs.strategy (quick_flip_scalping, directional_trading), which are not
# the names of the buttons - without it every card reads "unattributed".
# ---------------------------------------------------------------------------
def _seed_cards(client):
    """Closes and open positions spread across several strategy names."""
    _db()

    async def seed():
        async with aiosqlite.connect(wd.DB_PATH) as conn:
            rows = [
                # (strategy, pnl, mode)
                ("directional_trading", 1.00, "dry"),
                ("directional_trading", 2.00, "dry"),
                ("directional_trading", -0.50, "dry"),
                ("quick_flip_scalping", 0.75, "dry"),
                ("safe_compounder", -1.25, "dry"),
                ("market_making", 0.25, "dry"),
                # A row from the removed simulated book must not leak into a
                # real book's card.
                ("ai_directional", 99.00, "live"),
            ]
            for i, (strategy, pnl, mode) in enumerate(rows):
                await conn.execute(
                    "INSERT INTO trade_logs (market_id, side, entry_price, exit_price,"
                    " quantity, pnl, entry_timestamp, exit_timestamp, rationale,"
                    " strategy, mode) VALUES (?, 'yes', 0.40, 0.45, 5, ?,"
                    " '2026-01-01T00:00:00', ?, 'card test', ?, ?)",
                    (f"KXCARD{i}", pnl, f"2026-01-0{i + 1}T0{i}:00:00", strategy, mode),
                )
            await conn.execute(
                "INSERT INTO positions (market_id, side, entry_price, quantity,"
                " timestamp, live, status, strategy, mode)"
                " VALUES ('KXOPEN', 'YES', 0.30, 12, '2026-01-01T00:00:00', 0,"
                " 'open', 'market_making', 'dry')"
            )
            await conn.execute(
                "INSERT INTO positions (market_id, side, entry_price, quantity,"
                " timestamp, live, status, strategy, mode)"
                " VALUES ('KXLEGACYOPEN', 'YES', 0.30, 12, '2026-01-01T00:00:00', 1,"
                " 'open', 'ai_directional', 'live')"
            )
            await conn.commit()

    asyncio.run(seed())


def _cards(client):
    return {c["name"]: c for c in client.get("/api/snapshot").get_json()["strategy_cards"]}


def test_every_strategy_has_a_card(client):
    _seed_cards(client)
    cards = _cards(client)
    assert set(cards) == set(wd.strategy_state), "a strategy has no card"


def test_every_strategy_runs_its_own_command(client):
    """No two buttons may run the same process - that was how two fake
    'strategies' ended up being one bot.

    The UP/DOWN lanes are the deliberate exception: btc_updown,
    doge_updown and btc_1h_updown are separate operator-pushed
    lanes of the same --btc-updown trader.
    """
    _seed_cards(client)
    cards = _cards(client)
    assert set(cards) == set(wd.STRATEGY_COMMANDS), "a strategy has no command"
    commands = {c["name"]: c["command"] for c in cards.values()}
    shared = {name: cmd for name, cmd in commands.items() if list(commands.values()).count(cmd) > 1}
    # Only the UP/DOWN family may share, and only on the updown trader.
    assert set(shared) <= {"btc_updown", "doge_updown", "btc_1h_updown"}
    for cmd in shared.values():
        assert "--btc-updown" in cmd


def test_cards_alias_strategy_names(client):
    _seed_cards(client)
    cards = _cards(client)
    # "directional_trading" is what decide.py writes; the button is ai_directional.
    assert cards["ai_directional"]["trades"] == 3
    assert cards["ai_directional"]["realized"] == 2.5
    assert cards["quick_flip"]["trades"] == 1
    assert cards["quick_flip"]["realized"] == 0.75
    assert cards["safe_compounder"]["trades"] == 1
    assert cards["beast_mode"]["trades"] == 0


def test_card_curve_is_cumulative_and_persistent(client):
    _seed_cards(client)
    curve = _cards(client)["ai_directional"]["equity"]
    # Three closes: +1.00, +2.00, -0.50 -> 1.00, 3.00, 2.50
    assert curve["pnl"] == [1.0, 3.0, 2.5]
    assert len(curve["labels"]) == 3


def test_card_shows_only_the_current_book(client):
    _seed_cards(client)
    snap = client.get("/api/snapshot").get_json()
    assert snap["book"] == "dry"
    # The +99.00 LIVE close and the LIVE open position must not appear.
    assert _cards(client)["ai_directional"]["realized"] == 2.5
    assert _cards(client)["ai_directional"]["open_positions"] == 0


def test_card_counts_open_positions_per_strategy(client):
    _seed_cards(client)
    card = _cards(client)["market_making"]
    assert card["open_positions"] == 1
    assert card["deployed"] == 3.6  # 12 contracts at $0.30


def test_dedicated_roll_card_and_painter_are_removed(client):
    snap = client.get("/api/snapshot").get_json()
    html = client.get("/").get_data(as_text=True)

    assert snap["roll_card"]["closed_clips"] == 0
    assert snap["roll_card"]["open_clips"] == 0
    assert 'id="roll-card"' not in html
    assert "paintRollCard" not in html
    assert "onclick=\"openStrategy('roll_iq')\"" not in html
    assert 'id="btn-roll_iq"' not in html


def test_roll_card_aggregates_only_current_book_real_roll_trades(client):
    import asyncio

    _db()

    async def seed():
        async with aiosqlite.connect(wd.DB_PATH) as conn:
            for market_id, pnl, mode, exit_reason in (
                ("KXROLLDRY", 0.75, "dry", "market_resolution"),
                ("KXROLLPHANTOM", 0.0, "dry", "no_kalshi_position"),
                ("KXROLLLIVE", 9.0, "live", "market_resolution"),
            ):
                await conn.execute(
                    "INSERT INTO trade_logs (market_id, side, entry_price, exit_price,"
                    " quantity, pnl, entry_timestamp, exit_timestamp, rationale,"
                    " strategy, mode, exit_reason) VALUES (?, 'yes', 0.40, 0.55, 5, ?,"
                    " '2026-01-01T00:00:00', '2026-01-02T00:00:00', 'ROLL-IQ successor',"
                    " 'btc_updown', ?, ?)",
                    (market_id, pnl, mode, exit_reason),
                )
            for market_id, mode in (("KXROLLOPEN", "dry"), ("KXROLLLIVEOPEN", "live")):
                await conn.execute(
                    "INSERT INTO positions (market_id, side, entry_price, quantity,"
                    " timestamp, live, status, strategy, mode, rationale)"
                    " VALUES (?, 'YES', 0.20, 3, '2026-01-01T00:00:00', ?, 'open',"
                    " 'btc_updown', ?, 'ROLL-IQ successor')",
                    (market_id, int(mode == "live"), mode),
                )
            await conn.commit()

    asyncio.run(seed())
    snap = client.get("/api/snapshot").get_json()
    roll = snap["roll_card"]
    btc = {card["name"]: card for card in snap["strategy_cards"]}["btc_updown"]

    assert snap["book"] == "dry"
    assert roll["closed_clips"] == 1
    assert roll["open_clips"] == 1
    assert roll["total_clips"] == 2
    assert roll["wins"] == 1
    assert roll["losses"] == 0
    assert roll["win_rate"] == 100.0
    assert roll["realized_pnl"] == 0.75
    assert roll["open_deployed"] == 0.6
    assert btc["roll_clips"] == 1
    assert btc["roll_open"] == 1
    assert btc["roll_rate"] == 100.0
    strategy = {row["strategy"]: row for row in snap["by_strategy"]}["btc_updown"]
    assert strategy["roll_clips"] == 1
    assert strategy["roll_rate"] == 100.0


def test_card_reports_its_own_command(client):
    _seed_cards(client)
    cards = _cards(client)
    # Each strategy must run a distinct process, not fall back to one
    # loop. The UP/DOWN lanes are separate operator-pushed lanes of the
    # same --btc-updown trader, so they share that one command.
    commands = {c["name"]: c["command"] for c in cards.values()}
    shared = {name: cmd for name, cmd in commands.items() if list(commands.values()).count(cmd) > 1}
    assert set(shared) <= {"btc_updown", "doge_updown", "btc_1h_updown"}
    assert "--market-making" in commands["market_making"]
    assert "--quick-flip" in commands["quick_flip"]
    # A strategy that runs once and exits must not report itself as running.
    for cmd in commands.values():
        assert "--loop" in cmd or "--beast" in cmd


def test_index_renders_one_card_per_strategy(client):
    _seed_cards(client)
    html = client.get("/").get_data(as_text=True)
    for name in wd.strategy_state:
        assert f'id="card-{name}"' in html
        assert f'id="spark-{name}"' in html
    assert "Start all in DRY" in html


def test_strategy_detail_returns_that_strategy_only(client):
    _seed_cards(client)
    d = client.get("/api/strategy/ai_directional").get_json()
    assert d["book"] == "dry"
    assert d["strategy"]["trades"] == 3
    assert len(d["trades"]) == 3
    assert {t["market_id"] for t in d["trades"]} == {"KXCARD0", "KXCARD1", "KXCARD2"}
    assert d["strategy"]["equity"]["pnl"] == [1.0, 3.0, 2.5]


def test_strategy_detail_excludes_other_books_and_strategies(client):
    _seed_cards(client)
    d = client.get("/api/strategy/quick_flip").get_json()
    assert d["strategy"]["trades"] == 1
    assert all(t["market_id"] != "KXCARD0" for t in d["trades"])
    assert d["positions"] == []


def test_strategy_detail_404s_for_unknown(client):
    _seed_cards(client)
    assert client.get("/api/strategy/not_a_strategy").status_code == 404


def test_strategy_detail_lists_open_positions_and_logs(client):
    _seed_cards(client)
    wd.LOG_DIR.mkdir(parents=True, exist_ok=True)
    (wd.LOG_DIR / "strategy_market_making.log").write_text("cycle 1\ncycle 2\n", encoding="utf-8")
    d = client.get("/api/strategy/market_making").get_json()
    assert [p["market_id"] for p in d["positions"]] == ["KXOPEN"]
    assert d["logs"] == ["cycle 1", "cycle 2"]


def test_cards_exist_on_an_empty_database(client):
    """No closes yet must still yield a card per strategy with an explicit empty state."""
    cards = _cards(client)
    assert len(cards) == len(wd.strategy_state)
    for card in cards.values():
        assert card["trades"] == 0
        assert (
            card["equity"]["pnl"] == []
        )  # ---------------------------------------------------------------------------


# Truthfulness: DRY must never report the real account, and a strategy's state
# pill must reflect the OS rather than the flag the request that started it
# happened to set.
# ---------------------------------------------------------------------------
def _seed_two_books():
    """One DRY position/trade and one LIVE position/trade for the same strategy."""
    _db()

    async def seed():
        async with aiosqlite.connect(wd.DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO positions (market_id, side, entry_price, quantity,"
                " timestamp, live, status, strategy, mode)"
                " VALUES ('KXDRYPOS', 'YES', 0.40, 10, '2026-01-01T00:00:00', 0,"
                " 'open', 'ai_directional', 'dry')"
            )
            await conn.execute(
                "INSERT INTO positions (market_id, side, entry_price, quantity,"
                " timestamp, live, status, strategy, mode)"
                " VALUES ('KXLIVEPOS', 'YES', 0.60, 20, '2026-01-01T00:00:00', 1,"
                " 'open', 'ai_directional', 'live')"
            )
            for mid, pnl, mode in (("KXDRYTRD", 1.0, "dry"), ("KXLIVETRD", 50.0, "live")):
                await conn.execute(
                    "INSERT INTO trade_logs (market_id, side, entry_price, exit_price,"
                    " quantity, pnl, entry_timestamp, exit_timestamp, rationale,"
                    " strategy, mode) VALUES (?, 'yes', 0.40, 0.45, 5, ?,"
                    " '2026-01-01T00:00:00', '2026-01-02T00:00:00', 'book',"
                    " 'ai_directional', ?)",
                    (mid, pnl, mode),
                )
            await conn.commit()

    asyncio.run(seed())


def test_live_book_excludes_dry_rows(client):
    from src.utils.mode import TradingMode

    _seed_two_books()
    live = asyncio.run(TradingMode(db_path=wd.DB_PATH).live_account())
    assert live["deployed"] == 12.0  # 20 contracts at $0.60
    assert live["open_positions"] == 1
    assert live["realized"] == 50.0
    assert live["closed_trades"] == 1


def test_strategy_state_reads_the_database_not_the_request_flag(client, monkeypatch):
    """A pid recorded by another request - or another worker - must be honoured."""
    from src.utils.strategy_runtime import key as rt_key

    _db()
    from src.utils.strategy_runtime import StrategyRuntime

    store = StrategyRuntime(db_path=wd.DB_PATH)
    # A pid that is certainly alive: this very test process.
    asyncio.run(store.record_start("quick_flip", os.getpid(), "paper", "cli.py run --quick-flip"))

    # Wipe the in-memory flag, as a fresh worker or a redeploy would.
    wd.strategy_state["quick_flip"].update({"running": False, "pid": None})

    recorded = wd._recorded_state()
    assert recorded[rt_key("quick_flip", "paper")]["pid"] == os.getpid()
    assert wd.strategy_state["quick_flip"]["running"] is True
    assert "quick_flip" in wd._running_strategies()
    snap = client.get("/api/snapshot").get_json()
    cards = {c["name"]: c for c in snap["strategy_cards"]}
    assert cards["quick_flip"]["running"] is True
    assert cards["quick_flip"]["pid"] == os.getpid()


def test_strategy_state_records_why_a_dead_strategy_stopped(client):
    """A dead pid must read 'stopped' with a reason, not sit silently at zero."""
    _db()
    from src.utils.strategy_runtime import StrategyRuntime

    store = StrategyRuntime(db_path=wd.DB_PATH)
    # A pid that cannot exist, so the liveness probe must fail.
    asyncio.run(store.record_start("market_making", 999_999_999, "paper", "cli.py run"))
    wd.strategy_state["market_making"].update({"running": True, "pid": 999_999_999})

    wd._recorded_state()
    st = wd.strategy_state["market_making"]
    assert st["running"] is False
    assert st["pid"] is None
    assert "exited" in (st["stop_reason"] or "")

    # The reason must reach the page so the pill is never ambiguous.
    cards = {c["name"]: c for c in client.get("/api/snapshot").get_json()["strategy_cards"]}
    assert "exited" in cards["market_making"]["stop_reason"]


def test_strategy_state_is_empty_before_anything_runs(client):
    """Never started must be distinguishable from started-and-stopped."""
    _db()
    wd._recorded_state()
    cards = {c["name"]: c for c in client.get("/api/snapshot").get_json()["strategy_cards"]}
    for card in cards.values():
        assert card["running"] is False
        assert card["stop_reason"] == ""


def test_pid_alive_never_signals_the_process():
    """The Windows-safe probe must not terminate what it is asked about."""
    from src.utils.strategy_runtime import pid_alive

    assert pid_alive(os.getpid()) is True
    assert pid_alive(None) is False
    assert pid_alive(0) is False
    assert pid_alive(999_999_999) is False
    # Still alive after being probed - the regression that matters.
    assert pid_alive(os.getpid()) is True


# ---------------------------------------------------------------------------
# Regression: every spawned strategy died instantly on
# "Private key file not found: kalshi_private_key.pem". Railway injects
# KALSHI_PRIVATE_KEY as PEM text; KalshiClient loads a *path*, and only the
# dashboard was bridging the two. A child inherited the text and died, which is
# why every Start button read "stopped" no matter what it did.
# ---------------------------------------------------------------------------
class _RecordingPopen:
    """Captures the kwargs the dashboard spawns a strategy with."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 424242
        _RecordingPopen.instances.append(self)

    def poll(self):
        return None  # alive


def _toggle_env(monkeypatch, tmp_path, pem="-----BEGIN PRIVATE KEY-----\nxyz\n"):
    monkeypatch.setattr(wd.subprocess, "Popen", _RecordingPopen)
    monkeypatch.setattr(wd, "_child_procs", {})
    monkeypatch.setenv("KALSHI_API_KEY", "kid")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", pem)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "child.db"))
    # strategy_state is module-level, so a start from a previous test would make
    # the next one take the stop path and never spawn anything.
    for st in wd.strategy_state.values():
        st.update({"running": False, "pid": None, "stop_reason": ""})
    _RecordingPopen.instances = []
    return pem


def test_child_process_receives_a_readable_private_key_path(client, auth, monkeypatch, tmp_path):
    _toggle_env(monkeypatch, tmp_path)
    r = client.post("/api/strategy/ai_directional/toggle", json={"mode": "paper"}, headers=auth)
    assert r.status_code == 200
    env = _RecordingPopen.instances[-1].kwargs["env"]
    key_path = env.get("KALSHI_PRIVATE_KEY_PATH")
    assert key_path, "child got no KALSHI_PRIVATE_KEY_PATH"
    # The path must actually resolve to the PEM, or the child dies the same way.
    assert "BEGIN" in open(key_path, encoding="utf-8").read()


def test_child_process_shares_the_deployed_database(client, auth, monkeypatch, tmp_path):
    """Otherwise every strategy writes to a throwaway container database."""
    _toggle_env(monkeypatch, tmp_path)
    client.post("/api/strategy/ai_directional/toggle", json={"mode": "paper"}, headers=auth)
    env = _RecordingPopen.instances[-1].kwargs["env"]
    assert env["DB_PATH"] == str(wd.DB_PATH)


def test_every_strategy_command_is_paper(client, auth, monkeypatch, tmp_path):
    """A spawned strategy must never carry --live."""
    _toggle_env(monkeypatch, tmp_path)
    for name in wd.strategy_state:
        _RecordingPopen.instances = []
        r = client.post(f"/api/strategy/{name}/toggle", json={"mode": "paper"}, headers=auth)
        if name in wd.HEAVY_API_ABUSERS:
            # quick_flip is locked OFF as the #1 OpenRouter spender: the API
            # refuses Start with 403 and spawns nothing.
            assert r.status_code == 403, f"{name} abuser lockout bypassed"
            assert _RecordingPopen.instances == []
            continue
        if name in wd.RETIRED_LANES:
            # Source removed: Start is refused and nothing is spawned, so the
            # supervisor can never crash-loop a process that cannot exist.
            assert r.status_code == 409, f"{name} retired lane was not refused"
            assert _RecordingPopen.instances == []
            continue
        args = list(_RecordingPopen.instances[-1].args[0])
        assert "--live" not in args, f"{name} was spawned with --live"
        assert "--paper" in args, f"{name} was spawned without --paper"


def test_every_strategy_runs_its_own_command(client):
    """No two buttons may run the same process - that was how two fake
    'strategies' ended up being one bot.

    The UP/DOWN lanes are the deliberate exception: btc_updown,
    doge_updown and btc_1h_updown are separate operator-pushed
    lanes of the same --btc-updown trader.
    """
    _seed_cards(client)
    cards = _cards(client)
    assert set(cards) == set(wd.STRATEGY_COMMANDS), "a strategy has no command"
    commands = {c["name"]: c["command"] for c in cards.values()}
    shared = {name: cmd for name, cmd in commands.items() if list(commands.values()).count(cmd) > 1}
    # Only the UP/DOWN family may share, and only on the updown trader.
    assert set(shared) <= {"btc_updown", "doge_updown", "btc_1h_updown"}
    for cmd in shared.values():
        assert "--btc-updown" in cmd


def test_cards_alias_strategy_names(client):
    _seed_cards(client)
    cards = _cards(client)
    # "directional_trading" is what decide.py writes; the button is ai_directional.
    assert cards["ai_directional"]["trades"] == 3
    assert cards["ai_directional"]["realized"] == 2.5
    assert cards["quick_flip"]["trades"] == 1
    assert cards["quick_flip"]["realized"] == 0.75
    assert cards["safe_compounder"]["trades"] == 1
    assert cards["beast_mode"]["trades"] == 0


def test_card_curve_is_cumulative_and_persistent(client):
    _seed_cards(client)
    curve = _cards(client)["ai_directional"]["equity"]
    # Three closes: +1.00, +2.00, -0.50 -> 1.00, 3.00, 2.50
    assert curve["pnl"] == [1.0, 3.0, 2.5]
    assert len(curve["labels"]) == 3


def test_card_shows_only_the_current_book(client):
    _seed_cards(client)
    snap = client.get("/api/snapshot").get_json()
    assert snap["book"] == "dry"
    # The +99.00 LIVE close and the LIVE open position must not appear.
    assert _cards(client)["ai_directional"]["realized"] == 2.5
    assert _cards(client)["ai_directional"]["open_positions"] == 0


def test_card_counts_open_positions_per_strategy(client):
    _seed_cards(client)
    card = _cards(client)["market_making"]
    assert card["open_positions"] == 1
    assert card["deployed"] == 3.6  # 12 contracts at $0.30


def test_dedicated_roll_card_and_painter_are_removed(client):
    snap = client.get("/api/snapshot").get_json()
    html = client.get("/").get_data(as_text=True)

    assert snap["roll_card"]["closed_clips"] == 0
    assert snap["roll_card"]["open_clips"] == 0
    assert 'id="roll-card"' not in html
    assert "paintRollCard" not in html
    assert "onclick=\"openStrategy('roll_iq')\"" not in html
    assert 'id="btn-roll_iq"' not in html


def test_roll_card_aggregates_only_current_book_real_roll_trades(client):
    import asyncio

    _db()

    async def seed():
        async with aiosqlite.connect(wd.DB_PATH) as conn:
            for market_id, pnl, mode, exit_reason in (
                ("KXROLLDRY", 0.75, "dry", "market_resolution"),
                ("KXROLLPHANTOM", 0.0, "dry", "no_kalshi_position"),
                ("KXROLLLIVE", 9.0, "live", "market_resolution"),
            ):
                await conn.execute(
                    "INSERT INTO trade_logs (market_id, side, entry_price, exit_price,"
                    " quantity, pnl, entry_timestamp, exit_timestamp, rationale,"
                    " strategy, mode, exit_reason) VALUES (?, 'yes', 0.40, 0.55, 5, ?,"
                    " '2026-01-01T00:00:00', '2026-01-02T00:00:00', 'ROLL-IQ successor',"
                    " 'btc_updown', ?, ?)",
                    (market_id, pnl, mode, exit_reason),
                )
            for market_id, mode in (("KXROLLOPEN", "dry"), ("KXROLLLIVEOPEN", "live")):
                await conn.execute(
                    "INSERT INTO positions (market_id, side, entry_price, quantity,"
                    " timestamp, live, status, strategy, mode, rationale)"
                    " VALUES (?, 'YES', 0.20, 3, '2026-01-01T00:00:00', ?, 'open',"
                    " 'btc_updown', ?, 'ROLL-IQ successor')",
                    (market_id, int(mode == "live"), mode),
                )
            await conn.commit()

    asyncio.run(seed())
    snap = client.get("/api/snapshot").get_json()
    roll = snap["roll_card"]
    btc = {card["name"]: card for card in snap["strategy_cards"]}["btc_updown"]

    assert snap["book"] == "dry"
    assert roll["closed_clips"] == 1
    assert roll["open_clips"] == 1
    assert roll["total_clips"] == 2
    assert roll["wins"] == 1
    assert roll["losses"] == 0
    assert roll["win_rate"] == 100.0
    assert roll["realized_pnl"] == 0.75
    assert roll["open_deployed"] == 0.6
    assert btc["roll_clips"] == 1
    assert btc["roll_open"] == 1
    assert btc["roll_rate"] == 100.0
    strategy = {row["strategy"]: row for row in snap["by_strategy"]}["btc_updown"]
    assert strategy["roll_clips"] == 1
    assert strategy["roll_rate"] == 100.0


def test_card_reports_its_own_command(client):
    _seed_cards(client)
    cards = _cards(client)
    # Each strategy must run a distinct process, not fall back to one
    # loop. The UP/DOWN lanes are separate operator-pushed lanes of the
    # same --btc-updown trader, so they share that one command.
    commands = {c["name"]: c["command"] for c in cards.values()}
    shared = {name: cmd for name, cmd in commands.items() if list(commands.values()).count(cmd) > 1}
    assert set(shared) <= {"btc_updown", "doge_updown", "btc_1h_updown"}
    assert "--market-making" in commands["market_making"]
    assert "--quick-flip" in commands["quick_flip"]
    # A strategy that runs once and exits must not report itself as running.
    for cmd in commands.values():
        assert "--loop" in cmd or "--beast" in cmd


def test_index_renders_one_card_per_strategy(client):
    _seed_cards(client)
    html = client.get("/").get_data(as_text=True)
    for name in wd.strategy_state:
        assert f'id="card-{name}"' in html
        assert f'id="spark-{name}"' in html
    assert "Start all in DRY" in html


def test_strategy_detail_returns_that_strategy_only(client):
    _seed_cards(client)
    d = client.get("/api/strategy/ai_directional").get_json()
    assert d["book"] == "dry"
    assert d["strategy"]["trades"] == 3
    assert len(d["trades"]) == 3
    assert {t["market_id"] for t in d["trades"]} == {"KXCARD0", "KXCARD1", "KXCARD2"}
    assert d["strategy"]["equity"]["pnl"] == [1.0, 3.0, 2.5]


def test_strategy_detail_excludes_other_books_and_strategies(client):
    _seed_cards(client)
    d = client.get("/api/strategy/quick_flip").get_json()
    assert d["strategy"]["trades"] == 1
    assert all(t["market_id"] != "KXCARD0" for t in d["trades"])
    assert d["positions"] == []


def test_strategy_detail_404s_for_unknown(client):
    _seed_cards(client)
    assert client.get("/api/strategy/not_a_strategy").status_code == 404


def test_strategy_detail_lists_open_positions_and_logs(client):
    _seed_cards(client)
    wd.LOG_DIR.mkdir(parents=True, exist_ok=True)
    (wd.LOG_DIR / "strategy_market_making.log").write_text("cycle 1\ncycle 2\n", encoding="utf-8")
    d = client.get("/api/strategy/market_making").get_json()
    assert [p["market_id"] for p in d["positions"]] == ["KXOPEN"]
    assert d["logs"] == ["cycle 1", "cycle 2"]


def test_cards_exist_on_an_empty_database(client):
    """No closes yet must still yield a card per strategy with an explicit empty state."""
    cards = _cards(client)
    assert len(cards) == len(wd.strategy_state)
    for card in cards.values():
        assert card["trades"] == 0
        assert (
            card["equity"]["pnl"] == []
        )  # ---------------------------------------------------------------------------


# Truthfulness: DRY must never report the real account, and a strategy's state
# pill must reflect the OS rather than the flag the request that started it
# happened to set.
# ---------------------------------------------------------------------------
def _seed_two_books():
    """One DRY position/trade and one LIVE position/trade for the same strategy."""
    _db()

    async def seed():
        async with aiosqlite.connect(wd.DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO positions (market_id, side, entry_price, quantity,"
                " timestamp, live, status, strategy, mode)"
                " VALUES ('KXDRYPOS', 'YES', 0.40, 10, '2026-01-01T00:00:00', 0,"
                " 'open', 'ai_directional', 'dry')"
            )
            await conn.execute(
                "INSERT INTO positions (market_id, side, entry_price, quantity,"
                " timestamp, live, status, strategy, mode)"
                " VALUES ('KXLIVEPOS', 'YES', 0.60, 20, '2026-01-01T00:00:00', 1,"
                " 'open', 'ai_directional', 'live')"
            )
            for mid, pnl, mode in (("KXDRYTRD", 1.0, "dry"), ("KXLIVETRD", 50.0, "live")):
                await conn.execute(
                    "INSERT INTO trade_logs (market_id, side, entry_price, exit_price,"
                    " quantity, pnl, entry_timestamp, exit_timestamp, rationale,"
                    " strategy, mode) VALUES (?, 'yes', 0.40, 0.45, 5, ?,"
                    " '2026-01-01T00:00:00', '2026-01-02T00:00:00', 'book',"
                    " 'ai_directional', ?)",
                    (mid, pnl, mode),
                )
            await conn.commit()

    asyncio.run(seed())


def test_live_book_excludes_dry_rows(client):
    from src.utils.mode import TradingMode

    _seed_two_books()
    live = asyncio.run(TradingMode(db_path=wd.DB_PATH).live_account())
    assert live["deployed"] == 12.0  # 20 contracts at $0.60
    assert live["open_positions"] == 1
    assert live["realized"] == 50.0
    assert live["closed_trades"] == 1


def test_strategy_state_reads_the_database_not_the_request_flag(client, monkeypatch):
    """A pid recorded by another request - or another worker - must be honoured."""
    from src.utils.strategy_runtime import key as rt_key

    _db()
    from src.utils.strategy_runtime import StrategyRuntime

    store = StrategyRuntime(db_path=wd.DB_PATH)
    # A pid that is certainly alive: this very test process.
    asyncio.run(store.record_start("quick_flip", os.getpid(), "paper", "cli.py run --quick-flip"))

    # Wipe the in-memory flag, as a fresh worker or a redeploy would.
    wd.strategy_state["quick_flip"].update({"running": False, "pid": None})

    recorded = wd._recorded_state()
    assert recorded[rt_key("quick_flip", "paper")]["pid"] == os.getpid()
    assert wd.strategy_state["quick_flip"]["running"] is True
    assert "quick_flip" in wd._running_strategies()
    snap = client.get("/api/snapshot").get_json()
    cards = {c["name"]: c for c in snap["strategy_cards"]}
    assert cards["quick_flip"]["running"] is True
    assert cards["quick_flip"]["pid"] == os.getpid()


def test_strategy_state_records_why_a_dead_strategy_stopped(client):
    """A dead pid must read 'stopped' with a reason, not sit silently at zero."""
    _db()
    from src.utils.strategy_runtime import StrategyRuntime

    store = StrategyRuntime(db_path=wd.DB_PATH)
    # A pid that cannot exist, so the liveness probe must fail.
    asyncio.run(store.record_start("market_making", 999_999_999, "paper", "cli.py run"))
    wd.strategy_state["market_making"].update({"running": True, "pid": 999_999_999})

    wd._recorded_state()
    st = wd.strategy_state["market_making"]
    assert st["running"] is False
    assert st["pid"] is None
    assert "exited" in (st["stop_reason"] or "")

    # The reason must reach the page so the pill is never ambiguous.
    cards = {c["name"]: c for c in client.get("/api/snapshot").get_json()["strategy_cards"]}
    assert "exited" in cards["market_making"]["stop_reason"]


def test_strategy_state_is_empty_before_anything_runs(client):
    """Never started must be distinguishable from started-and-stopped."""
    _db()
    wd._recorded_state()
    cards = {c["name"]: c for c in client.get("/api/snapshot").get_json()["strategy_cards"]}
    for card in cards.values():
        assert card["running"] is False
        assert card["stop_reason"] == ""


def test_pid_alive_never_signals_the_process():
    """The Windows-safe probe must not terminate what it is asked about."""
    from src.utils.strategy_runtime import pid_alive

    assert pid_alive(os.getpid()) is True
    assert pid_alive(None) is False
    assert pid_alive(0) is False
    assert pid_alive(999_999_999) is False
    # Still alive after being probed - the regression that matters.
    assert pid_alive(os.getpid()) is True


# ---------------------------------------------------------------------------
# Regression: every spawned strategy died instantly on
# "Private key file not found: kalshi_private_key.pem". Railway injects
# KALSHI_PRIVATE_KEY as PEM text; KalshiClient loads a *path*, and only the
# dashboard was bridging the two. A child inherited the text and died, which is
# why every Start button read "stopped" no matter what it did.
# ---------------------------------------------------------------------------
class _RecordingPopen:
    """Captures the kwargs the dashboard spawns a strategy with."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 424242
        _RecordingPopen.instances.append(self)

    def poll(self):
        return None  # alive


def _toggle_env(monkeypatch, tmp_path, pem="-----BEGIN PRIVATE KEY-----\nxyz\n"):
    monkeypatch.setattr(wd.subprocess, "Popen", _RecordingPopen)
    monkeypatch.setattr(wd, "_child_procs", {})
    monkeypatch.setenv("KALSHI_API_KEY", "kid")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", pem)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "child.db"))
    # strategy_state is module-level, so a start from a previous test would make
    # the next one take the stop path and never spawn anything.
    for st in wd.strategy_state.values():
        st.update({"running": False, "pid": None, "stop_reason": ""})
    _RecordingPopen.instances = []
    return pem


def test_child_process_receives_a_readable_private_key_path(client, auth, monkeypatch, tmp_path):
    _toggle_env(monkeypatch, tmp_path)
    r = client.post("/api/strategy/ai_directional/toggle", json={"mode": "paper"}, headers=auth)
    assert r.status_code == 200
    env = _RecordingPopen.instances[-1].kwargs["env"]
    key_path = env.get("KALSHI_PRIVATE_KEY_PATH")
    assert key_path, "child got no KALSHI_PRIVATE_KEY_PATH"
    # The path must actually resolve to the PEM, or the child dies the same way.
    assert "BEGIN" in open(key_path, encoding="utf-8").read()


def test_child_process_shares_the_deployed_database(client, auth, monkeypatch, tmp_path):
    """Otherwise every strategy writes to a throwaway container database."""
    _toggle_env(monkeypatch, tmp_path)
    client.post("/api/strategy/ai_directional/toggle", json={"mode": "paper"}, headers=auth)
    env = _RecordingPopen.instances[-1].kwargs["env"]
    assert env["DB_PATH"] == str(wd.DB_PATH)


def test_every_strategy_command_is_paper(client, auth, monkeypatch, tmp_path):
    """A spawned strategy must never carry --live."""
    _toggle_env(monkeypatch, tmp_path)
    for name in wd.strategy_state:
        _RecordingPopen.instances = []
        r = client.post(f"/api/strategy/{name}/toggle", json={"mode": "paper"}, headers=auth)
        if name in wd.HEAVY_API_ABUSERS:
            # quick_flip is locked OFF as the #1 OpenRouter spender: the API
            # refuses Start with 403 and spawns nothing.
            assert r.status_code == 403, f"{name} abuser lockout bypassed"
            assert _RecordingPopen.instances == []
            continue
        if name in wd.RETIRED_LANES:
            # Source removed: Start is refused and nothing is spawned, so the
            # supervisor can never crash-loop a process that cannot exist.
            assert r.status_code == 409, f"{name} retired lane was not refused"
            assert _RecordingPopen.instances == []
            continue
        args = list(_RecordingPopen.instances[-1].args[0])
        assert "--live" not in args, f"{name} was spawned with --live"
        assert "--paper" in args, f"{name} was spawned without --paper"


def test_websocket_iterator_does_not_use_the_missing_messages_attribute():
    from src.jobs.market_data import _ws_messages

    class FakeWs:
        """An aiohttp response from 3.9: iterable, but with no .messages."""

        def __init__(self):
            self.frames = iter([1, 2, 3])

        def __aiter__(self):
            return self

        async def __anext__(self):
            return next(self.frames)

    ws = FakeWs()
    assert not hasattr(ws, "messages")
    assert _ws_messages(ws) is ws


# ---------------------------------------------------------------------------
# The page order the operator asked for: ENGINE tiles on top, DRY account under
# them, the live-feeds row under that. Everything else follows.
# ---------------------------------------------------------------------------
def test_page_order_tiles_then_feeds_then_account(client):
    """Feeds moved under the strategy cards, above the funding panel.

    The live picture of what the market is doing belongs next to the cards, not
    three panels down beneath the account history.
    """
    html = client.get("/").get_data(as_text=True)
    order = [
        html.index('<div class="tiles">'),
        html.index('id="feedsPanel"'),
        html.index('id="fBalance"'),
    ]
    assert order == sorted(order), "tiles -> feeds -> funding"
    # The feed charts must render above the account row.
    assert html.index('id="spotChart"') > html.index('<div class="tiles">')
    assert html.index('id="spotChart"') < html.index('id="fBalance"')


def test_the_feeds_panel_can_be_collapsed(client):
    """A tall panel whose numbers tick every 5s needs a way out of the way."""
    html = client.get("/").get_data(as_text=True)
    assert 'id="feedsPanel"' in html
    assert 'id="feedsBody"' in html
    assert 'id="feedToggle"' in html
    assert "function toggleFeeds()" in html
    assert "feedsHidden" in html  # the choice survives a reload


def test_live_page_funding_card_is_the_real_account(client, auth, monkeypatch):
    """In LIVE the funding card answers with the real Kalshi balance."""
    from src.utils.mode import TradingMode

    async def funded(_self, private_key_path=None):
        return {"connected": True, "balance": 41.05, "can_fund": True, "reason": ""}

    monkeypatch.setattr(TradingMode, "funding", funded)
    r = client.post("/api/mode", json={"mode": "live", "confirm": True}, headers=auth)
    assert r.status_code == 200
    html = client.get("/").get_data(as_text=True)
    funding = html.split("LIVE account", 1)[1].split("</dl>", 1)[0]
    assert "real Kalshi" in funding
    assert "$41.05" in funding
    client.post("/api/mode", json={"mode": "live", "confirm": True}, headers=auth)


# ---------------------------------------------------------------------------
# Every trade-derived figure is scoped to the book the switch is in.
# ---------------------------------------------------------------------------
def _seed_two_book_trades():
    _db()

    async def seed():
        async with aiosqlite.connect(wd.DB_PATH) as conn:
            for i, (pnl, mode, strategy) in enumerate(
                [
                    (1.0, "dry", "ai_directional"),
                    (-2.0, "dry", "ai_directional"),
                    (100.0, "live", "ai_directional"),
                ]
            ):
                await conn.execute(
                    "INSERT INTO trade_logs (market_id, side, entry_price, exit_price,"
                    " quantity, pnl, entry_timestamp, exit_timestamp, rationale,"
                    " strategy, mode) VALUES (?, 'yes', 0.40, 0.45, 5, ?,"
                    " '2026-10-01T00:00:00', ?, 'book test', ?, ?)",
                    (f"KXB{i}", pnl, f"2026-10-01T0{i + 1}:00:00", strategy, mode),
                )
            for i, (mode,) in enumerate([("dry",), ("live",)]):
                await conn.execute(
                    "INSERT INTO positions (market_id, side, entry_price, quantity,"
                    " timestamp, live, status, strategy, mode)"
                    " VALUES (?, 'YES', 0.30, 10, '2026-10-01T00:00:00',"
                    " ?, 'open', 'ai_directional', ?)",
                    (f"KXPOS{i}", 0 if mode == "dry" else 1, mode),
                )
            await conn.commit()

    asyncio.run(seed())


def test_trade_totals_are_book_scoped(client):
    _seed_two_book_trades()
    snap = client.get("/api/snapshot").get_json()
    # The real book: two closes, +1.00 and -2.00. The legacy +100.00 must not
    # appear beside them.
    assert snap["trades"]["trades"] == 2
    assert snap["trades"]["realized_pnl"] == -1.0
    assert len(snap["positions"]) == 1
    assert snap["positions"][0]["market_id"] == "KXPOS0"


def test_equity_curve_and_recent_trades_are_book_scoped(client):
    _seed_two_book_trades()
    snap = client.get("/api/snapshot").get_json()
    assert snap["equity"]["pnl"] == [1.0, -1.0]
    recent = snap["recent_trades"]
    assert len(recent) == 2
    assert all(abs(r["pnl"]) <= 2 for r in recent)


def test_by_strategy_is_book_scoped(client):
    _seed_two_book_trades()
    snap = client.get("/api/snapshot").get_json()
    strategies = {s["strategy"]: s for s in snap["by_strategy"]}
    assert strategies["ai_directional"]["pnl"] == -1.0
    assert strategies["ai_directional"]["trades"] == 2


# ---------------------------------------------------------------------------
# The Start button follows the mode switch; LIVE spawns --live.
# ---------------------------------------------------------------------------
def test_toggle_defaults_to_the_current_book(client, auth, monkeypatch):
    """A Start press with no mode resolves to the one book: DRY, --paper."""
    _RecordingPopen.instances = []
    monkeypatch.setattr(wd.subprocess, "Popen", _RecordingPopen)
    monkeypatch.setattr(wd, "_child_procs", {})
    monkeypatch.setenv("KALSHI_API_KEY", "kid")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nxyz\n")
    for st in wd.strategy_state.values():
        st.update({"running": False, "pid": None, "stop_reason": ""})

    # There is one book, so a request carrying no mode resolves to DRY.
    # (Uses btc_updown: safe_compounder is locked OFF as a heavy API abuser and its
    # Start is refused with 403.)
    from src.utils.mode import TradingMode

    async def funded(_self, private_key_path=None):
        return {"connected": True, "balance": 5000.0, "can_fund": True, "reason": ""}

    monkeypatch.setattr(TradingMode, "funding", funded)
    r = client.post("/api/strategy/btc_updown/toggle", json={}, headers=auth)
    assert r.status_code == 200
    args = list(_RecordingPopen.instances[-1].args[0])
    assert "--paper" in args
    assert "--live" not in args
    client.post("/api/mode", json={"mode": "live", "confirm": True}, headers=auth)


def test_start_all_button_label_follows_the_book(client):
    html = client.get("/").get_data(as_text=True)
    btn = html.split('id="startAllBtn"', 1)[1].split("</button>", 1)[0]
    assert "Start all in DRY" in btn
    assert "LIVE" not in btn


def test_live_feeds_do_not_depend_on_the_chart_cdn():
    """Chart.js failing must not silence the price feeds.

    Chart.js is loaded from a CDN. When that request was blocked or slow, `Chart`
    was undefined, the first `new Chart` threw, and it aborted the init script
    before `setInterval(refreshFeeds, 5000)` was ever registered - so the feed
    panel sat on its hardcoded "connecting..." / "--" defaults indefinitely while
    /api/marketdata served live data throughout. Every `new Chart` is now guarded
    and the pollers are registered first."""
    html = wd.app.test_client().get("/").get_data(as_text=True)

    # Pollers must be scheduled before the first chart is constructed.
    init = html[html.index("// --- init ---") :]
    assert init.index("setInterval(refreshFeeds, 5000)") < init.index("safeDraw(drawChart")

    # Every construction site is conditional on the library existing.
    assert html.count("typeof Chart === 'undefined'") >= 3
    assert "const CHARTS_OK = typeof Chart !== 'undefined';" in html

    # No unguarded construction or unguarded renderer is left on the init path.
    assert "safeDraw(drawChart, 'account')" in init
    assert "safeDraw(drawSparks, 'sparks')" in init
    assert "\ndrawChart();" not in init
    assert init.count("new Chart(") == 0


def test_strategy_cards_are_wide_enough_to_read_their_curve():
    """Cards were 258px wide with a 54px curve - too small to see a P&L line."""

    html = wd.app.test_client().get("/").get_data(as_text=True)
    assert "minmax(430px,1fr)" in html
    assert ".cards .cchart{height:104px" in html
    assert 'id="spark-' in html and 'height="104"' in html


def test_dry_page_does_not_point_at_a_panel_it_does_not_render(client):
    """The funding note used to reference a 'Real Kalshi account panel below'."""
    gen = _load_kalshi(client)
    next(gen)
    try:
        html = client.get("/").get_data(as_text=True)
    finally:
        gen.close()
    assert "Real Kalshi account" not in html
    assert "panel below" not in html


# ---------------------------------------------------------------------------
# The LIVE realized-P&L tile
#
# It used to sum `realized_pnl_dollars` over /portfolio/positions, which only
# describes markets still held. Kalshi drops a ticker from that list once it
# settles, taking its realized P&L with it, so the total reported the P&L of open
# markets only: it read a fixed $11.54 that never moved while trades opened and
# closed underneath it, next to a cost basis of $1144.22 and an exposure of $3.84
# that could not both be true. The totals now come from the fill and settlement
# history, which is complete by construction.
# ---------------------------------------------------------------------------

FILL_BUY_YES = {
    "ticker": "KXBTC15M-TEST",
    "outcome_side": "yes",
    "book_side": "bid",
    "count_fp": "10.00",
    "yes_price_dollars": "0.400000",
    "no_price_dollars": "0.600000",
    "fee_cost": "0.010000",
}
FILL_SELL_YES = {
    "ticker": "KXBTC15M-TEST",
    "outcome_side": "yes",
    "book_side": "ask",
    "count_fp": "10.00",
    "yes_price_dollars": "0.550000",
    "no_price_dollars": "0.450000",
    "fee_cost": "0.010000",
}


def test_a_sold_position_realizes_from_the_fills():
    """Bought at 0.40, sold at 0.55 on 10 contracts = +1.50."""
    led = wd._kalshi_ledger([FILL_BUY_YES, FILL_SELL_YES], [])
    assert led["realized"] == pytest.approx(1.50, abs=0.01)
    assert led["fees"] == pytest.approx(0.02, abs=0.001)
    # Everything bought was sold, so nothing is still deployed.
    assert led["cost_basis"] == pytest.approx(0.0, abs=0.01)


def test_realized_follows_a_settled_market_that_positions_no_longer_lists():
    """The regression: a settled ticker's P&L must still count.

    `positions` has no row for it, which is exactly why the old sum went stale.
    """
    settlement = {
        "ticker": "KXOLD-TEST",
        "market_result": "yes",
        "yes_count_fp": "10.00",
        "yes_total_cost_dollars": "4.000000",
        "no_count_fp": "0.00",
        "no_total_cost_dollars": "0.000000",
        "revenue": 1000,  # integer cents: 10 winners at 100c
        "fee_cost": "0.020000",
    }
    buy = dict(FILL_BUY_YES, ticker="KXOLD-TEST")
    led = wd._kalshi_ledger([buy], [settlement])
    # 10.00 revenue - 4.00 cost = +6.00, and it is not zero just because the
    # market is gone from the positions list.
    assert led["realized"] == pytest.approx(6.00, abs=0.01)
    assert led["settlements_counted"] == 1


def test_settled_cost_is_not_reported_as_capital_still_deployed():
    """The phantom-basis regression: settled contracts have no cost basis.

    open_cost was captured BEFORE settlements zeroed their tickers, so every
    buy-then-settled contract kept reporting its cost as capital at work:
    the tile read "exposure $1.92 - cost $485.82" for an account holding $2.
    """
    settlement = {
        "ticker": "KXOLD-TEST",
        "market_result": "yes",
        "yes_count_fp": "10.00",
        "yes_total_cost_dollars": "4.000000",
        "no_count_fp": "0.00",
        "no_total_cost_dollars": "0.000000",
        "revenue": 1000,
        "fee_cost": "0.020000",
    }
    buy = dict(FILL_BUY_YES, ticker="KXOLD-TEST")
    led = wd._kalshi_ledger([buy], [settlement])
    assert led["cost_basis"] == pytest.approx(0.0, abs=0.01)
    assert led["basis_by_ticker"] == {}
    # And a genuinely still-held ticker keeps its basis alongside.
    other = dict(FILL_BUY_YES, ticker="KXHELD-TEST")
    led2 = wd._kalshi_ledger([buy, other], [settlement])
    assert led2["cost_basis"] == pytest.approx(4.00, abs=0.01)
    assert led2["basis_by_ticker"] == {"KXHELD-TEST": pytest.approx(4.00, abs=0.01)}


def test_a_losing_settlement_is_reported_as_a_loss():
    settlement = {
        "ticker": "KXOLD-TEST",
        "market_result": "no",
        "yes_count_fp": "10.00",
        "yes_total_cost_dollars": "4.000000",
        "no_count_fp": "0.00",
        "no_total_cost_dollars": "0.000000",
        "revenue": 0,
        "fee_cost": "0.000000",
    }
    led = wd._kalshi_ledger([dict(FILL_BUY_YES, ticker="KXOLD-TEST")], [settlement])
    assert led["realized"] == pytest.approx(-4.00, abs=0.01)


def test_open_cost_basis_is_what_is_still_deployed():
    """Bought 20, sold 10: 10 shares' worth of capital is still at work."""
    bigger_buy = dict(FILL_BUY_YES, count_fp="20.00")
    led = wd._kalshi_ledger([bigger_buy, FILL_SELL_YES], [])
    assert led["cost_basis"] == pytest.approx(4.00, abs=0.01)
    assert led["realized"] == pytest.approx(1.50, abs=0.01)


def test_buying_a_no_fill_prices_off_the_no_leg():
    """A NO fill costs `no_price_dollars`, not the YES price.

    Reading the wrong leg is a ~2x error on every NO trade, and this strategy
    takes NO clips whenever spot sits below the target.
    """
    no_fill = {
        "ticker": "KXNO-TEST",
        "outcome_side": "no",
        "book_side": "bid",
        "count_fp": "10.00",
        "yes_price_dollars": "0.600000",
        "no_price_dollars": "0.400000",
        "fee_cost": "0.000000",
    }
    led = wd._kalshi_ledger([no_fill], [])
    assert led["cost_basis"] == pytest.approx(4.00, abs=0.01)


def test_selling_no_is_a_sale_and_reduces_cost_basis():
    """`outcome_side: no` + `book_side: ask` is selling NO, i.e. money in.

    outcome_side alone does not carry direction - the pair does. Reading it as a
    purchase would understate cost basis and invent a loss.
    """
    buy = {
        "ticker": "KXNO-TEST",
        "outcome_side": "no",
        "book_side": "bid",
        "count_fp": "10.00",
        "yes_price_dollars": "0.600000",
        "no_price_dollars": "0.400000",
        "fee_cost": "0.000000",
    }
    sell = dict(buy, book_side="ask", yes_price_dollars="0.550000", no_price_dollars="0.450000")
    led = wd._kalshi_ledger([buy, sell], [])
    assert led["realized"] == pytest.approx(0.50, abs=0.01)
    assert led["cost_basis"] == pytest.approx(0.0, abs=0.01)


def test_the_legacy_action_field_is_still_honoured():
    """Older payloads predate book_side; action is deprecated but not absent."""
    legacy = {
        "ticker": "KXLEG-TEST",
        "action": "buy",
        "side": "yes",
        "count_fp": "10.00",
        "yes_price_dollars": "0.400000",
        "fee_cost": "0.000000",
    }
    led = wd._kalshi_ledger([legacy], [])
    assert led["cost_basis"] == pytest.approx(4.00, abs=0.01)


def test_an_unusable_fill_is_skipped_rather_than_guessed():
    """A row with no ticker or no direction must not silently become money."""
    led = wd._kalshi_ledger(
        [
            {"count_fp": "10.00", "yes_price_dollars": "0.400000"},  # no ticker
            {"ticker": "KX", "count_fp": "10.00", "yes_price_dollars": "0.4"},  # no direction
            {"ticker": "KX", "book_side": "bid", "count_fp": "0", "yes_price_dollars": "0.4"},
            FILL_BUY_YES,
        ],
        [],
    )
    assert led["fills_counted"] == 1
    assert led["cost_basis"] == pytest.approx(4.00, abs=0.01)


def test_junk_rows_do_not_crash_the_ledger():
    led = wd._kalshi_ledger(["nope", None, {}], ["nope", None])
    assert led["realized"] == 0.0
    assert led["fills_counted"] == 0


def test_realized_pnl_is_not_sourced_from_open_positions(client, auth, monkeypatch):
    """The tile must not quote the positions-row sum as the account's P&L."""
    _go_live(client, auth, monkeypatch)
    monkeypatch.setitem(
        wd.dashboard_state,
        "ledger",
        wd._kalshi_ledger([FILL_BUY_YES, FILL_SELL_YES], []),
    )
    gen = _load_kalshi(client)
    next(gen)
    try:
        snap = client.get("/api/snapshot").get_json()
    finally:
        gen.close()
    k = snap["kalshi"]
    # The positions rows claim 0.30; the fills say 1.50. The fills win.
    assert k["positions_realized"] == pytest.approx(0.30, abs=0.01)
    assert k["realized"] == pytest.approx(1.50, abs=0.01)
    assert k["realized_known"] is True


def test_the_tile_says_na_rather_than_zero_when_history_is_missing(client, auth, monkeypatch):
    """A $0.00 realized P&L would read as break-even. It must read as unknown."""
    _go_live(client, auth, monkeypatch)
    monkeypatch.setitem(wd.dashboard_state, "ledger", None)
    # The monitor thread syncs Kalshi on its own interval and would repopulate the
    # ledger between the request and the assertion.
    monkeypatch.setattr(wd, "_refresh_kalshi", lambda: None)
    gen = _load_kalshi(client)
    next(gen)
    try:
        snap = client.get("/api/snapshot").get_json()
        html = client.get("/").get_data(as_text=True)
    finally:
        gen.close()
    assert snap["kalshi"]["realized_known"] is False
    tile = html.split('id="tKalshiPnl"', 1)[1].split("</div>", 1)[0]
    assert "n/a" in tile
    assert "$0.00" not in tile


def test_a_failed_history_fetch_keeps_the_balance_and_positions(client, monkeypatch):
    """The history is an enhancement; losing it must not blank the account panel.

    Fills and settlements are fetched after the client that served balance and
    positions has already been closed, so a failure there has to degrade only the
    realized-P&L tile. Taking the account panel down with it would leave the page
    unreadable for a figure that is merely unavailable.
    """
    import src.clients.kalshi_client as kc

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def get_balance(self):
            return {"balance": 5000}

        async def get_positions(self):
            return {"market_positions": [dict(KALSHI_MARKET_POSITION)], "event_positions": []}

        async def get_fills(self, **kw):
            raise RuntimeError("history exploded")

        async def get_settlements(self, **kw):
            raise RuntimeError("history exploded")

        async def close(self):
            return None

    monkeypatch.setattr(kc, "KalshiClient", FakeClient)
    monkeypatch.setattr(wd, "kalshi_configured", lambda: True)
    monkeypatch.setattr(wd, "materialize_private_key", lambda: "key.pem")

    balance, positions, fills, settlements = wd._run_async(wd._fetch_kalshi_data())
    # The account panel still has what it needs.
    assert wd._normalise_balance(balance)["balance"] == 5000
    assert positions["market_positions"]
    assert (fills, settlements) == ([], [])
    assert any("history exploded" in str(e.get("error")) for e in wd.dashboard_state["errors"])


def test_an_unreachable_history_leaves_the_tally_unknown_not_zero(client, auth, monkeypatch):
    """No history means the tile says so; it must never read as break-even."""
    _go_live(client, auth, monkeypatch)
    monkeypatch.setitem(wd.dashboard_state, "ledger", None)
    monkeypatch.setattr(wd, "_refresh_kalshi", lambda: None)
    gen = _load_kalshi(client)
    next(gen)
    try:
        k = client.get("/api/snapshot").get_json()["kalshi"]
    finally:
        gen.close()
    assert k["realized_known"] is False
    assert k["realized"] == 0.0
    assert k["cost_basis"] == 0.0


# ---------------------------------------------------------------------------
# /api/live/journal: the local mode='live' book must be fully visible
# ---------------------------------------------------------------------------
def test_live_journal_empty_shape(client):
    r = client.get("/api/live/journal")
    assert r.status_code == 200
    body = r.get_json()
    assert body["book"] == "live"
    assert body["open"] == [] and body["open_count"] == 0
    assert body["recent_closes"] == [] and body["closed_count"] == 0


def test_live_journal_shows_mode_live_rows_only(client):
    import aiosqlite

    async def seed():
        from src.utils.database import DatabaseManager

        db = DatabaseManager(db_path=wd.DB_PATH)
        await db.initialize()
        async with aiosqlite.connect(wd.DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO positions (market_id, side, entry_price, quantity,"
                " timestamp, live, status, strategy, mode)"
                " VALUES ('LIVE-1', 'YES', 0.64, 2, '2026-10-05T00:00:00', 0,"
                " 'open', 'btc_updown', 'live')"
            )
            await conn.execute(
                "INSERT INTO positions (market_id, side, entry_price, quantity,"
                " timestamp, live, status, strategy, mode)"
                " VALUES ('DRY-1', 'YES', 0.50, 10, '2026-10-05T00:00:00', 0,"
                " 'open', 'btc_updown', 'dry')"
            )
            await conn.commit()

    import asyncio

    asyncio.run(seed())
    body = client.get("/api/live/journal").get_json()
    assert body["open_count"] == 1
    assert body["open"][0]["market_id"] == "LIVE-1"
    assert body["open"][0]["notional"] == 1.28
    assert body["open_notional"] == 1.28


# ---------------------------------------------------------------------------
# Snapshot single-flight: one rebuild serves a burst, never a pileup
# ---------------------------------------------------------------------------
def test_snapshot_burst_triggers_a_single_rebuild(client, monkeypatch):
    """Five concurrent misses must cost one build, not five Kalshi walks."""
    import threading
    import time as _time

    calls = []

    def _slow_build():
        calls.append(1)
        _time.sleep(0.5)
        return {"marker": len(calls)}

    monkeypatch.setattr(wd, "build_snapshot", _slow_build)
    stale = {"marker": "stale"}
    # Present but long expired: every thread misses the cache, but only the
    # first may rebuild while the rest serve this stale payload.
    monkeypatch.setitem(wd._SNAPSHOT_CACHE, "payload", stale)
    monkeypatch.setitem(wd._SNAPSHOT_CACHE, "at", 0.0)

    results = []

    def _call():
        results.append(wd.build_snapshot_cached())

    threads = [threading.Thread(target=_call) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(calls) == 1
    fresh = [r for r in results if r is not stale]
    assert len(results) == 5
    assert len(fresh) == 1 and fresh[0] == {"marker": 1}
    assert sum(1 for r in results if r is stale) == 4


# ---------------------------------------------------------------------------
# Kalshi freshness truth: numbers carry their age, changes push an event
# ---------------------------------------------------------------------------
def _fresh_state(monkeypatch):
    monkeypatch.setitem(wd.dashboard_state, "balance", None)
    monkeypatch.setitem(wd.dashboard_state, "positions", [])
    monkeypatch.setitem(wd.dashboard_state, "ledger", None)
    monkeypatch.setitem(wd.dashboard_state, "kalshi_at", None)
    monkeypatch.setitem(wd.dashboard_state, "kalshi_at_ts", 0.0)
    monkeypatch.setattr(wd, "_LAST_SNAPSHOT_DIGEST", None)
    monkeypatch.setitem(wd._SNAPSHOT_CACHE, "payload", None)
    monkeypatch.setitem(wd._SNAPSHOT_CACHE, "at", 0.0)


def test_successful_sync_stamps_kalshi_time(monkeypatch, tmp_path):
    """Only a sync that delivers a balance moves the stamp (the $0.29 rule)."""
    import time as _time

    _fresh_state(monkeypatch)
    monkeypatch.setattr(wd, "DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(wd, "_refresh_market_titles", lambda *a, **k: None)

    async def _fake_fetch():
        return (
            {"balance": 29},
            {"market_positions": [], "event_positions": []},
            [],
            [],
        )

    monkeypatch.setattr(wd, "_fetch_kalshi_data", _fake_fetch)
    wd._refresh_kalshi()
    assert wd.dashboard_state["balance"] == pytest.approx(0.29)
    assert wd.dashboard_state["kalshi_at"] is not None
    assert wd.dashboard_state["kalshi_at_ts"] > _time.time() - 60


def test_failed_sync_keeps_the_old_stamp(monkeypatch, tmp_path):
    """A dead sync must age the numbers, never freeze them silently."""
    _fresh_state(monkeypatch)
    monkeypatch.setattr(wd, "DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setitem(wd.dashboard_state, "balance", 0.29)
    monkeypatch.setitem(wd.dashboard_state, "kalshi_at", "2026-10-05 12:00:00")
    monkeypatch.setitem(wd.dashboard_state, "kalshi_at_ts", 1000.0)

    async def _boom():
        raise RuntimeError("Kalshi down")

    monkeypatch.setattr(wd, "_fetch_kalshi_data", _boom)
    with pytest.raises(RuntimeError):
        wd._refresh_kalshi()
    assert wd.dashboard_state["balance"] == pytest.approx(0.29)
    assert wd.dashboard_state["kalshi_at_ts"] == 1000.0
    assert wd._kalshi_age_sec() > 3600


def test_publish_fires_once_per_change(monkeypatch):
    """The monitor busts the memo and pushes SSE exactly when Kalshi moves."""
    _fresh_state(monkeypatch)
    events = []
    monkeypatch.setattr(wd, "_broadcast", lambda e, d: events.append((e, d)))
    monkeypatch.setitem(wd.dashboard_state, "balance", 1.0)

    assert wd._publish_if_changed() is True
    assert [e for e, _ in events] == ["snapshot"]
    assert wd._SNAPSHOT_CACHE["payload"] is None
    # Same numbers again: quiet, no rebuild, no event.
    assert wd._publish_if_changed() is False
    assert len(events) == 1
    # A fill moves the balance: fires again.
    monkeypatch.setitem(wd.dashboard_state, "balance", 2.0)
    assert wd._publish_if_changed() is True
    assert len(events) == 2


def test_snapshot_carries_kalshi_age(monkeypatch, tmp_path):
    """The tally tiles render from these two fields."""
    import time as _time

    _fresh_state(monkeypatch)
    monkeypatch.setattr(wd, "DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path / "logs")
    wd.app.config["TESTING"] = False
    try:
        monkeypatch.setitem(wd.dashboard_state, "balance", 5.0)
        monkeypatch.setitem(wd.dashboard_state, "kalshi_at", "2026-10-05 13:00:00")
        monkeypatch.setitem(wd.dashboard_state, "kalshi_at_ts", _time.time())
        payload = wd.build_snapshot_cached()
    finally:
        wd.app.config["TESTING"] = True
    assert payload["kalshi_at"] == "2026-10-05 13:00:00"
    assert 0 <= payload["kalshi_age_sec"] <= 30


# ---------------------------------------------------------------------------
# Latch reset: the surgical kill for stale volume state.
# ---------------------------------------------------------------------------
def test_reset_state_walls_every_lane_off_in_both_books(client, auth, monkeypatch, tmp_path):
    """Every recorded lane, both books, down - and only that is touched."""
    from src.utils.strategy_runtime import StrategyRuntime, key

    db = str(tmp_path / "reset_state.db")
    monkeypatch.setattr(wd, "DB_PATH", db)
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path / "logs")
    store = StrategyRuntime(db_path=db)

    async def seed():
        await store.record_start("btc_updown", 999101, "paper", "cli.py run")
        await store.record_start("xrp_updown", 999102, "live", "cli.py run --live")
        await store.record_start("hyperliquid_updown", 999103, "paper", "cli.py run")

    asyncio.run(seed())

    r = client.post("/api/strategies/reset-state", headers=auth)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True
    assert body["lanes_cleared"] >= 3

    snap = asyncio.run(store.snapshot())
    for lane, book in (
        ("btc_updown", "paper"),
        ("xrp_updown", "live"),
        ("hyperliquid_updown", "paper"),
    ):
        row = snap[key(lane, book)]
        assert row["desired"] == 0
        assert (row["stop_reason"] or "") in wd._OPERATOR_STOP_REASONS


def test_live_mode_is_blood_red(client, auth, monkeypatch, tmp_path):
    """Operator order: pressing LIVE turns the button's word blood red and
    makes the LIVE MODE flag red with a red border (both books of styling
    are class-driven, so the poll update keeps it red without a reload)."""
    import asyncio

    from src.utils.mode import TradingMode

    db = str(tmp_path / "blood.db")
    monkeypatch.setattr(wd, "DB_PATH", db)
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path / "logs")
    # Persist LIVE directly: the endpoint rightly refuses to go live without
    # Kalshi credentials, and this test is about the RENDERED styling.
    asyncio.run(TradingMode(db_path=db).set("live", confirmed=True))

    html = client.get("/").get_data(as_text=True)
    assert "--blood" in html
    # The live classes are on the right elements...
    assert 'id="modeLive"' in html and "modebtn live on" in html
    assert 'id="modeFlag"' in html and "modeflag live" in html
    # ...and the red rules hang off exactly those classes.
    assert ".modebtn.live.on" in html
    assert "color:var(--blood)" in html
    assert "border-color:var(--blood)" in html


def test_dashboard_shows_the_calibration_report_card(client):
    """The tally time frames are labelled and the calibration table exists:
    real realized P&L is labelled by ledger, bot P&L says 'closed trades',
    and the outcomes-by-entry-fair table is rendered."""
    html = client.get("/").get_data(as_text=True)
    assert "Real realized P&amp;L" in html
    assert "Bot realized P&amp;L" in html and "closed trades" in html
    assert "Calibration" in html and "fairBandsBody" in html
    assert "Entry fair band" in html


def test_config_edits_survive_a_restart(client, auth, monkeypatch, tmp_path):
    """The 'max positions keeps reverting to 10' bug: a saved edit must be
    persisted in runtime_config and re-applied at the next process start."""
    import asyncio

    from src.config.settings import settings as _S
    from src.utils.mode import apply_persisted_trading_config

    db = str(tmp_path / "cfg_survive.db")
    monkeypatch.setattr(wd, "DB_PATH", db)
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path / "logs")
    before = _S.trading.max_positions
    try:
        r = client.post("/api/config", json={"max_positions": 77}, headers=auth)
        assert r.status_code == 200, r.get_data(as_text=True)
        assert _S.trading.max_positions == 77
        # Simulate the redeploy: memory is fresh, only the DB remembers.
        _S.trading.max_positions = before
        applied = asyncio.run(apply_persisted_trading_config(db))
        assert applied >= 1
        assert _S.trading.max_positions == 77
    finally:
        _S.trading.max_positions = before


def test_default_max_positions_is_one_hundred():
    """Operator order: 100 concurrent positions, both books, nothing can
    revert it - the dataclass default itself is 100."""
    from src.config.settings import settings as _S

    assert _S.trading.max_positions == 100
    assert _S.trading.max_positions_per_strategy == 100
    # The manager's hardcoded cap must honor the same order.
    import inspect

    from src.utils.position_limits import PositionLimitsManager

    src = inspect.getsource(PositionLimitsManager.__init__)
    assert "self.max_positions = 100" in src


def test_calibration_excludes_phantoms_and_never_says_unknown():
    """The 15 'unknown' rows were phantoms (entered, never filled). The
    calibration query must exclude them, bin certain-win arithmetic
    separately, and label any true leftover 'legacy / other' - never
    'unknown'."""
    import asyncio

    import aiosqlite

    sql = wd._SQL_FAIR_BANDS.format(book="1=1")
    assert "no_kalshi_position" in sql
    assert "certain-win" in sql
    assert "legacy / other" in sql
    assert "'unknown'" not in sql

    async def _run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.execute(
                "CREATE TABLE trade_logs (exit_reason TEXT, entry_fair REAL,"
                " rationale TEXT, pnl REAL)"
            )
            await conn.execute(
                "INSERT INTO trade_logs (exit_reason, entry_fair, pnl) VALUES"
                " ('take_profit', 0.70, 1.0)"
            )
            await conn.execute(
                "INSERT INTO trade_logs (exit_reason, entry_fair, pnl) VALUES"
                " ('no_kalshi_position', NULL, 0.0)"
            )
            await conn.execute(
                "INSERT INTO trade_logs (exit_reason, entry_fair, rationale, pnl)"
                " VALUES ('take_profit', NULL, 'CERTAIN-WIN pair x', 0.05)"
            )
            await conn.execute(
                "INSERT INTO trade_logs (exit_reason, entry_fair, rationale, pnl)"
                " VALUES ('take_profit', NULL, 'old format', -0.5)"
            )
            cur = await conn.execute(sql)
            rows = {r[0]: r for r in await cur.fetchall()}
            assert "0.65-0.75" in rows and rows["0.65-0.75"][1] == 1
            assert "certain-win" in rows
            assert "legacy / other" in rows
            assert "unknown" not in rows
            # Phantom row is nowhere to be found.
            assert sum(r[1] for r in rows.values()) == 3

    asyncio.run(_run())


def test_api_trades_lists_rows_with_entry_fair(client, monkeypatch, tmp_path):
    """Regression: adding entry_fair to the SELECT must be mirrored in the
    unpack, or the endpoint dies with 'too many values to unpack'."""
    import asyncio

    import aiosqlite

    db = str(tmp_path / "trades_api.db")
    monkeypatch.setattr(wd, "DB_PATH", db)
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path / "logs")

    async def _seed():
        async with aiosqlite.connect(db) as conn:
            await conn.execute(
                "CREATE TABLE trade_logs (market_id TEXT, side TEXT,"
                " entry_price REAL, pnl REAL, quantity REAL, exit_timestamp TEXT,"
                " mode TEXT, strategy TEXT, rationale TEXT, exit_reason TEXT,"
                " entry_fair REAL)"
            )
            await conn.execute(
                "INSERT INTO trade_logs VALUES ('KX-T','YES',0.5,1.0,2,'t1','dry',"
                "'btc_updown','r','market_resolution',0.8)"
            )
            await conn.commit()

    asyncio.run(_seed())
    r = client.get("/api/trades?mode=dry")
    assert r.status_code == 200, r.get_data(as_text=True)
    trades = r.get_json()["trades"]
    assert trades and trades[0]["entry_fair"] == 0.8


def test_recent_closed_trades_api_paginates_current_book(client):
    import asyncio

    _db()

    async def seed():
        async with aiosqlite.connect(wd.DB_PATH) as conn:
            for index in range(27):
                await conn.execute(
                    "INSERT INTO trade_logs (market_id, side, entry_price, exit_price,"
                    " quantity, pnl, entry_timestamp, exit_timestamp, rationale,"
                    " strategy, mode, exit_reason) VALUES (?, 'yes', 0.40, 0.50, 2, 0.20,"
                    " '2026-01-01T00:00:00', ?, 'pagination test', 'btc_updown',"
                    " 'dry', 'market_resolution')",
                    (f"KXPAGE{index:02d}", f"2026-01-{index + 1:02d}T00:00:00"),
                )
            await conn.execute(
                "INSERT INTO trade_logs (market_id, side, entry_price, exit_price,"
                " quantity, pnl, entry_timestamp, exit_timestamp, rationale, strategy,"
                " mode, exit_reason) VALUES ('KXPHANTOM', 'yes', 0.4, 0.4, 1, 0,"
                " '2026-01-01', '2026-01-29', 'not filled', 'btc_updown', 'live',"
                " 'no_kalshi_position')"
            )
            await conn.execute(
                "INSERT INTO trade_logs (market_id, side, entry_price, exit_price,"
                " quantity, pnl, entry_timestamp, exit_timestamp, rationale, strategy,"
                " mode, exit_reason) VALUES ('KXLEGACYPAGE', 'yes', 0.4, 0.5, 2, 0.2,"
                " '2026-01-01', '2026-01-30', 'live', 'btc_updown', 'dry',"
                " 'market_resolution')"
            )
            await conn.commit()

    asyncio.run(seed())
    pages = [client.get(f"/api/recent-trades?page={page}&per_page=10") for page in (1, 2, 3)]
    assert all(response.status_code == 200 for response in pages)
    data = [response.get_json() for response in pages]

    assert [page["total"] for page in data] == [27, 27, 27]
    ids = [t["market_id"] for t in data[0]["trades"]]
    assert "KXLEGACYPAGE" not in ids
    assert [page["pages"] for page in data] == [3, 3, 3]
    assert [len(page["trades"]) for page in data] == [10, 10, 7]
    assert data[0]["trades"][0]["market_id"] == "KXPAGE26"
    assert not {trade["market_id"] for page in data for trade in page["trades"]} & {
        "KXPHANTOM",
        "KXLIVEPAGE",
    }
    page_ids = [{trade["market_id"] for trade in page["trades"]} for page in data]
    assert not (page_ids[0] & page_ids[1] or page_ids[0] & page_ids[2] or page_ids[1] & page_ids[2])


def test_rtf_download_is_current_book_scoped_and_escapes_content(client):
    import asyncio

    _db()

    async def seed():
        async with aiosqlite.connect(wd.DB_PATH) as conn:
            for market_id, mode, exit_reason in (
                ("KX{LIVE}\\ROLL", "dry", "market_resolution"),
                ("KXLEGACYONLY", "live", "market_resolution"),
                ("KXNOTFILLED", "dry", "no_kalshi_position"),
            ):
                await conn.execute(
                    "INSERT INTO trade_logs (market_id, side, entry_price, exit_price,"
                    " quantity, pnl, entry_timestamp, exit_timestamp, rationale,"
                    " strategy, mode, exit_reason) VALUES (?, 'yes', 0.4, 0.5, 2, 0.2,"
                    " '2026-01-01', '2026-01-02', 'rtf test', 'btc_updown', ?, ?)",
                    (market_id, mode, exit_reason),
                )
            await conn.commit()

    asyncio.run(seed())
    response = client.get("/api/trades/rtf")
    html = client.get("/").get_data(as_text=True)
    content = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.mimetype == "application/rtf"
    assert "attachment;" in response.headers["Content-Disposition"]
    assert content.startswith("{\\rtf1")
    assert "KX\\{LIVE\\}\\\\ROLL" in content
    assert "KXLEGACYONLY" not in content
    assert "KXNOTFILLED" not in content
    assert 'href="/api/trades/rtf"' in html
    assert "Download RTF" in html
    assert 'id="recentTradesBody"' in html
    assert 'id="recentTradesPrev"' in html
    assert 'id="recentTradesNext"' in html
    assert "function loadRecentTrades" in html


def test_recent_trade_renderer_can_format_money_outside_snapshot_paint():
    template = wd._TEMPLATE
    renderer_start = template.index("async function loadRecentTrades")
    paint_start = template.index("function paint(s)", renderer_start)

    assert "function money(v)" in template[:renderer_start]
    assert "const money =" not in template[paint_start:]


def test_parse_entry_fair_reads_the_chosen_side():
    """The rationale proves the fair; garbage yields None, never a guess.
    Fair 1.00 is VALID (deadband-past-target certainty) - the strict '< 1.0'
    bound was exactly what left the last 15 calibration rows unknown."""
    assert (
        wd._parse_entry_fair(
            "coinbase-coinbase-ws 82,457 vs target 82,478 (-20.51): fair 0.81 vs "
            "Kalshi 0.44 on DOWN - edge +0.382"
        )
        == 0.81
    )
    assert wd._parse_entry_fair("fair 0.55 vs Kalshi 0.50 on UP - edge +0.05") == 0.55
    assert wd._parse_entry_fair("fair 1.00 vs Kalshi 0.46 on DOWN - edge +0.540") == 1.00
    assert wd._parse_entry_fair("no fair here") is None
    assert wd._parse_entry_fair(None) is None
    assert wd._parse_entry_fair("fair 1.50 vs Kalshi") is None


def test_backfill_fair_identifies_the_unknown_rows(client, auth, monkeypatch, tmp_path):
    """The 342-'unknown' fix: rationale-parsed fair lands in both tables;
    unparseable rows stay unknown; re-running is idempotent."""
    import aiosqlite

    db = str(tmp_path / "backfill.db")
    monkeypatch.setattr(wd, "DB_PATH", db)
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path / "logs")

    import asyncio

    async def _seed():
        async with aiosqlite.connect(db) as conn:
            await conn.execute(
                "CREATE TABLE trade_logs (id INTEGER PRIMARY KEY, rationale TEXT, entry_fair REAL)"
            )
            await conn.execute(
                "CREATE TABLE positions (id INTEGER PRIMARY KEY, rationale TEXT, entry_fair REAL)"
            )
            await conn.execute(
                "INSERT INTO trade_logs (rationale) VALUES "
                "('fair 0.81 vs Kalshi 0.64 on DOWN - edge +0.183')"
            )
            await conn.execute("INSERT INTO trade_logs (rationale) VALUES ('no parseable fair')")
            await conn.execute(
                "INSERT INTO positions (rationale) VALUES "
                "('fair 0.58 vs Kalshi 0.52 on UP - edge +0.06')"
            )
            await conn.commit()

    asyncio.run(_seed())
    r = client.post("/api/maintenance/backfill-fair", headers=auth)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["trade_logs_updated"] == 1
    assert body["positions_updated"] == 1
    assert body["unparseable"] == 1

    async def _check():
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT entry_fair FROM trade_logs WHERE id=1")
            assert (await cur.fetchone())[0] == 0.81
            cur = await conn.execute("SELECT entry_fair FROM trade_logs WHERE id=2")
            assert (await cur.fetchone())[0] is None
            cur = await conn.execute("SELECT entry_fair FROM positions WHERE id=1")
            assert (await cur.fetchone())[0] == 0.58

    asyncio.run(_check())
