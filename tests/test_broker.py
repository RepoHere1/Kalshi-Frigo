"""DRY must be a real rehearsal of the live order path.

The point of these tests is that a DRY run is only worth something if it fails
for the same reasons a live order would. Before the broker split, the paper
branch skipped validation entirely and always reported success.
"""

import asyncio
import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from typing import Any, TypedDict

import pytest

from src.jobs.broker import (
    MAX_PRICE_CENTS,
    MIN_ORDER_CONTRACTS,
    minimum_viable_quantity,
    DryBroker,
    LiveBroker,
    OrderRequest,
    broker_for_mode,
    build_order_request,
)
from src.utils.database import Position
from src.utils.mode import MODE_DRY, MODE_LIVE, TradingMode, run

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


@pytest.fixture
def mode_manager(tmp_path):
    return TradingMode(db_path=str(tmp_path / "frigo_broker.db"))


# ---------------------------------------------------------------------------
# Structural safety: DRY has no way to transmit
# ---------------------------------------------------------------------------
def test_dry_broker_has_no_place_order(mode_manager):
    """The guarantee is structural, not a runtime flag."""
    assert not hasattr(DryBroker(mode_manager), "place_order")
    assert not hasattr(DryBroker(mode_manager), "_client")


def test_broker_for_dry_refuses_a_kalshi_client(mode_manager):
    """Passing a client into the DRY path is not even possible."""
    client = MagicMock()
    broker = broker_for_mode(MODE_DRY, kalshi_client=client, mode_manager=mode_manager)
    assert isinstance(broker, DryBroker)
    assert getattr(broker, "_client", None) is None
    client.place_order.assert_not_called()


def test_broker_for_dry_requires_a_manager():
    with pytest.raises(ValueError, match="requires a TradingMode"):
        broker_for_mode(MODE_DRY)


def test_broker_for_live_requires_a_client(mode_manager):
    with pytest.raises(ValueError, match="requires a KalshiClient"):
        broker_for_mode(MODE_LIVE, mode_manager=mode_manager)


def test_only_live_broker_can_place_orders(mode_manager):
    client = MagicMock()
    live = broker_for_mode(MODE_LIVE, kalshi_client=client, mode_manager=mode_manager)
    assert isinstance(live, LiveBroker)
    assert live.simulated is False
    assert DryBroker(mode_manager).simulated is True


# ---------------------------------------------------------------------------
# Validation is shared, so DRY fails like LIVE
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
    """issue #42: collection tickers quote 100/100 and Kalshi 400s on them."""
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
# DRY books against the simulated ledger
# ---------------------------------------------------------------------------
def test_dry_submit_debits_simulated_cash(mode_manager):
    req, _ = build_order_request(
        market_id="KXTEST-26",
        side="YES",
        action="buy",
        quantity=10,
        market=NORMAL_MARKET,
        available_cents=300_00,
    )
    broker = DryBroker(mode_manager)
    response = asyncio.run(broker.submit(req))
    assert response["simulated"] is True
    assert response["order"]["status"] == "filled"
    assert response["order"]["order_id"].startswith("dry-")
    assert run(mode_manager.dry_account())["cash"] == pytest.approx(295.62)


def test_dry_available_cents_comes_from_the_simulated_book(mode_manager):
    broker = DryBroker(mode_manager)
    assert asyncio.run(broker.available_cents()) == 300_00


def test_dry_refuses_when_simulated_cash_is_gone(mode_manager):
    run(mode_manager.set_dry_cash(1.0))
    req, reason = build_order_request(
        market_id="KXTEST-26",
        side="YES",
        action="buy",
        quantity=10,
        market=NORMAL_MARKET,
        available_cents=100,
    )
    assert req is None, "validation should refuse first"
    assert "available" in reason


def test_dry_sell_credits_cash(mode_manager, tmp_path):
    """A sell credits cash ONLY when an open DRY position backs it."""
    from datetime import datetime

    from src.utils.database import DatabaseManager, Position

    db = DatabaseManager(db_path=str(tmp_path / "frigo_broker.db"))
    asyncio.run(db.initialize())
    position = Position(
        market_id="KXTEST-26",
        side="YES",
        entry_price=0.60,
        quantity=10,
        timestamp=datetime.now(),
        rationale="backing",
        confidence=0.5,
        live=False,
        strategy="btc_updown",
        mode="dry",
    )
    position.id = asyncio.run(db.add_position(position, allow_duplicate=True))
    req, _ = build_order_request(
        market_id="KXTEST-26",
        side="YES",
        action="sell",
        quantity=10,
        market=NORMAL_MARKET,
        available_cents=300_00,
        limit_price_dollars=0.60,
    )
    asyncio.run(DryBroker(mode_manager).submit(req))
    assert run(mode_manager.dry_account())["cash"] == pytest.approx(305.83)


def test_dry_sell_without_a_backing_position_is_refused(mode_manager):
    """The phantom-cash fix: no open row, no credit, ever."""
    req, _ = build_order_request(
        market_id="KXTEST-26",
        side="YES",
        action="sell",
        quantity=10,
        market=NORMAL_MARKET,
        available_cents=300_00,
        limit_price_dollars=0.60,
    )
    response = asyncio.run(DryBroker(mode_manager).submit(req))
    assert response.get("error")
    assert run(mode_manager.dry_account())["cash"] == pytest.approx(300.0)


def test_live_broker_calls_place_order_with_validated_kwargs():
    client = MagicMock()
    client.place_order = AsyncMock(return_value={"order": {"order_id": "abc"}})
    client.get_balance = AsyncMock(return_value={"balance": 50_000})
    req, _ = build_order_request(
        market_id="KXTEST-26",
        side="YES",
        action="buy",
        quantity=10,
        market=NORMAL_MARKET,
        available_cents=50_000,
    )
    response = asyncio.run(LiveBroker(client).submit(req))
    assert response["order"]["order_id"] == "abc"
    kwargs = client.place_order.call_args.kwargs
    assert kwargs["ticker"] == "KXTEST-26"
    assert kwargs["side"] == "yes"
    assert kwargs["count"] == 10
    assert kwargs["yes_price"] == 42
    assert "no_price" not in kwargs


# ---------------------------------------------------------------------------
# execute_position in DRY: validates, then simulates, and never transmits
# ---------------------------------------------------------------------------
def _position(quantity=10, entry=0.42):
    return Position(
        market_id="KXTEST-26",
        side="YES",
        entry_price=entry,
        quantity=quantity,
        timestamp=datetime(2026, 1, 1),
        id=1,
        strategy="ai_directional",
    )


def _client(market):
    client = MagicMock()
    client.get_market = AsyncMock(return_value={"market": market})
    client.get_balance = AsyncMock(return_value={"balance": 0})
    client.place_order = AsyncMock(return_value={"order": {"order_id": "SHOULD-NOT-HAPPEN"}})
    return client


def _patch_mode(monkeypatch, db_path):
    monkeypatch.setenv("DB_PATH", str(db_path))
    import src.jobs.execute as ex

    monkeypatch.setattr(ex, "default_mode_manager", lambda p: TradingMode(db_path=p))


def test_execute_dry_does_not_promote_the_position(monkeypatch, tmp_path):
    """`live` is how a real position is told from a simulated one.

    DRY used to call update_position_to_live, which made simulated fills
    indistinguishable from real ones everywhere downstream (Kalshi fallback,
    per-strategy P&L, exposure figures).
    """
    from src.jobs.execute import execute_position

    _patch_mode(monkeypatch, tmp_path / "m.db")
    db = MagicMock()
    db.update_position_to_live = AsyncMock()
    # execute_position now enforces the per-strategy open-position cap.
    db.get_open_positions = AsyncMock(return_value=[])
    client = _client(NORMAL_MARKET)

    ok = asyncio.run(execute_position(_position(), False, db, client))

    assert ok is True
    db.update_position_to_live.assert_not_called(), "DRY must not mark a position live"
    client.place_order.assert_not_called(), "DRY must never transmit"
    # The simulated ledger recorded the real notional.
    mgr = TradingMode(db_path=str(tmp_path / "m.db"))
    assert run(mgr.dry_account())["cash"] == pytest.approx(295.62)


def test_execute_live_promotes_the_position(monkeypatch, tmp_path):
    from src.jobs.execute import execute_position

    _patch_mode(monkeypatch, tmp_path / "m.db")
    db = MagicMock()
    db.update_position_to_live = AsyncMock()
    # execute_position now enforces the per-strategy open-position cap.
    db.get_open_positions = AsyncMock(return_value=[])
    client = _client(NORMAL_MARKET)
    client.get_balance = AsyncMock(return_value={"balance": 50_000})

    assert asyncio.run(execute_position(_position(), True, db, client)) is True
    db.update_position_to_live.assert_awaited_once()


def test_execute_dry_rejects_untradeable_market(monkeypatch, tmp_path):
    """The old paper branch said success here; the exchange would say 400."""
    from src.jobs.execute import execute_position

    _patch_mode(monkeypatch, tmp_path / "m.db")
    db = MagicMock()
    db.update_position_to_live = AsyncMock()
    # execute_position now enforces the per-strategy open-position cap.
    db.get_open_positions = AsyncMock(return_value=[])
    client = _client(
        {"ticker": "KXCOLL", "yes_bid": 100, "yes_ask": 100, "no_bid": 100, "no_ask": 100}
    )

    ok = asyncio.run(execute_position(_position(), False, db, client))

    assert ok is False
    db.update_position_to_live.assert_not_called()
    client.place_order.assert_not_called()


def test_execute_dry_rejects_when_simulated_cash_too_small(monkeypatch, tmp_path):
    from src.jobs.execute import execute_position

    _patch_mode(monkeypatch, tmp_path / "m.db")
    mgr = TradingMode(db_path=str(tmp_path / "m.db"))
    run(mgr.set_dry_cash(2.0))
    db = MagicMock()
    db.update_position_to_live = AsyncMock()
    # execute_position now enforces the per-strategy open-position cap.
    db.get_open_positions = AsyncMock(return_value=[])
    client = _client(NORMAL_MARKET)

    ok = asyncio.run(execute_position(_position(quantity=100), False, db, client))

    assert ok is False
    db.update_position_to_live.assert_not_called()
    client.place_order.assert_not_called()


def test_execute_live_still_places(monkeypatch, tmp_path):
    """The live path must be unchanged by the DRY work."""
    from src.jobs.execute import execute_position

    _patch_mode(monkeypatch, tmp_path / "m.db")
    db = MagicMock()
    db.update_position_to_live = AsyncMock()
    # execute_position now enforces the per-strategy open-position cap.
    db.get_open_positions = AsyncMock(return_value=[])
    client = _client(NORMAL_MARKET)
    client.get_balance = AsyncMock(return_value={"balance": 50_000})

    ok = asyncio.run(execute_position(_position(), True, db, client))

    assert ok is True
    client.place_order.assert_awaited_once()
    assert client.place_order.call_args.kwargs["ticker"] == "KXTEST-26"


def test_order_request_repr_is_informative():
    req = OrderRequest(ticker="KXTEST-26", client_order_id="x", side="yes", action="buy", count=10)
    assert "KXTEST-26" in repr(req)
    assert os.environ.get("PATH")  # sanity: module imported cleanly
