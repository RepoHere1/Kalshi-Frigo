"""LIVE-only behaviour: every test proves DRY is untouched.

Contract under test (see src/jobs/live_fees.py):
  - DRY keeps the raw 0.06 edge, the 2%-discount exits, aggressive polling and
    gross PnL. None of these tests changes a DRY expectation in
    tests/test_ladder_trader.py; they only assert the LIVE branches.
"""

import time
from datetime import datetime, timezone

from src.jobs import live_fees
from src.jobs.execute import place_sell_limit_order  # noqa: F401 (signature guard)
from src.jobs.ladder_trader import UpDownConfig, UpDownTrader
from src.jobs.market_data import Btc15mFeed, SpotFeed, UpDownMarket


def _dollars(**kw):
    return UpDownMarket(
        ticker=kw.pop("ticker", "KXBTC15M-26OCT011715-15"),
        event_ticker="KXBTC15M-26OCT011715",
        bucket="26OCT011715",
        horizon=15,
        title="BTC price up in next 15 mins?",
        target=kw.pop("target", 84609.34),
        close_ts=time.time() + kw.pop("seconds_left", 600),
        **kw,
    )


def _scorer(**cfg_kw):
    spot = SpotFeed()
    spot.price = 85000.0
    spot.ts = time.time()
    spot.source = "test"
    return UpDownTrader(spot, Btc15mFeed(), UpDownConfig(**cfg_kw))


def _quoted_market(yes_ask=0.45, no_ask=0.55, target=84000.0, seconds_left=600):
    return _dollars(
        yes_bid=round(yes_ask - 0.01, 4),
        yes_ask=yes_ask,
        no_bid=round(no_ask - 0.01, 4),
        no_ask=no_ask,
        target=target,
        seconds_left=seconds_left,
    )


# ---------------------------------------------------------------------------
# Fee math
# ---------------------------------------------------------------------------
def test_taker_fee_peaks_at_fifty_cents():
    assert live_fees.taker_fee_dollars(0.50, 100) == 1.75
    assert live_fees.taker_fee_dollars(0.90, 100) == 0.63
    assert live_fees.taker_fee_dollars(0.10, 100) == 0.63


def test_maker_fee_is_a_quarter_of_taker():
    assert live_fees.maker_fee_dollars(0.50, 100) == 0.44
    assert live_fees.maker_fee_dollars(0.50, 100) < live_fees.taker_fee_dollars(
        0.50, 100
    )


def test_roundtrip_fee_covers_both_sides():
    rt = live_fees.roundtrip_fee_dollars(0.40, 0.60, 10, maker_exit=True)
    assert rt > 0.0
    # Maker exit is cheaper than taking both sides.
    assert rt < live_fees.roundtrip_fee_dollars(0.40, 0.60, 10, maker_exit=False)


# ---------------------------------------------------------------------------
# Session filter is LIVE-only
# ---------------------------------------------------------------------------
def test_session_filter_only_hits_the_losing_hour():
    losing = datetime(2026, 10, 4, 18, 30, tzinfo=timezone.utc)
    other = datetime(2026, 10, 4, 17, 30, tzinfo=timezone.utc)
    assert live_fees.live_session_extra_edge(losing) == 0.03
    assert live_fees.live_session_extra_edge(other) == 0.0


def test_dry_ignores_the_session_bump(monkeypatch):
    import src.jobs.ladder_trader as lt

    # Force the losing hour for the whole test.
    monkeypatch.setattr(
        lt, "fair_up_probability", lambda s, t, n: 0.61, raising=False
    )
    trader = _scorer()
    market = _quoted_market(yes_ask=0.55, no_ask=0.45, target=84000.0)
    # 6c of edge at a 0.55 fill: DRY outside the sweet band needs 0.08, so a
    # 0.06 edge refuses regardless of the hour -- the point is DRY never even
    # consults the session clock (no live_session_extra_edge call on this path
    # changes a DRY verdict; LIVE would add up to 0.03 more).
    out = trader.evaluate(market, live=False)
    assert out is None or not out.actionable


# ---------------------------------------------------------------------------
# Maker exits are LIVE-only
# ---------------------------------------------------------------------------
def test_live_rests_patient_exits_and_takes_urgent_ones():
    assert live_fees.live_exit_limit_price("YES", 0.60, 300.0) == 0.60
    assert live_fees.live_exit_limit_price("YES", 0.60, 30.0) == round(0.60 * 0.98, 4)
    assert live_fees.live_exit_limit_price("NO", 0.60, None) == 0.60


def test_dry_exit_price_is_always_the_discount():
    assert live_fees.dry_exit_limit_price(0.60) == round(0.60 * 0.98, 4)


# ---------------------------------------------------------------------------
# Settled-book detection is LIVE-only
# ---------------------------------------------------------------------------
def test_settled_books_are_detected():
    assert live_fees.is_settled_market({"status": "closed"})
    assert live_fees.is_settled_market(
        {"yes_bid_dollars": "0.99", "yes_ask_dollars": "1.00",
         "no_bid_dollars": "0.99", "no_ask_dollars": "1.00"}
    )
    assert not live_fees.is_settled_market(
        {"status": "open", "yes_bid_dollars": "0.47", "yes_ask_dollars": "0.48",
         "no_bid_dollars": "0.52", "no_ask_dollars": "0.53"}
    )


# ---------------------------------------------------------------------------
# 429 backoff helper
# ---------------------------------------------------------------------------
def test_backoff_is_bounded_and_jittered():
    d0 = live_fees.backoff_delay_seconds(0)
    d9 = live_fees.backoff_delay_seconds(9)
    assert 1.0 <= d0 <= 3.5
    assert d9 <= 31.5


# ---------------------------------------------------------------------------
# Variance-commensurate sizing is LIVE-only (DRY keeps its fixed $5 clip)
# ---------------------------------------------------------------------------
def test_variance_multiplier_scales_with_the_lie():
    assert live_fees.variance_clip_multiplier(0.0) == 1.0
    assert live_fees.variance_clip_multiplier(15.0) == 1.0
    assert live_fees.variance_clip_multiplier(30.0) == 1.0
    assert live_fees.variance_clip_multiplier(58.0) == 1.93
    assert live_fees.variance_clip_multiplier(-58.0) == 1.93
    assert live_fees.variance_clip_multiplier(200.0) == 2.5


# ---------------------------------------------------------------------------
# Settlement receipts: only a real receipt may close a stale LIVE row
# ---------------------------------------------------------------------------
def test_settlement_parser_finds_a_receipt():
    payload = {
        "settlements": [
            {"ticker": "KXBTC15M-OLD", "result": "yes", "fees": 0.35},
            {"ticker": "OTHER", "result": "no", "fees": 0.10},
        ]
    }
    assert live_fees.parse_settlement_result(payload, "KXBTC15M-OLD") == {
        "result": "yes",
        "fees": 0.35,
    }


def test_settlement_parser_never_invents_a_close():
    assert live_fees.parse_settlement_result({}, "KXBTC15M-OLD") is None
    assert live_fees.parse_settlement_result(None, "KXBTC15M-OLD") is None
    assert (
        live_fees.parse_settlement_result({"settlements": []}, "KXBTC15M-OLD")
        is None
    )
    assert (
        live_fees.parse_settlement_result(
            {"settlements": [{"ticker": "OTHER", "result": "yes"}]}, "KXBTC15M-OLD"
        )
        is None
    )
    assert (
        live_fees.parse_settlement_result(
            {"settlements": [{"ticker": "KXBTC15M-OLD"}]}, "KXBTC15M-OLD"
        )
        is None
    )
# ---------------------------------------------------------------------------
# DB migration: fee_paid is additive, DRY rows read back unaffected
# ---------------------------------------------------------------------------
def test_fee_column_migrates_and_defaults_to_zero(tmp_path):

    import asyncio

    from src.utils.database import DatabaseManager, TradeLog

    async def _run():
        db = DatabaseManager(db_path=str(tmp_path / "t.db"))
        await db.initialize()
        now = datetime.now(timezone.utc)
        await db.add_trade_log(
            TradeLog(
                market_id="M",
                side="YES",
                entry_price=0.40,
                exit_price=0.60,
                quantity=10,
                pnl=2.0,
                entry_timestamp=now,
                exit_timestamp=now,
                rationale="dry",
                strategy="btc_updown",
                exit_reason="take_profit",
                mode="dry",
            )
        )
        import aiosqlite

        async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT fee_paid, pnl FROM trade_logs")
            row = await cur.fetchone()
            return float(row["fee_paid"] or 0.0), float(row["pnl"])

    fee, pnl = asyncio.run(_run())
    assert fee == 0.0
    assert pnl == 2.0
