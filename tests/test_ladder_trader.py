"""Tests for the BTC 15-minute up/down trader and its market-data layer.

The contract's shape is the thing most easily got wrong: the horizon is a ticker
suffix, the quotes live in `*_dollars` string fields while the integer-cent
fields are null, and settlement is a 60-second average of CF Benchmarks BRTI.
Each of those was a silent "no quote" before.
"""
import time
from datetime import datetime

import pytest

from src.jobs.ladder_trader import UpDownConfig, UpDownSignal, UpDownTrader, fair_up_probability
from src.jobs.market_data import Btc15mFeed, SpotFeed, UpDownMarket
from src.utils.database import Position


def _signal(ticker, *, side="up", ask=0.5, contracts=8, seconds_left=300.0):
    """A tradable clip, as the scorer would emit one."""
    return UpDownSignal(
        ticker=ticker,
        bucket="26OCT011715",
        side=side,
        target=84609.0,
        spot=84900.0,
        spot_vs_target=291.0,
        fair=0.89,
        kalshi_price=ask,
        edge=0.28,
        ask=ask,
        contracts=contracts,
        notional=round(ask * contracts, 4),
        seconds_left=seconds_left,
        reason="test",
    )


def _trader(db):
    spot = SpotFeed()
    spot.price = 84900.0
    spot.ts = time.time()
    spot.source = "test"
    return UpDownTrader(spot, Btc15mFeed(), UpDownConfig())


# ---------------------------------------------------------------------------
# Ticker shape
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ticker,expected",
    [
        ("KXBTC15M-26OCT011715-15", ("KXBTC15M-26OCT011715", "26OCT011715", 15)),
        ("KXBTC15M-26OCT011715-30", ("KXBTC15M-26OCT011715", "26OCT011715", 30)),
        ("KXBTC-26OCT0117-T73250", (None, None, 0)),
        # An event ticker has no horizon suffix, so it is not a market.
        ("KXBTC15M-26OCT011715", (None, None, 0)),
        ("garbage", (None, None, 0)),
    ],
)
def test_ticker_parsing(ticker, expected):
    assert Btc15mFeed._parse(ticker) == expected


def test_bucket_is_a_quarter_hour_stamp():
    _, bucket, horizon = Btc15mFeed._parse("KXBTC15M-26OCT011715-15")
    assert bucket == "26OCT011715"
    assert horizon == 15
    # YY MON DD HH MM - the minute field is the last two characters.
    assert bucket[9:11] in ("00", "15", "30", "45"), "bucket must be a 15-minute step"


def _dollars(**kw):
    """A market quoted the way this series actually quotes."""
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


def test_tradable_requires_both_sides_quoted():
    assert _dollars(yes_bid=0.52, yes_ask=0.53, no_bid=0.47, no_ask=0.48).tradable
    assert not _dollars(yes_bid=None, yes_ask=None, no_bid=0.47, no_ask=0.48).tradable


def test_nearest_skips_unquoted_contracts():
    feed = Btc15mFeed()
    feed.markets = [
        # Closes sooner but has no quotes at all.
        _dollars(
            ticker="unquoted", seconds_left=60, yes_bid=None, yes_ask=None, no_bid=None, no_ask=None
        ),
        _dollars(
            ticker="quoted", seconds_left=600, yes_bid=0.52, yes_ask=0.53, no_bid=0.47, no_ask=0.48
        ),
    ]
    assert feed.nearest().ticker == "quoted"


# ---------------------------------------------------------------------------
# The fair-value rule
# ---------------------------------------------------------------------------
def test_fair_probability_is_half_at_the_target():
    assert fair_up_probability(84609.34, 84609.34) == 0.5


def test_fair_probability_rises_above_the_target_and_falls_below():
    assert fair_up_probability(84800, 84609.34) > 0.9
    assert fair_up_probability(84400, 84609.34) < 0.1


def test_noise_band_is_respected():
    """Inside the deadband, spot is treated as no information at all."""
    c = UpDownConfig(noise_usd=15.0, min_edge=0.06)
    trader = UpDownTrader(SpotFeed(), Btc15mFeed(), c)
    # $10 above target with Kalshi at an even 0.50/0.50: fair is barely above
    # 0.5, so there is no edge and nothing should be traded.
    spot = SpotFeed()
    spot.price = 84619.34
    spot.ts = time.time()
    trader.spot = spot
    market = _dollars(yes_bid=0.49, yes_ask=0.51, no_bid=0.49, no_ask=0.51, target=84609.34)
    signal = trader.evaluate(market)
    assert signal is not None
    assert signal.side == ""
    assert not signal.actionable


def test_buys_up_when_spot_is_well_past_target_but_kalshi_is_even():
    c = UpDownConfig(noise_usd=15.0, min_edge=0.06)
    trader = UpDownTrader(SpotFeed(), Btc15mFeed(), c)
    spot = SpotFeed()
    spot.price = 84900.0
    spot.ts = time.time()
    trader.spot = spot
    market = _dollars(yes_bid=0.49, yes_ask=0.51, no_bid=0.49, no_ask=0.51, target=84609.34)
    signal = trader.evaluate(market)
    assert signal.side == "up"
    assert signal.edge >= 0.06
    assert signal.actionable


def test_buys_down_when_spot_is_below_target_but_kalshi_is_even():
    c = UpDownConfig(noise_usd=15.0, min_edge=0.06)
    trader = UpDownTrader(SpotFeed(), Btc15mFeed(), c)
    spot = SpotFeed()
    spot.price = 84300.0
    spot.ts = time.time()
    trader.spot = spot
    market = _dollars(yes_bid=0.49, yes_ask=0.51, no_bid=0.49, no_ask=0.51, target=84609.34)
    signal = trader.evaluate(market)
    assert signal.side == "down"
    # Edge is always "worth minus price" on the side being bought, so a good
    # trade is positive on either side.
    assert signal.edge >= 0.06
    assert signal.actionable


def test_no_trade_when_kalshi_already_agrees_with_spot():
    c = UpDownConfig(noise_usd=15.0, min_edge=0.06)
    trader = UpDownTrader(SpotFeed(), Btc15mFeed(), c)
    spot = SpotFeed()
    spot.price = 84900.0
    spot.ts = time.time()
    trader.spot = spot
    # Kalshi already charges 0.95 for UP, which is what the spot implies.
    market = _dollars(yes_bid=0.94, yes_ask=0.96, no_bid=0.04, no_ask=0.06, target=84609.34)
    signal = trader.evaluate(market)
    assert signal.side == ""
    assert trader.book.skipped_no_edge == 1


# ---------------------------------------------------------------------------
# Refusals: the conditions under which this strategy is wrong
# ---------------------------------------------------------------------------
def test_refuses_on_a_stale_spot_tick():
    c = UpDownConfig(max_spot_age=5.0)
    trader = UpDownTrader(SpotFeed(), Btc15mFeed(), c)
    spot = SpotFeed()
    spot.price = 84900.0
    spot.ts = time.time() - 30
    trader.spot = spot
    assert trader.evaluate(_dollars(yes_bid=0.49, yes_ask=0.51)) is None
    assert trader.book.skipped_stale == 1


def test_refuses_inside_the_settlement_window():
    """The final 60 seconds are the settlement window; spot leads nothing."""
    c = UpDownConfig(min_seconds_left=45.0)
    trader = UpDownTrader(SpotFeed(), Btc15mFeed(), c)
    spot = SpotFeed()
    spot.price = 84900.0
    spot.ts = time.time()
    trader.spot = spot
    market = _dollars(yes_bid=0.49, yes_ask=0.51, no_bid=0.49, no_ask=0.51, seconds_left=20)
    assert trader.evaluate(market) is None
    assert trader.book.skipped_too_close == 1


def test_refuses_when_the_contract_is_unquoted():
    trader = UpDownTrader(SpotFeed(), Btc15mFeed())
    spot = SpotFeed()
    spot.price = 84900.0
    spot.ts = time.time()
    trader.spot = spot
    market = _dollars(yes_bid=None, yes_ask=None, no_bid=None, no_ask=None)
    assert trader.evaluate(market) is None
    assert trader.book.skipped_unquoted == 1


def test_refuses_when_there_is_no_contract_at_all():
    trader = UpDownTrader(SpotFeed(), Btc15mFeed())
    assert trader.evaluate(None) is None


# ---------------------------------------------------------------------------
# Sizing and limits
# ---------------------------------------------------------------------------
def test_size_targets_five_dollars():
    trader = UpDownTrader(SpotFeed(), Btc15mFeed())
    assert trader._size(0.50) == 10
    assert trader._size(0.10) == 50
    assert trader._size(0.95) == 5


def test_size_never_returns_a_fractional_or_negative_count():
    trader = UpDownTrader(SpotFeed(), Btc15mFeed())
    for price in (0.0, None, -0.5, 1.5):
        n = trader._size(price)
        assert isinstance(n, int)
        assert n >= 0


def test_limits_are_hard_defaults():
    c = UpDownConfig()
    assert c.notional_usd == 5.0
    # The one-clip-per-market COUNT guard is gone: repeated clips of the same
    # contract are allowed while the model still favours it. What caps risk now
    # is money (total open notional) and probability (min_win_prob).
    assert not hasattr(c, "max_open_positions")
    assert c.max_open_notional == 25.0
    assert c.min_win_prob == 0.60


def test_book_summary_reports_the_limits_it_enforces():
    trader = UpDownTrader(SpotFeed(), Btc15mFeed())
    trader.book.open_positions = [{"notional": 5.0}, {"notional": 5.0}]
    s = trader.book.summary()
    assert s["open_positions"] == 2
    assert s["open_notional"] == 10.0
    assert s["max_open_notional"] == 25.0
    assert "max_open_positions" not in s


# ---------------------------------------------------------------------------
# Entry gating: probability and money, not clip counts
# ---------------------------------------------------------------------------
def test_a_second_clip_of_the_same_contract_is_not_blocked():
    trader = UpDownTrader(SpotFeed(), Btc15mFeed())
    held = [{"ticker": "KXBTC15M-26OCT011715-15", "side": "YES", "notional": 5.0}]
    second = _signal("KXBTC15M-26OCT011715-15", side="up", ask=0.5, contracts=8)
    assert trader._entry_block(second, held) == ""


def test_the_opposite_side_of_the_same_contract_is_still_blocked():
    trader = UpDownTrader(SpotFeed(), Btc15mFeed())
    held = [{"ticker": "KXBTC15M-26OCT011715-15", "side": "YES", "notional": 5.0}]
    down = _signal("KXBTC15M-26OCT011715-15", side="down", ask=0.5, contracts=8)
    assert "opposite side" in trader._entry_block(down, held)


def test_an_entry_under_min_win_prob_is_refused():
    trader = UpDownTrader(SpotFeed(), Btc15mFeed())
    # fair=0.89 for up -> win_prob 0.89 passes; a DOWN clip on fair 0.89 has
    # win_prob 0.11, which is below min_win_prob 0.60.
    down = _signal("KXBTC15M-26OCT011715-15", side="down", ask=0.5, contracts=8)
    blocked = trader._entry_block(down, [])
    assert "win probability" in blocked
    assert trader.book.skipped_low_prob == 1


def test_the_notional_cap_stops_accumulation():
    trader = UpDownTrader(SpotFeed(), Btc15mFeed())
    held = [
        {"ticker": "KXBTC15M-26OCT011715-15", "side": "YES", "notional": 12.0},
        {"ticker": "KXBTC15M-26OCT011715-15", "side": "YES", "notional": 12.0},
    ]
    clip = _signal("KXBTC15M-26OCT011715-15", side="up", ask=0.5, contracts=8)
    blocked = trader._entry_block(clip, held)
    assert "exceed" in blocked and "$25.00" in blocked


def test_under_the_notional_cap_repeated_clips_pass():
    trader = UpDownTrader(SpotFeed(), Btc15mFeed())
    held = [{"ticker": "KXBTC15M-26OCT011715-15", "side": "YES", "notional": 5.0}]
    clip = _signal("KXBTC15M-26OCT011715-15", side="up", ask=0.5, contracts=8)
    assert trader._entry_block(clip, held) == ""


# ---------------------------------------------------------------------------
# LIVE pays fees, so the edge bar is higher there
# ---------------------------------------------------------------------------
def _quoted_market(**kw):
    return _dollars(
        yes_bid=kw.get("yes_bid", 0.45),
        yes_ask=kw.get("yes_ask", 0.46),
        no_bid=kw.get("no_bid", 0.54),
        no_ask=kw.get("no_ask", 0.55),
        target=kw.get("target", 84609.34),
    )


def _scorer(spot_price=84900.0):
    spot = SpotFeed()
    spot.price = spot_price
    spot.ts = time.time()
    spot.source = "test"
    return UpDownTrader(spot, Btc15mFeed(), UpDownConfig())


def test_live_requires_the_fee_on_top_of_the_edge(monkeypatch):
    """A 6c edge clears DRY but not LIVE once the fee is taken out."""
    import src.jobs.ladder_trader as lt

    monkeypatch.setattr(lt, "fair_up_probability", lambda s, t, n: 0.53)
    trader = _scorer()
    market = _quoted_market(yes_ask=0.45, no_ask=0.55, target=84000.0)
    dry = trader.evaluate(market, live=False)
    assert dry is not None and dry.actionable, "DRY takes the 8c in-band edge"
    trader.book.skipped_no_edge = 0
    live = trader.evaluate(market, live=True)
    assert (live is None or not live.actionable), "LIVE must refuse an edge under fee+min"


def test_live_takes_an_edge_that_clears_fee_and_minimum():
    trader = _scorer()
    trader.spot.price = 85000.0
    # In the LIVE 0.10-0.50 entry band: the fee-aware bar is the gate here.
    market = _quoted_market(yes_ask=0.40, no_ask=0.60, target=84000.0)
    live = trader.evaluate(market, live=True)
    assert live is not None and live.actionable
    assert live.edge >= trader.config.min_edge + trader.config.live_fee_rate


# ---------------------------------------------------------------------------
# Standard tuning: the recommendations from the forever log, wired in
# ---------------------------------------------------------------------------
def test_entries_at_or_above_ninety_cents_are_hard_blocked():
    """The log: $0.90+ entries won 4% of the time. Never take them."""
    trader = _scorer()
    trader.spot.price = 85000.0
    market = _quoted_market(yes_ask=0.92, no_ask=0.08, target=84000.0)
    out = trader.evaluate(market, live=False)
    assert out is None or not out.actionable
    if out is not None:
        assert "blocked $0.90+" in (out.reason or "")


def test_out_of_band_entries_need_extra_edge(monkeypatch):
    """The sweet band is $0.25-$0.50; outside it the bar is higher."""
    import src.jobs.ladder_trader as lt

    # 6c of edge at a 0.55 fill: clears the base bar but not the band penalty.
    monkeypatch.setattr(lt, "fair_up_probability", lambda s, t, n: 0.61)
    trader = _scorer()
    market = _quoted_market(yes_ask=0.55, no_ask=0.45, target=84000.0)
    out = trader.evaluate(market, live=False)
    assert out is None or not out.actionable

    # 9c of edge at the same fill: clears base + band penalty.
    monkeypatch.setattr(lt, "fair_up_probability", lambda s, t, n: 0.64)
    trader.book.skipped_no_edge = 0
    out2 = trader.evaluate(market, live=False)
    assert out2 is not None and out2.actionable


def test_the_no_side_is_preferred_when_edges_are_close(monkeypatch):
    """NO wins 83% vs YES at 67% - prefer NO unless YES is clearly better."""
    import src.jobs.ladder_trader as lt

    # Tied edges: NO is chosen.
    monkeypatch.setattr(lt, "fair_up_probability", lambda s, t, n: 0.44)
    trader = _scorer()
    market = _quoted_market(yes_ask=0.38, no_ask=0.50, target=84000.0)
    out = trader.evaluate(market, live=False)
    assert out is not None and out.side == "down"

    # YES clearly better (by more than the override margin): YES is chosen.
    monkeypatch.setattr(lt, "fair_up_probability", lambda s, t, n: 0.44)
    trader.book.skipped_no_edge = 0
    market2 = _quoted_market(yes_ask=0.35, no_ask=0.50, target=84000.0)
    out2 = trader.evaluate(market2, live=False)
    assert out2 is not None and out2.side == "up"


def test_live_clip_sizes_against_the_balance_budget():
    trader = _scorer()
    trader.spot.price = 85000.0
    # In-band price: the LIVE entry band is 0.10-0.50.
    market = _quoted_market(yes_ask=0.40, no_ask=0.60, target=84000.0)
    # A $4 budget -> 10 contracts at 0.40, not the fixed $5 clip's 12.
    live = trader.evaluate(market, live=True, clip_usd=4.0)
    assert live is not None
    assert live.contracts == 10
    assert live.notional == pytest.approx(4.00, abs=0.01)


def test_a_zero_dollar_live_budget_produces_no_clip():
    trader = _scorer()
    trader.spot.price = 85000.0
    market = _quoted_market(yes_ask=0.85, no_ask=0.15, target=84000.0)
    live = trader.evaluate(market, live=True, clip_usd=0.0)
    assert not live.actionable


# ---------------------------------------------------------------------------
# The process must be immortal: nothing but Stop ends it
# ---------------------------------------------------------------------------
async def test_a_cycle_crash_never_kills_the_process(tmp_path, monkeypatch):
    """An exception in a cycle used to exit the process and wait for a respawn.

    Between the crash and the supervisor's next pass the card read "stopped",
    which is what "it never stays on" was. The loop now swallows the failure,
    keeps going, and still stamps the heartbeat that proves it is alive.
    """
    import os

    import src.jobs.ladder_trader as lt
    from src.utils.strategy_runtime import StrategyRuntime

    rt = __import__("src.utils.strategy_runtime", fromlist=["key"])

    db = str(tmp_path / "t.db")
    store = StrategyRuntime(db_path=db)
    await store.record_start("btc_updown", os.getpid(), "paper", "cli.py run --btc-updown")

    class _Hub:
        spot = None
        feed = None

        async def start(self):
            return None

        async def stop(self):
            return None

    calls = []

    class _BoomTrader:
        def __init__(self, *a, **k):
            pass

        def _db_path(self):
            return db

        async def cycle(self):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return {"ok": True}

    monkeypatch.setattr(lt, "UpDownTrader", _BoomTrader)
    monkeypatch.setattr("src.jobs.market_data.MarketDataHub", _Hub)

    # A single pass must survive a cycle crash and return normally.
    await lt.run_updown_trader(lt.UpDownConfig(), loop=False)

    assert calls == [1], "the crashed cycle must not have been retried into an infinite loop"
    row = (await store.snapshot())[rt.key("btc_updown", "paper")]
    assert row.get("heartbeat_at"), "a crashed pass must still stamp its heartbeat"


# ---------------------------------------------------------------------------
# The feed must not invent a lead
# ---------------------------------------------------------------------------
def test_payload_reports_the_real_contract_and_no_45_second_claim():
    from src.jobs.market_data import LeadEstimator, MarketDataHub

    hub = MarketDataHub()
    hub.spot.price = 84900.0
    hub.spot.ts = time.time()
    hub.spot.source = "test"
    hub.feed.markets = [
        _dollars(yes_bid=0.52, yes_ask=0.53, no_bid=0.47, no_ask=0.48, target=84609.34)
    ]
    hub.feed.refreshes = 3
    hub.lead = LeadEstimator()
    hub.lead.observe(hub.spot, hub.feed)
    payload = hub.chart_payload()

    contract = payload["kalshi"]["market"]
    assert contract["ticker"] == "KXBTC15M-26OCT011715-15"
    assert contract["up_ask"] == 0.53
    assert contract["down_ask"] == 0.48
    assert contract["target"] == 84609.34
    assert payload["lead"]["spot_vs_target"] == pytest.approx(290.66)
    assert "proxy" in payload["lead"]["note"]
    # The measured reading, not a hardcoded lead.
    assert "45" not in payload["lead"]["note"]


# ---------------------------------------------------------------------------
# A clip must be recorded before it is bought
# ---------------------------------------------------------------------------
async def test_clip_is_persisted_before_the_fill_is_sought(tmp_path, monkeypatch):
    """The DRY ledger must never be debited for a position that was never saved.

    This clip used to be handed to `execute_position` unsaved. The broker filled
    it and debited simulated cash, and only then did execute_position notice the
    missing id and bail. No position row existed, so the book still reported zero
    open positions, the max-open guard never tripped, and every cycle bought the
    same contract again - roughly $5 a pass, with nothing on the board.
    """
    from src.utils.database import DatabaseManager

    db = DatabaseManager(db_path=str(tmp_path / "t.db"))
    await db.initialize()

    trader = _trader(db)
    trader.db_manager = db
    trader._client = object()  # never used: the fill is refused up front

    submitted = []

    async def _never(position, live_mode, db_manager, kalshi_client, maker_wait_seconds=0.0):
        submitted.append(position.market_id)
        return True

    monkeypatch.setattr("src.jobs.execute.execute_position", _never)

    signal = _signal("KXBTC15M-26OCT011715-15", side="up", ask=0.5, contracts=8)
    assert await trader._place(signal, live=False) is True
    assert submitted == ["KXBTC15M-26OCT011715-15"]

    stored = await db.get_open_positions(mode="dry")
    assert len(stored) == 1
    assert stored[0].strategy == "btc_updown"


async def test_more_clips_of_the_same_contract_are_allowed_by_design(tmp_path, monkeypatch):
    """The one-clip-per-market guard is gone, at every layer.

    btc_updown is meant to pyramid: buy additional clips of the same contract
    while the model still gives it better than min_win_prob odds. That used to
    be impossible three different ways - the in-cycle `already holding` refusal,
    the `add_position` duplicate check, and a UNIQUE(market_id, side) constraint
    in the schema. All three are removed; what caps the book now is the win
    probability gate and the total-notional cap in `cycle`.
    """
    from src.utils.database import DatabaseManager

    db = DatabaseManager(db_path=str(tmp_path / "t.db"))
    await db.initialize()

    trader = _trader(db)
    trader.db_manager = db
    trader._client = object()

    fills = []

    async def _count(position, live_mode, db_manager, kalshi_client, maker_wait_seconds=0.0):
        fills.append(position.market_id)
        return True

    monkeypatch.setattr("src.jobs.execute.execute_position", _count)

    first = _signal("KXBTC15M-26OCT011715-15", side="up", ask=0.5, contracts=8)
    assert await trader._place(first, live=False) is True

    second = _signal("KXBTC15M-26OCT011715-15", side="up", ask=0.5, contracts=8)
    assert await trader._place(second, live=False) is True
    assert len(fills) == 2, "the second clip of the same contract was refused"

    stored = await db.get_open_positions(mode="dry")
    assert len(stored) == 2
    assert [p.market_id for p in stored] == ["KXBTC15M-26OCT011715-15"] * 2


async def test_execute_refuses_a_position_that_was_never_saved():
    """No caller can spend money - simulated or real - on an unrecordable fill."""
    from src.jobs.execute import execute_position

    class _ExplodingBroker:
        async def available_cents(self):
            raise AssertionError("must not reach the broker with an unsaved position")

    position = Position(
        market_id="KXBTC15M-26OCT011715-15",
        side="YES",
        entry_price=0.5,
        quantity=8,
        timestamp=datetime.now(),
        rationale="unsaved",
        confidence=0.8,
        live=False,
        strategy="btc_updown",
        mode="dry",
    )
    assert position.id is None
    assert await execute_position(position, False, None, None) is False
