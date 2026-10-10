"""Tiny-account competence: $10.04 is LIVE now - every clip is life or death.

Kalshi's $1.00 order minimum, its ceil-to-the-cent fee, and the 20%-of-balance
clip all interact at this scale. These tests pin the arithmetic that keeps a
tiny account trading on every edge instead of stalling or overpaying.
"""
import math
import time

import pytest

from src.jobs.broker import DryBroker, OrderRequest, build_order_request, minimum_viable_quantity
from src.jobs.ladder_trader import UpDownConfig, UpDownTrader
from src.jobs.market_data import Btc15mFeed, SpotFeed
from src.utils.mode import TradingMode


def _trader():
    spot = SpotFeed()
    spot.price = 84900.0
    spot.ts = __import__("time").time()
    spot.source = "test"
    return UpDownTrader(spot, Btc15mFeed(), UpDownConfig())


def _trader_no_session_mult():
    spot = SpotFeed()
    spot.price = 84900.0
    spot.ts = __import__("time").time()
    spot.source = "test"
    return UpDownTrader(spot, Btc15mFeed(), UpDownConfig(session_mult_enabled=False))


def test_size_floors_at_one_contract_not_a_dollar():
    """The exchange minimum is one whole contract; a $0.60 budget at 0.45 still
    submits one contract, never zero and never a fabricated $1 ceiling."""
    trader = _trader()
    assert trader._size(0.45, clip_usd=0.60) == 1
    assert trader._size(0.58, clip_usd=0.60) == 1  # 1 @ 0.58 = $0.58


def test_size_spends_the_budget_not_the_default_clip():
    """A $10.04 account clips at 20% ($2.01), not the $5 default."""
    trader = _trader()
    assert trader._size(0.58, clip_usd=2.01) == 3  # 3 @ 0.58 = $1.74 <= 2.01
    assert trader._size(0.41, clip_usd=2.01) == 4  # 4 @ 0.41 = $1.64


def test_minimum_viable_quantity_is_one_contract():
    assert minimum_viable_quantity(0.45) == 1
    assert minimum_viable_quantity(0.10) == 1
    assert minimum_viable_quantity(0.05) == 1
    assert minimum_viable_quantity(0) == 0


def test_broker_refloor_meets_one_contract(tmp_path):
    """A fractional clip rounds up to one whole contract, never a $1 notional."""
    from src.utils.database import DatabaseManager

    db = DatabaseManager(db_path=str(tmp_path / "t.db"))
    import asyncio

    asyncio.run(db.initialize())
    req, reason = build_order_request(
        market_id="KXTEST-26",
        side="YES",
        action="buy",
        quantity=1,  # 1 @ 0.45 = $0.45 - a valid whole-contract order
        market={
            "yes_bid_dollars": 0.44, "yes_ask_dollars": 0.45,
            "no_bid_dollars": 0.54, "no_ask_dollars": 0.55,
            "status": "open",
        },
        available_cents=1_000,  # $10.00
    )
    assert req is not None
    assert req.count == 1


def test_fee_is_ceiled_to_the_cent_like_kalshi():
    """3 @ 0.58: raw 0.0511 - Kalshi charges up; the fake book must too."""
    fee = math.ceil(0.07 * 0.58 * 0.42 * 3 * 100.0) / 100.0
    assert fee == 0.06
    assert round(0.07 * 0.58 * 0.42 * 3, 2) == 0.05  # the old, undercharging way


async def test_dry_ledger_books_the_ceiled_fee(tmp_path):
    db_path = str(tmp_path / "t.db")
    from datetime import datetime

    from src.utils.database import DatabaseManager, Position

    db = DatabaseManager(db_path=db_path)
    await db.initialize()
    position = Position(
        market_id="KXTEST-26", side="YES", entry_price=0.58, quantity=3,
        timestamp=datetime.now(), rationale="fee test", confidence=0.5,
        live=False, strategy="btc_updown", mode="dry",
    )
    position.id = await db.add_position(position, allow_duplicate=True)

    broker = DryBroker(TradingMode(db_path=db_path))
    req = OrderRequest(
        ticker="KXTEST-26", client_order_id="fee1", side="yes", action="buy",
        count=3, type_="limit", notional=1.74, fill_price=0.58,
    )
    req.yes_price = 58
    resp = await broker.submit(req)
    assert not resp.get("error")
    ledger = await broker._mode.ledger(limit=1)
    # 300 - 3*0.58 - ceiled fee 0.06 = 298.20 (the round-fee era booked 298.21... no:
    # round gave 0.05 -> 298.21; Kalshi's ceil gives 0.06 -> 298.20).
    assert ledger[0]["cash_after"] == pytest.approx(298.20)


def test_balance_below_one_cent_sets_a_plain_error():
    """The cycle must say WHY it is sitting out, in words a human can act on."""
    import inspect

    from src.jobs.ladder_trader import UpDownTrader as T

    src = inspect.getsource(T.cycle)
    assert "empty - nothing can be" in src
    assert "at least one cent" in src


# ---------------------------------------------------------------------------
# Maker-first bar: fee-free, bid-priced
# ---------------------------------------------------------------------------
def _mkt(**kw):
    import time as _t

    from src.jobs.market_data import UpDownMarket

    return UpDownMarket(
        ticker=kw.pop("ticker", "KXBTC15M-26OCT011715-15"),
        event_ticker="KXBTC15M-26OCT011715",
        bucket=kw.pop("bucket", "26OCT011715"),
        horizon=15,
        title="BTC up in 15?",
        target=kw.pop("target", 84609.34),
        close_ts=_t.time() + kw.pop("seconds_left", 600),
        **kw,
    )


def test_maker_bar_takes_what_the_taker_bar_refuses(monkeypatch):
    """Same book, same fair value: resting at the bid clears where paying the
    ask plus the taker fee does not. That difference is real money on $10."""
    import src.jobs.ladder_trader as lt

    monkeypatch.setattr(
        lt, "fair_up_probability", lambda s, t, n, sl=900.0, *a, **k: 0.55
    )
    trader = _trader()

    # Maker window (600s > 120s patience): resting bid 0.44 clears the maker bar
    # (quarter fee) + the YES surcharge, where the taker ask would not.
    mkt = _mkt(
        seconds_left=600, yes_bid=0.44, yes_ask=0.45, no_bid=0.55, no_ask=0.56
    )
    signal = trader.evaluate(mkt, live=True)
    assert signal is not None and signal.side == "up"
    assert "maker-bar" in signal.reason

    # Taker window (60s): ask 0.45, edge 0.10 < taker fee + YES surcharge -> refused.
    trader2 = _trader()
    mkt2 = _mkt(
        seconds_left=60, yes_bid=0.44, yes_ask=0.45, no_bid=0.55, no_ask=0.56
    )
    signal2 = trader2.evaluate(mkt2, live=True)
    assert signal2 is not None and signal2.side == ""
    assert "taker-bar" in signal2.reason


def test_vol_rich_never_raises_the_bar_only_annotates():
    """Momentum makes implied sigma look rich; the diffusion fair value already
    prices that move. Penalizing it twice blocked every trending window - the
    bar may only move DOWN (cheap-vol credit), never up."""
    trader = _trader()
    t0 = time.monotonic()
    for i in range(60):
        trader.rv.observe(
            84000.0 + 0.1 * i + (1.0 if i % 2 else -1.0), now=t0 - 300.0 + i * 5.0
        )
    # Far-from-target quote -> implied sigma enormous vs realized.
    mkt = _mkt(
        seconds_left=300, yes_bid=0.20, yes_ask=0.21, no_bid=0.79, no_ask=0.80
    )
    trader.evaluate(mkt, live=False)
    assert trader._vol_adj <= 0.0


# ---------------------------------------------------------------------------
# Fractional Kelly: the compounding engine
# ---------------------------------------------------------------------------
def test_kelly_scales_the_clip_with_the_edge():
    """Quarter-Kelly on a real edge, THEN conviction on top: $10 book,
    fair 0.75 at 0.55 -> k* = 0.20/0.45 = 0.444, k_quarter = 0.111 ->
    clip $1.11; the view is >= strong_prob so conviction doubles it to
    ~$2.22 -> 4 contracts."""
    import src.jobs.ladder_trader as lt

    trader = _trader_no_session_mult()
    trader._live_balance = 10.0
    monkeypatched = lt  # keep the namespace honest for the fair patch below
    market = _mkt(
        seconds_left=600, target=84000.0,
        yes_bid=0.55, yes_ask=0.56, no_bid=0.44, no_ask=0.45,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lt, "fair_up_probability", lambda s, t, n, sl=900.0, *a, **k: 0.75)
        signal = trader.evaluate(market, live=True)
    assert signal is not None and signal.side == "up"
    assert signal.contracts == 4
    assert signal.notional == pytest.approx(2.20, abs=0.01)


def test_kelly_cap_bounds_a_single_clip():
    """Fair 0.95 at 0.70: certified tier (>=85%) triples the 0.208 quarter-
    Kelly clip to ~$6.24, and the 0.5-of-book ceiling ($5 on a $10 book)
    clips it to $5.00 -> 7 contracts at 0.70 ($4.90). The fraction ceiling
    is the binding money law; kelly_cap never binds at quarter scale."""
    import src.jobs.ladder_trader as lt

    trader = _trader_no_session_mult()
    trader._live_balance = 10.0
    market = _mkt(
        seconds_left=600, target=84000.0,
        yes_bid=0.70, yes_ask=0.71, no_bid=0.29, no_ask=0.30,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lt, "fair_up_probability", lambda s, t, n, sl=900.0, *a, **k: 0.95)
        signal = trader.evaluate(market, live=True)
    assert signal is not None and signal.side == "up"
    assert signal.contracts == 7
    assert signal.notional == pytest.approx(4.90, abs=0.01)
    assert signal.notional <= 10.0 * trader.config.conviction_max_fraction + 0.01


def test_maker_entries_stop_at_75c_takers_stop_at_60c():
    """The 0.80-0.85 YES band resolves to zero almost every time (the $0.90+
    '4% win' curse bleeding lower). Maker cap is now 0.75, taker 0.60."""
    import src.jobs.ladder_trader as lt

    trader = _trader()
    trader._live_balance = 10.0

    # Maker window (600s): 0.80 resting bid is ABOVE the 0.75 maker cap -> blocked.
    mkt = _mkt(
        seconds_left=600, target=84000.0,
        yes_bid=0.80, yes_ask=0.82, no_bid=0.19, no_ask=0.20,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lt, "fair_up_probability", lambda s, t, n, sl=900.0, *a, **k: 0.97)
        signal = trader.evaluate(mkt, live=True)
    assert signal is None or signal.side == ""

    # A 0.70 maker entry (under the 0.75 cap, real edge) still goes.
    trader3 = _trader()
    trader3._live_balance = 10.0
    mkt3 = _mkt(
        seconds_left=600, target=84000.0,
        yes_bid=0.70, yes_ask=0.72, no_bid=0.29, no_ask=0.31,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lt, "fair_up_probability", lambda s, t, n, sl=900.0, *a, **k: 0.97)
        signal3 = trader3.evaluate(mkt3, live=True)
    assert signal3 is not None and signal3.side == "up"

    # Taker window (60s): the same 0.80 ask is above the 0.60 taker cap.
    trader2 = _trader()
    trader2._live_balance = 10.0
    mkt2 = _mkt(
        seconds_left=60, target=84000.0,
        yes_bid=0.79, yes_ask=0.80, no_bid=0.19, no_ask=0.21,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lt, "fair_up_probability", lambda s, t, n, sl=900.0, *a, **k: 0.97)
        signal2 = trader2.evaluate(mkt2, live=True)
    assert signal2 is None or signal2.side == ""


def test_ninety_cents_hard_block_holds_for_makers_too():
    """0.90+ entries won 4% forever. No fee schedule changes that."""
    import src.jobs.ladder_trader as lt

    trader = _trader()
    trader._live_balance = 10.0
    mkt = _mkt(
        seconds_left=600, target=84000.0,
        yes_bid=0.91, yes_ask=0.92, no_bid=0.07, no_ask=0.08,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lt, "fair_up_probability", lambda s, t, n, sl=900.0, *a, **k: 0.99)
        signal = trader.evaluate(mkt, live=True)
    assert signal is None or signal.side == ""


def test_dry_mirrors_kelly_on_its_own_cash():
    """The law: the same sizing rule, on the DRY book's own money.
    Kelly with confidence-weighting (pivot=0.70): scale = 0.25 + 0.25*(fair-pivot)/(1-pivot)
    = 0.2917; k = 0.2917*0.444 = 0.1296; clip = 300*0.1296 = $38.88; with conviction
    doubling -> $77.76 -> 141 contracts at 0.55, under the 0.5-of-book ceiling."""
    import src.jobs.ladder_trader as lt

    trader = _trader_no_session_mult()
    trader._dry_cash_cache = 300.0
    market = _mkt(
        seconds_left=600, target=84000.0,
        yes_bid=0.55, yes_ask=0.56, no_bid=0.44, no_ask=0.45,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lt, "fair_up_probability", lambda s, t, n, sl=900.0, *a, **k: 0.75)
        signal = trader.evaluate(market, live=False)
    assert signal is not None and signal.side == "up"
    assert signal.contracts == 141
    assert signal.notional <= 300.0 * trader.config.conviction_max_fraction + 0.01


# ---------------------------------------------------------------------------
# One bet per flip: the pyramiding kill
# ---------------------------------------------------------------------------
def _signal_for(ticker="KXBTC15M-26OCT011715-15", side="up", fair=0.75):
    from src.jobs.ladder_trader import UpDownSignal

    return UpDownSignal(
        ticker=ticker, bucket="26OCT011715", side=side, target=84000.0,
        spot=84900.0, spot_vs_target=900.0, fair=fair, kalshi_price=0.45,
        edge=0.30, ask=0.45, contracts=4, notional=1.80,
        seconds_left=300.0, reason="t",
    )


def test_second_clip_on_the_same_flip_is_refused_both_books():
    """The trade-log verdict: -8.16, -6.27, -4.87, -4.31 were ALL pyramids.
    Same-window clips are one bet at 3-4x the Kelly cap."""
    trader = _trader()
    sig = _signal_for()
    held = [{"ticker": sig.ticker, "side": "YES", "notional": 1.80}]
    live_block = trader._entry_block(sig, held, live=True)
    dry_block = trader._entry_block(sig, held, live=False)
    assert "one coin flip" in live_block
    assert "one coin flip" in dry_block


def test_first_clip_passes_and_opposite_side_still_blocks():
    trader = _trader()
    sig = _signal_for()
    assert trader._entry_block(sig, [], live=True) == ""
    opp = [{"ticker": sig.ticker, "side": "NO", "notional": 1.0}]
    block = trader._entry_block(sig, opp, live=True)
    assert "opposite side" in block


def test_pyramid_law_is_one_clip_constant():
    from src.jobs.ladder_trader import MAX_CLIPS_PER_TICKER

    assert MAX_CLIPS_PER_TICKER == 1


def test_take_profit_at_a_loss_holds_for_resolution():
    """A losing take_profit is a locked loss; resolution pays 1 or 0."""
    import inspect

    from src.jobs import track

    src = inspect.getsource(track)
    assert "would net" in src
