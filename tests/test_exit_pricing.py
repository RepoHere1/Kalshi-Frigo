"""The exit system must price positions from the fields Kalshi actually returns.

Kalshi API v2 removed `yes_price` / `no_price` in favour of `*_dollars` string
fields. Reading the old names defaulted to 0, and because every exit path
guarded on `if current_price > 0`, the effect was not a crash but silence:
take-profit and stop-loss never evaluated a single position.

What that looked like from the dashboard: DRY showed 17 open positions, 0
closed trades and $0.00 realized, growing forever. A NO position priced at 0.0
trips `0.0 <= take_profit`, fires a take-profit at exit_price 0.0, and the
$0 sanity guard then refuses to write the close - so the position is stuck open
permanently, retrying forever.
"""
from datetime import datetime, timedelta

import pytest

from src.jobs.track import should_exit_position
from src.utils.database import Position
from src.utils.market_prices import get_market_prices

# Exactly the shape the live API returns today.
V2_MARKET = {
    "ticker": "KXTEST-26-DEC31",
    "status": "open",
    "yes_bid_dollars": "0.5100",
    "yes_ask_dollars": "0.5300",
    "no_bid_dollars": "0.4700",
    "no_ask_dollars": "0.4900",
    "result": None,
}


def test_prices_are_read_from_the_fields_that_exist():
    yes_bid, yes_ask, no_bid, no_ask = get_market_prices(V2_MARKET)
    assert (yes_bid, yes_ask, no_bid, no_ask) == (0.51, 0.53, 0.47, 0.49)


def test_legacy_cent_fields_still_work():
    legacy = {"yes_bid": 51, "yes_ask": 53, "no_bid": 47, "no_ask": 49}
    assert get_market_prices(legacy) == (0.51, 0.53, 0.47, 0.49)


def _mid(market):
    yes_bid, yes_ask, no_bid, no_ask = get_market_prices(market)
    return (yes_bid + yes_ask) / 2.0, (no_bid + no_ask) / 2.0


async def test_a_no_position_takes_profit_on_a_real_quote():
    """The bug that stranded 17 positions: NO priced at 0.0 always triggers."""
    yes, no = _mid(V2_MARKET)
    position = Position(
        market_id="KXTEST-26-DEC31",
        side="NO",
        entry_price=0.50,
        quantity=10,
        timestamp=datetime.now(),
        rationale="test",
        confidence=0.8,
        live=False,
        take_profit_price=0.48,  # NO at 0.48 is a profit
        mode="dry",
    )
    should_exit, reason, exit_price = await should_exit_position(position, yes, no, "open", None)
    assert should_exit is True
    assert reason == "take_profit"
    # The guard in track_positions can only pass a non-zero price.
    assert exit_price > 0.0
    assert exit_price == pytest.approx(0.48)


async def test_a_no_position_that_is_not_profitable_holds():
    yes, no = _mid(V2_MARKET)
    position = Position(
        market_id="KXTEST-26-DEC31",
        side="NO",
        entry_price=0.40,
        quantity=10,
        timestamp=datetime.now(),
        rationale="test",
        confidence=0.8,
        live=False,
        take_profit_price=0.30,
        # A NO position profits when its price FALLS, so its stop sits above the
        # entry (exit if NO climbs to 0.55). Both levels must be out of reach at
        # the current 0.48 quote for this to be a genuine hold.
        stop_loss_price=0.55,
        mode="dry",
    )
    should_exit, reason, _ = await should_exit_position(position, yes, no, "open", None)
    assert should_exit is False
    assert reason != "take_profit"


async def test_a_yes_position_takes_profit_on_a_real_quote():
    yes, no = _mid(V2_MARKET)
    position = Position(
        market_id="KXTEST-26-DEC31",
        side="YES",
        entry_price=0.40,
        quantity=10,
        timestamp=datetime.now(),
        rationale="test",
        confidence=0.8,
        live=False,
        take_profit_price=0.50,
        mode="dry",
    )
    should_exit, reason, exit_price = await should_exit_position(position, yes, no, "open", None)
    assert should_exit is True
    assert reason == "take_profit"
    assert exit_price > 0.0


async def test_a_yes_stop_loss_fires_at_the_real_price_not_zero():
    """YES priced at 0.0 tripped stop-loss on every position, at exit_price 0."""
    yes, no = _mid(V2_MARKET)
    position = Position(
        market_id="KXTEST-26-DEC31",
        side="YES",
        entry_price=0.50,
        quantity=10,
        timestamp=datetime.now(),
        rationale="test",
        confidence=0.8,
        live=False,
        stop_loss_price=0.45,
        mode="dry",
    )
    # 0.52 mid is above the 0.45 stop, so it must hold rather than fire on a $0 read.
    should_exit, _, _ = await should_exit_position(position, yes, no, "open", None)
    assert should_exit is False


async def test_a_market_with_no_quotes_never_books_a_zero_price_close():
    """Unquoted markets must not manufacture an exit at $0.00."""
    empty = {
        "ticker": "KXTEST-26-DEC31",
        "status": "open",
        "yes_bid_dollars": "0.0000",
        "yes_ask_dollars": "0.0000",
        "no_bid_dollars": "0.0000",
        "no_ask_dollars": "0.0000",
        "result": None,
    }
    yes, no = _mid(empty)
    position = Position(
        market_id="KXTEST-26-DEC31",
        side="NO",
        entry_price=0.50,
        quantity=10,
        timestamp=datetime.now(),
        rationale="test",
        confidence=0.8,
        live=False,
        take_profit_price=0.48,
        mode="dry",
    )
    should_exit, _, exit_price = await should_exit_position(position, yes, no, "open", None)
    # Either it holds, or it exits - but it must never report a $0 fill.
    if should_exit:
        assert exit_price > 0.0


async def test_time_based_exit_still_works():
    yes, no = _mid(V2_MARKET)
    position = Position(
        market_id="KXTEST-26-DEC31",
        side="YES",
        entry_price=0.50,
        quantity=10,
        timestamp=datetime.now() - timedelta(hours=48),
        rationale="test",
        confidence=0.8,
        live=False,
        max_hold_hours=24,
        mode="dry",
    )
    should_exit, reason, exit_price = await should_exit_position(position, yes, no, "open", None)
    assert should_exit is True
    assert exit_price > 0.0
