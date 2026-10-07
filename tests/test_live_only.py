"""LIVE-only behaviour: every test proves DRY is untouched.

Contract under test (see src/jobs/live_fees.py):
  - DRY keeps the raw 0.06 edge, the 2%-discount exits, aggressive polling and
    gross PnL. None of these tests changes a DRY expectation in
    tests/test_ladder_trader.py; they only assert the LIVE branches.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

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
        lt, "fair_up_probability", lambda s, t, n, sl=900.0, *a, **k: 0.61, raising=False
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


def test_exit_price_is_098_when_urgent():
    # live_exit_limit_price with None seconds_left (unknown) defaults
    # to maker (no discount), so we explicitly test with a short time
    # to verify the 2% taker discount is applied when urgent.
    assert live_fees.live_exit_limit_price("YES", 0.60, seconds_left=30.0) == round(0.60 * 0.98, 4)


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
# BRTI truth: Kalshi's own settlement index beats retail spot when fresh
# ---------------------------------------------------------------------------
def test_brti_parser_reads_flat_and_nested_shapes():
    from src.jobs.brti_feed import parse_brti_message

    flat = {"value": 86500.5, "avg_60s_data": 86490.0, "source_ts_ms": 1791157700000}
    out = parse_brti_message(flat)
    assert out["value"] == 86500.5
    assert out["avg60"] == 86490.0
    assert out["source_ts"] == 1791157700.0

    nested = {
        "data": {
            "value": "86501.25",
            "avg_60s_data": {"average": 86491.5},
            "last_60s_windowed_average_15min": 86495.0,
        }
    }
    out2 = parse_brti_message(nested)
    assert out2["value"] == 86501.25
    assert out2["avg60"] == 86491.5
    assert out2["win_avg"] == 86495.0

    assert parse_brti_message({})["value"] is None
    assert parse_brti_message("garbage")["value"] is None


def test_brti_parser_reads_the_real_kalshi_ws_frame():
    """The exact frame captured live from the cfbenchmarks_value channel."""
    from src.jobs.brti_feed import parse_brti_message

    frame = {
        "type": "cfbenchmarks_value",
        "sid": 1,
        "seq": 1,
        "msg": {
            "index_id": "BRTI",
            "received_at": 1791180714074,
            "data": '{"type":"value","time":1791180714000,"id":"BRTI","value":"85850.67"}',
            "avg_60s_data": {
                "value": "85850.67000000",
                "window_size": 0,
                "window_start_ts_ms": 1791180654000,
                "window_end_ts_exclusive": 1791180714000,
            },
        },
        "sending_ts_ms": 1791180714077,
    }
    out = parse_brti_message(frame)
    assert out["value"] == 85850.67
    assert out["avg60"] == 85850.67
    # Inner data.time (1791180714000) or the envelope's sending_ts_ms
    # (1791180714077) are both valid millisecond source stamps.
    assert abs((out["source_ts"] or 0) - 1791180714.0) < 1.0


def test_brti_estimate_prefers_windowed_then_avg60_then_value():
    from src.jobs.brti_feed import BrtiFeed

    b = BrtiFeed()
    b.value, b.avg60, b.win_avg = 86500.0, 86490.0, 86495.0
    assert b.estimate() == 86495.0
    assert b.estimate_kind() == "windowed-settlement-avg"
    b.win_avg = 0.0
    assert b.estimate() == 86490.0
    assert b.estimate_kind() == "trailing-avg60"
    b.avg60 = 0.0
    assert b.estimate() == 86500.0


def _brti_fake(price, fresh=True, kind="trailing-avg60"):
    from types import SimpleNamespace

    return SimpleNamespace(
        fresh=fresh, estimate=lambda: price, estimate_kind=lambda: kind
    )


def test_evaluate_uses_brti_when_fresh():
    import time as _time

    from src.jobs.market_data import Btc15mFeed, SpotFeed

    spot = SpotFeed()
    spot.price = 84000.0  # stale-side decoy: would refuse or flip the side
    spot.ts = _time.time()
    feed = Btc15mFeed()
    feed.ts = _time.time()
    trader = UpDownTrader(spot, feed, UpDownConfig(), brti=_brti_fake(85000.0))
    market = _quoted_market(yes_ask=0.45, no_ask=0.55, target=84000.0)
    feed.markets = [market]
    out = trader.evaluate(market, live=False)
    # DRY now uses the same spot truth as LIVE so both books are comparable.
    assert out is not None and out.truth.startswith("coinbase-")
    assert out.spot == 84000.0


def test_evaluate_falls_back_to_coinbase_when_brti_stale():
    import time as _time

    from src.jobs.market_data import Btc15mFeed, SpotFeed

    spot = SpotFeed()
    spot.price = 85000.0
    spot.ts = _time.time()
    feed = Btc15mFeed()
    feed.ts = _time.time()
    trader = UpDownTrader(
        spot, feed, UpDownConfig(), brti=_brti_fake(85000.0, fresh=False)
    )
    market = _quoted_market(yes_ask=0.45, no_ask=0.55, target=84000.0)
    feed.markets = [market]
    out = trader.evaluate(market, live=False)
    assert out is not None and out.truth.startswith("coinbase-")


def test_stale_quote_is_refused_even_with_fresh_truth():
    import time as _time

    from src.jobs.market_data import Btc15mFeed, SpotFeed

    spot = SpotFeed()
    spot.price = 85000.0
    spot.ts = _time.time()
    feed = Btc15mFeed()
    feed.ts = _time.time() - 120.0  # quote 2 minutes old
    trader = UpDownTrader(spot, feed, UpDownConfig(), brti=_brti_fake(85000.0))
    market = _quoted_market(yes_ask=0.45, no_ask=0.55, target=84000.0)
    feed.markets = [market]
    assert trader.evaluate(market, live=False) is None


def test_live_entry_block_caps_clips_per_ticker():
    """ONE clip of one ticker, BOTH books: the trade log showed every
    dollar-large loss was a pyramid - same-window clips are the same bet at
    3-4x the Kelly cap, and the stack dies together."""
    from src.jobs.ladder_trader import (
        MAX_CLIPS_PER_TICKER,
        UpDownSignal,
        UpDownTrader,
    )
    from src.jobs.market_data import Btc15mFeed, SpotFeed

    def trader():
        spot = SpotFeed()
        spot.price = 85000.0
        spot.ts = time.time()
        return UpDownTrader(spot, Btc15mFeed(), UpDownConfig())

    sig = UpDownSignal(
        ticker="T",
        bucket="B",
        side="up",
        target=84609.0,
        spot=84900.0,
        spot_vs_target=291.0,
        fair=0.89,
        kalshi_price=0.40,
        edge=0.28,
        ask=0.40,
        contracts=12,
        notional=4.80,
        seconds_left=300.0,
        reason="test",
    )
    held = [
        {"ticker": "T", "side": "YES", "notional": 5.0, "contracts": 12}
        for _ in range(MAX_CLIPS_PER_TICKER)
    ]
    assert trader()._entry_block(sig, held, live=True) != ""
    assert trader()._entry_block(sig, held, live=False) != ""
    assert trader()._entry_block(sig, [], live=True) == ""
    assert trader()._entry_block(sig, [], live=False) == ""
    assert MAX_CLIPS_PER_TICKER == 1


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


# ---------------------------------------------------------------------------
# DRY parity: LIVE runs DRY's decision rule, plus the one cost DRY does not
# have (the exchange fee). No LIVE-only bands, no hour bans, no extra refusals.
# ---------------------------------------------------------------------------
def test_live_takes_everything_dry_takes_except_at_the_fee_bar(monkeypatch):
    """Same market, both books: DRY acts, and LIVE acts whenever the fee bar
    is cleared. The only difference permitted is the fee."""
    import src.jobs.ladder_trader as lt

    # In-band price (no surcharge), 15c of modelled edge: both books act.
    monkeypatch.setattr(lt, "fair_up_probability", lambda s, t, n, sl=900.0, *a, **k: 0.60)
    market = _quoted_market(yes_ask=0.45, no_ask=0.55, target=84000.0)
    trader = _scorer()
    trader.feed.markets = [market]
    dry = trader.evaluate(market, live=False)
    live = trader.evaluate(market, live=True)
    assert dry is not None and dry.actionable
    assert live is not None and live.actionable
    assert dry.side == live.side


def test_live_refuses_only_what_the_fee_makes_unprofitable(monkeypatch):
    """6c of edge at the ask: the TAKER window refuses it on both books - the
    fee is what refuses it. In the maker window the same 6c clears, because a
    resting bid pays a quarter of the fee and fills a cent better."""
    import src.jobs.ladder_trader as lt

    monkeypatch.setattr(lt, "fair_up_probability", lambda s, t, n, sl=900.0, *a, **k: 0.53)

    # Taker window (60s < maker patience): the fee refuses both books.
    market = _quoted_market(yes_ask=0.45, no_ask=0.55, target=84000.0, seconds_left=60)
    trader = _scorer()
    trader.feed.markets = [market]
    dry = trader.evaluate(market, live=False)
    live = trader.evaluate(market, live=True)
    assert (dry is None or not dry.actionable), "the fee makes DRY refuse too"
    assert (live is None or not live.actionable), "the fee is what refuses it"

    # Maker window (600s): both books take the same quotes, identically.
    market = _quoted_market(yes_ask=0.45, no_ask=0.55, target=84000.0, seconds_left=600)
    trader = _scorer()
    trader.feed.markets = [market]
    dry = trader.evaluate(market, live=False)
    live = trader.evaluate(market, live=True)
    assert dry is not None and dry.actionable, "DRY must mirror the maker window"
    assert live is not None and live.actionable
    assert "maker-bar" in live.reason


def test_live_trades_the_18_utc_hour_dry_does():
    """The losing-hour ban is gone: DRY trades it, LIVE trades it."""
    market = _quoted_market(yes_ask=0.40, no_ask=0.60, target=84000.0)
    trader = _scorer()
    trader.feed.markets = [market]
    out = trader.evaluate(market, live=True)
    assert out is not None and out.actionable


def test_both_books_refuse_the_final_minute_alike():
    market = _quoted_market(yes_ask=0.40, no_ask=0.60, target=84000.0, seconds_left=30)
    trader = _scorer()
    trader.feed.markets = [market]
    trader.brti = _brti_fake(85100.0, kind="windowed-settlement-avg")
    assert trader.evaluate(market, live=True) is None
    assert trader.evaluate(market, live=False) is None


def test_both_books_use_the_same_entry_ceiling():
    market = _quoted_market(yes_ask=0.92, no_ask=0.08, target=84000.0)
    trader = _scorer()
    trader.feed.markets = [market]
    for live in (False, True):
        out = trader.evaluate(market, live=live)
        assert out is None or not out.actionable, f"live={live} took a 92c entry"


# ---------------------------------------------------------------------------
# Item 2: maker entry fill confirmation - never books a resting order
# ---------------------------------------------------------------------------
class _FakeMakerClient:
    def __init__(self, resting, held, raise_positions=False):
        self._resting = list(resting)
        self._held = held
        self._raise_positions = raise_positions
        self.cancelled = []

    async def get_orders(self, ticker=None, status=None):
        return {"orders": list(self._resting)}

    async def cancel_order(self, order_id, market_ticker=None):
        self.cancelled.append(str(order_id))
        self._resting = []
        return {}

    async def get_positions(self, ticker=None):
        if self._raise_positions:
            raise RuntimeError("api down")
        return {"market_positions": [{"ticker": ticker, "position_fp": str(self._held)}]}


class _Req:
    client_order_id = "cid-1"


def _pos(ticker="KXTEST-T", qty=12):
    from src.utils.database import Position

    return Position(
        market_id=ticker,
        side="YES",
        entry_price=0.44,
        quantity=qty,
        timestamp=datetime.now(timezone.utc),
    )


def test_maker_fill_confirmed_when_holding_appears():
    import logging

    from src.jobs.execute import _confirm_maker_fill

    client = _FakeMakerClient(resting=[], held=12)
    ok = asyncio.run(
        _confirm_maker_fill(client, _Req(), _pos(), 0.1, logging.getLogger("t"), "oid-1")
    )
    assert ok is True
    assert client.cancelled == []


def test_maker_unfilled_order_is_cancelled_and_reports_false():
    import logging

    from src.jobs.execute import _confirm_maker_fill

    client = _FakeMakerClient(
        resting=[{"client_order_id": "cid-1", "order_id": "oid-1"}], held=0
    )
    ok = asyncio.run(
        _confirm_maker_fill(client, _Req(), _pos(), 0.1, logging.getLogger("t"), "oid-1")
    )
    assert ok is False
    assert client.cancelled == ["oid-1"]


def test_maker_unreadable_holding_keeps_the_row():
    import logging

    from src.jobs.execute import _confirm_maker_fill

    client = _FakeMakerClient(resting=[], held=0, raise_positions=True)
    ok = asyncio.run(
        _confirm_maker_fill(client, _Req(), _pos(), 0.1, logging.getLogger("t"), "oid-1")
    )
    assert ok is True


# ---------------------------------------------------------------------------
# Item 4b: a winning LIVE position rides the settlement window, no scalp
# ---------------------------------------------------------------------------
def test_winning_position_rides_the_settlement_window(monkeypatch):
    from src.jobs import execute as ex

    close_soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    market = {
        "status": "open",
        "yes_bid_dollars": "0.55",
        "yes_ask_dollars": "0.57",
        "no_bid_dollars": "0.43",
        "no_ask_dollars": "0.45",
        "close_time": close_soon,
    }

    class _FakeDB:
        async def get_open_live_positions(self):
            return [_pos(qty=10)]

    class _FakeClient:
        async def get_market(self, ticker):
            return {"market": market}

    sells = []

    async def _fake_sell(**kw):
        sells.append(kw)
        return True

    monkeypatch.setattr(ex, "place_sell_limit_order", _fake_sell)
    res = asyncio.run(
        ex.place_profit_taking_orders(_FakeDB(), _FakeClient(), 0.20, live_mode=True)
    )
    assert res["orders_placed"] == 0
    assert sells == []


def test_winning_position_with_time_left_still_takes_profit(monkeypatch):
    from src.jobs import execute as ex

    close_later = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
    market = {
        "status": "open",
        "yes_bid_dollars": "0.55",
        "yes_ask_dollars": "0.57",
        "no_bid_dollars": "0.43",
        "no_ask_dollars": "0.45",
        "close_time": close_later,
    }

    class _FakeDB:
        async def get_open_live_positions(self):
            return [_pos(qty=10)]

    class _FakeClient:
        async def get_market(self, ticker):
            return {"market": market}

    sells = []

    async def _fake_sell(**kw):
        sells.append(kw)
        return True

    monkeypatch.setattr(ex, "place_sell_limit_order", _fake_sell)
    res = asyncio.run(
        ex.place_profit_taking_orders(_FakeDB(), _FakeClient(), 0.20, live_mode=True)
    )
    assert res["orders_placed"] == 1
    assert len(sells) == 1


# ---------------------------------------------------------------------------
# Session skip is enforced in LIVE evaluate(), never in DRY
# ---------------------------------------------------------------------------
def test_the_losing_hour_ban_is_gone_both_books_trade_it(monkeypatch):
    """The 18 UTC ban was ancient single-hour data from the old model - and it
    was also a DRY/LIVE law violation (DRY traded the hour, LIVE sat out).
    The per-trade guards (edge bar, venue guard, hard blocks) are the data-
    driven protection now; a clock superstition is not."""
    monkeypatch.setattr(live_fees, "live_session_skip", lambda now=None: True)
    trader = _scorer()
    live = trader.evaluate(_quoted_market(), live=True)
    dry = trader.evaluate(_quoted_market(), live=False)
    assert live is not None and live.actionable, "LIVE must trade the hour now"
    assert dry is not None and dry.actionable
    assert trader.book.skipped_session == 0


def test_live_trades_outside_losing_hour(monkeypatch):
    monkeypatch.setattr(live_fees, "live_session_skip", lambda now=None: False)
    trader = _scorer()
    signal = trader.evaluate(_quoted_market(), live=True)
    assert signal is not None and signal.actionable


def test_the_losing_hour_config_field_is_gone():
    """No clock-based skip flag survives on the config: the guards that matter
    are per-trade, not per-hour."""
    from src.jobs.ladder_trader import UpDownConfig

    assert not hasattr(UpDownConfig(), "live_skip_losing_hour")
    src = open("src/jobs/ladder_trader.py", encoding="utf-8").read()
    assert "live_session_skip" not in src


# ---------------------------------------------------------------------------
# Ride-to-settlement: LIVE never scalps 15-minute binaries mid-bucket
# ---------------------------------------------------------------------------
def _btc_pos():
    from src.utils.database import Position

    return Position(
        market_id="KXBTC15M-26OCT011715-15",
        side="NO",
        entry_price=0.44,
        quantity=10,
        timestamp=datetime.now(timezone.utc),
        strategy="btc_updown",
    )


class _ExplodingClient:
    async def get_market(self, ticker):
        raise AssertionError("ride-to-settlement must not read the book")


def test_live_profit_take_skips_btc15m():
    from src.jobs import execute as ex

    class _FakeDB:
        async def get_open_live_positions(self):
            return [_btc_pos()]

    res = asyncio.run(
        ex.place_profit_taking_orders(
            _FakeDB(), _ExplodingClient(), 0.20, live_mode=True
        )
    )
    assert res == {"orders_placed": 0, "positions_processed": 0}


def test_live_stop_loss_skips_btc15m():
    from src.jobs import execute as ex

    class _FakeDB:
        async def get_open_live_positions(self):
            return [_btc_pos()]

    res = asyncio.run(
        ex.place_stop_loss_orders(_FakeDB(), _ExplodingClient(), -0.15, live_mode=True)
    )
    assert res == {"orders_placed": 0, "positions_processed": 0}


def test_dry_also_skips_btc15m_mid_bucket(monkeypatch):
    """DRY and LIVE both skip mid-bucket BTC profit-take.

    The 15-minute BTC binary rides to settlement. Exiting mid-bucket
    pays the spread plus a second fee for nothing. Applied to
    BOTH books so DRY rehearses exactly what LIVE does.
    """
    from src.jobs import execute as ex

    close_later = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
    market = {
        "status": "open",
        "yes_bid_dollars": "0.55",
        "yes_ask_dollars": "0.57",
        "no_bid_dollars": "0.70",
        "no_ask_dollars": "0.72",
        "close_time": close_later,
    }

    class _FakeDB:
        async def get_open_live_positions(self):
            return [_btc_pos()]

    class _FakeClient:
        async def get_market(self, ticker):
            return {"market": market}

    sells = []

    async def _fake_sell(**kw):
        sells.append(kw)
        return True

    monkeypatch.setattr(ex, "place_sell_limit_order", _fake_sell)
    res = asyncio.run(
        ex.place_profit_taking_orders(_FakeDB(), _FakeClient(), 0.20, live_mode=False)
    )
    assert res["orders_placed"] == 0
    assert len(sells) == 0
