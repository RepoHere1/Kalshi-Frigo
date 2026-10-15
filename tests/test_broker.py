"""Broker tests for LIVE trading only.

This file tests order validation and submission for the single LIVE broker.
All DRY book functionality has been removed.
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.jobs.broker import (
    MAX_PRICE_CENTS,
    MIN_ORDER_CONTRACTS,
    minimum_viable_quantity,
    LiveBroker,
    OrderRequest,
    build_order_request,
)

# A tradeable market with a real-looking v2 book.
NORMAL_MARKET = {
    "ticker": "KXTEST-26",
    "yes_bid": 40,
    "yes_ask": 42,
    "no_bid": 58,
    "no_ask": 60,
    "volume": 5000,
    "status": "open",
}


# ---------------------------------------------------------------------------
# Validation is shared (DRY and LIVE use the same validation logic)
# ---------------------------------------------------------------------------
def test_build_accepts_a_valid_order():
    req, reason = build_order_request(
        market_id="KXTEST-26",
        side="YES",
        action="buy",
        quantity=10,
        market=NORMAL_MARKET,
        available_cents=300_00,
    )
    assert reason == ""
    assert req is not None
    assert req.yes_price == 42 and req.no_price is None
    assert req.notional == pytest.approx(4.20)
    assert req.type_ == "market"
    kwargs = req.as_place_order_kwargs()
    assert set(kwargs) == {
        "ticker",
        "client_order_id",
        "side",
        "action",
        "count",
        "type_",
        "yes_price",
    }


def test_build_rejects_untradeable_collection_ticker():
    """issue #42: collection tickers quote $1.00/$1.00 and Kalshi 400s on them."""
    market = {"ticker": "KXCOLL", "yes_bid": 100, "yes_ask": 100, "no_bid": 100, "no_ask": 100}
    req, reason = build_order_request(
        market_id="KXCOLL",
        side="YES",
        action="buy",
        quantity=10,
        market=market,
        available_cents=300_00,
    )
    assert req is None
    assert "not directly tradeable" in reason


def test_build_rejects_out_of_range_price():
    market = dict(NORMAL_MARKET, yes_ask=0, no_ask=0, yes_bid=0, no_bid=0)
    req, reason = build_order_request(
        market_id="KXTEST-26",
        side="YES",
        action="buy",
        quantity=10,
        market=market,
        available_cents=300_00,
    )
    assert req is None
    assert str(MAX_PRICE_CENTS) in reason


def test_build_rejects_unaffordable_order():
    req, reason = build_order_request(
        market_id="KXTEST-26",
        side="YES",
        action="buy",
        quantity=100,
        market=NORMAL_MARKET,
        available_cents=100,
    )
    assert req is None
    assert "only 100c available" in reason


def test_size_floor_raises_a_sub_minimum_buy():
    """A fractional (sub-contract) buy is rounded up to one whole contract, the
    exchange's only floor. There is no $1.00 notional minimum."""
    req, reason = build_order_request(
        market_id="KXTEST-26", side="YES", action="buy", quantity=1,
        market=NORMAL_MARKET, available_cents=300_00,
    )
    assert req is not None and reason == ""
    assert req.count == 1, "one whole contract is the floor"
    assert req.notional == pytest.approx(0.42)


def test_size_floor_never_reduces_an_adequate_order():
    req, _ = build_order_request(
        market_id="KXTEST-26", side="YES", action="buy", quantity=50,
        market=NORMAL_MARKET, available_cents=300_00,
    )
    assert req is not None
    assert req.count == 50


def test_size_floor_still_respects_the_funding_source():
    """The order must not exceed the available cash, even at one contract."""
    req, reason = build_order_request(
        market_id="KXTEST-26", side="YES", action="buy", quantity=1,
        market=NORMAL_MARKET, available_cents=0,  # empty account
    )
    assert req is None
    assert "available" in reason


def test_size_floor_does_not_apply_to_sells():
    """A small remainder must still be sellable so it can be closed out."""
    req, _ = build_order_request(
        market_id="KXTEST-26", side="YES", action="sell", quantity=1,
        market=NORMAL_MARKET, available_cents=300_00, limit_price_dollars=0.42,
    )
    assert req is not None and req.count == 1


def test_minimum_viable_quantity_math():
    # The exchange minimum is one whole contract, regardless of dollar notional.
    assert minimum_viable_quantity(0.42) == 1
    assert minimum_viable_quantity(0.99) == 1
    assert minimum_viable_quantity(0.50) == 1
    assert minimum_viable_quantity(0.05) == 1
    assert minimum_viable_quantity(0) == 0


def test_build_rejects_bad_side_and_quantity():
    bad_cases: list[tuple[str, str, str, int, dict[str, Any], int, Any]] = [
        ("KXTEST-26", "MAYBE", "buy", 10, NORMAL_MARKET, 300_00, None),
        ("KXTEST-26", "YES", "buy", 0, NORMAL_MARKET, 300_00, None),
    ]
    for market_id, side, action, quantity, market, available_cents, limit_price in bad_cases:
        req, reason = build_order_request(
            market_id=market_id,
            side=side,
            action=action,
            quantity=quantity,
            market=market,
            available_cents=available_cents,
            limit_price_dollars=limit_price,
        )
        assert req is None and reason


def test_no_side_price_is_sent_when_buying_yes():
    req, _ = build_order_request(
        market_id="KXTEST-26",
        side="YES",
        action="buy",
        quantity=10,
        market=NORMAL_MARKET,
        available_cents=300_00,
    )
    assert "no_price" not in req.as_place_order_kwargs()


def test_limit_order_uses_the_limit_price():
    req, _ = build_order_request(
        market_id="KXTEST-26",
        side="NO",
        action="sell",
        quantity=10,
        market=NORMAL_MARKET,
        available_cents=300_00,
        limit_price_dollars=0.55,
    )
    assert req.type_ == "limit"
    assert req.no_price == 55
    assert req.yes_price is None


# ---------------------------------------------------------------------------
# Live broker tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
def test_live_broker_calls_place_order_with_validated_kwargs():
    async def run_test():
        client = MagicMock()
        client.get_balance = AsyncMock(return_value={"balance": 50_000})
        client.place_order = AsyncMock(return_value={"order": {"order_id": "abc"}})
        req, _ = build_order_request(
            market_id="KXTEST-26",
            side="YES",
            action="buy",
            quantity=10,
            market=NORMAL_MARKET,
            available_cents=50_000,
        )
        response = await LiveBroker(client).submit(req)
        assert response["order"]["order_id"] == "abc"
        kwargs = client.place_order.call_args.kwargs
        assert kwargs["ticker"] == "KXTEST-26"
        assert kwargs["side"] == "yes"
        assert kwargs["count"] == 10
        assert kwargs["yes_price"] == 42
        assert "no_price" not in kwargs

    asyncio.run(run_test())


@pytest.mark.asyncio
def test_live_broker_available_cents():
    async def run_test():
        client = MagicMock()
        client.get_balance = AsyncMock(return_value={"balance": 50_000})
        assert await LiveBroker(client).available_cents() == 50_000

    asyncio.run(run_test())


@pytest.mark.asyncio
def test_live_broker_cancel():
    async def run_test():
        client = MagicMock()
        client.cancel_order = AsyncMock(return_value={"order": {"order_id": "abc", "status": "cancelled"}})
        response = await LiveBroker(client).cancel("abc")
        assert response["order"]["order_id"] == "abc"

    asyncio.run(run_test())


# ---------------------------------------------------------------------------
# OrderRequest tests
# ---------------------------------------------------------------------------
def test_order_request_repr_is_informative():
    req = OrderRequest(ticker="KXTEST-26", client_order_id="x", side="yes", action="buy", count=10)
    assert "KXTEST-26" in repr(req)
    assert "OrderRequest" in repr(req)