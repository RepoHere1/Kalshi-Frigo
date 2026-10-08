"""Switching to DRY must never destroy the DRY book.

`api_mode_set` called `reset_dry_account()` every time the mode was set to DRY.
That was harmless while reset only re-seeded the cash figure, but once reset
became a real wipe - clearing the ledger, the open positions and the closes -
every pass through the DRY switch silently erased the simulated book.

That is what kept a profitable account snapping back to $300 with nothing to
show for the difference: a wipe, not a loss. Switching modes now *creates* a
book if one is missing and leaves an existing one untouched.
"""
from datetime import datetime

import pytest

from src.utils.database import DatabaseManager, Position, TradeLog
import web_dashboard as wd
from src.utils import mode as mode_module
from src.utils.mode import MODE_DRY, TradingMode


async def _db(tmp_path, name="t.db"):
    db = DatabaseManager(db_path=str(tmp_path / name))
    await db.initialize()
    return db


def _position(**kw):
    base = dict(
        market_id="KXTEST-26",
        side="NO",
        entry_price=0.25,
        quantity=10,
        timestamp=datetime.now(),
        rationale="test",
        confidence=0.8,
        live=False,
        strategy="ai_directional",
        mode="dry",
    )
    base.update(kw)
    return Position(**base)


def _trade(**kw):
    base = dict(
        market_id="KXTEST-26",
        side="NO",
        entry_price=0.25,
        exit_price=0.30,
        quantity=10,
        pnl=0.5,
        entry_timestamp=datetime.now(),
        exit_timestamp=datetime.now(),
        rationale="test",
        strategy="ai_directional",
        exit_reason="take_profit",
        mode="dry",
    )
    base.update(kw)
    return TradeLog(**base)


async def test_ensure_creates_a_book_when_there_is_none(tmp_path):
    mgr = TradingMode(db_path=str(tmp_path / "a.db"))
    account = await mgr.ensure_dry_account()
    assert account["cash"] == pytest.approx(200.0, abs=0.01)


async def test_ensure_never_touches_a_book_with_history(tmp_path):
    """The regression: a mode-set used to wipe a profitable book."""
    db = await _db(tmp_path)
    mgr = TradingMode(db_path=db.db_path)

    await mgr.record_fill(market_id="KXTEST-26", side="NO", action="buy", quantity=10, price=0.25)
    await db.add_position(_position())
    await db.add_trade_log(_trade())

    before = await mgr.dry_account()
    assert before["open_positions"] == 1
    assert before["closed_trades"] == 1

    for _ in range(3):
        account = await mgr.ensure_dry_account()

    assert account["cash"] == pytest.approx(before["cash"], abs=0.01)
    assert account["open_positions"] == 1
    assert account["closed_trades"] == 1
    assert account["realized"] == pytest.approx(0.5, abs=0.01)


async def test_ensure_leaves_the_ledger_intact(tmp_path):
    db = await _db(tmp_path)
    mgr = TradingMode(db_path=db.db_path)
    await mgr.record_fill(market_id="KXTEST-26", side="NO", action="buy", quantity=1, price=1.0)
    before = len(await mgr.ledger(limit=1000))
    await mgr.ensure_dry_account()
    assert len(await mgr.ledger(limit=1000)) == before


async def test_the_explicit_reset_is_still_destructive(tmp_path):
    """The button on the page must still mean 'start over'."""
    db = await _db(tmp_path)
    mgr = TradingMode(db_path=db.db_path)
    await mgr.record_fill(market_id="KXTEST-26", side="NO", action="buy", quantity=10, price=0.25)
    await db.add_position(_position())
    await db.add_trade_log(_trade())

    account = await mgr.reset_dry_account()

    assert account["cash"] == pytest.approx(200.0, abs=0.01)
    assert account["open_positions"] == 0
    assert account["closed_trades"] == 0


async def test_switching_to_live_does_not_destroy_the_dry_book(tmp_path):
    """The cause of the book "resetting" to \ over and over.

    TradingMode.set() used to delete every DRY position, close and ledger row
    when switching to LIVE, on the reasoning that the other book's rows "must
    not exist". So every glance at LIVE wiped the simulated book, and coming
    back to DRY showed \ with no history. Isolation comes from the mode
    column, not from deleting data.
    """
    db = await _db(tmp_path)
    mgr = TradingMode(db_path=db.db_path)

    await mgr.record_fill(market_id="KXTEST-26", side="NO", action="buy", quantity=10, price=0.25)
    await db.add_position(_position())
    await db.add_trade_log(_trade())
    before = await mgr.dry_account()

    await mgr.set("live", confirmed=True)
    await mgr.set("dry", confirmed=True)

    after = await mgr.dry_account()
    assert after["cash"] == pytest.approx(before["cash"], abs=0.01)
    assert after["open_positions"] == before["open_positions"] == 1
    assert after["closed_trades"] == before["closed_trades"] == 1
    assert after["realized"] == pytest.approx(0.5, abs=0.01)


async def test_the_two_books_can_coexist(tmp_path):
    """A DRY position and a LIVE position are both retained and read apart."""
    db = await _db(tmp_path)

    await db.add_position(_position(strategy="ai_directional", mode="dry"))
    await db.add_position(
        Position(
            market_id="KXLIVE-26",
            side="YES",
            entry_price=0.5,
            quantity=2,
            timestamp=datetime.now(),
            rationale="live",
            confidence=0.8,
            live=True,
            strategy="ai_directional",
            mode="live",
        )
    )

    dry_rows = await db.get_open_positions(mode="dry")
    live_rows = await db.get_open_positions(mode="live")
    assert [p.market_id for p in dry_rows] == ["KXTEST-26"]
    assert [p.market_id for p in live_rows] == ["KXLIVE-26"]


# ---------------------------------------------------------------------------
# DRY mirrors LIVE: a fresh book is seeded with the real Kalshi balance
# through the live credentials, so the only difference from LIVE is that
# fills are simulated.
# ---------------------------------------------------------------------------


def _mirror_env(monkeypatch, balance: float):
    """Pretend the live keys resolve and the Kalshi API answers `balance`."""
    monkeypatch.setattr(mode_module, "_materialize_private_key", lambda: "/fake/key.pem")

    async def fake_funding(self, private_key_path=None):
        return {
            "connected": True,
            "balance": round(balance, 2),
            "balance_cents": int(round(balance * 100)),
            "can_fund": balance >= 1.0,
            "reason": "",
        }

    monkeypatch.setattr(TradingMode, "funding", fake_funding)


async def test_ensure_seeds_a_fresh_book_at_the_default(tmp_path, monkeypatch):
    """A fresh DRY book opens at the fixed $200 figure, live balance ignored."""
    _mirror_env(monkeypatch, 1234.56)
    mgr = TradingMode(db_path=str(tmp_path / "mirror.db"))
    account = await mgr.ensure_dry_account()
    assert account["starting_balance"] == pytest.approx(200.0, abs=0.01)
    assert account["cash"] == pytest.approx(200.0, abs=0.01)


async def test_ensure_keeps_the_default_when_live_is_unfundable(tmp_path, monkeypatch):
    """A tiny live balance still yields the $200 DRY start."""
    _mirror_env(monkeypatch, 0.004)
    mgr = TradingMode(db_path=str(tmp_path / "poor.db"))
    account = await mgr.ensure_dry_account()
    assert account["cash"] == pytest.approx(200.0, abs=0.01)


async def test_ensure_keeps_the_default_when_no_credentials(tmp_path, monkeypatch):
    """No key material means the live balance is unknowable - fall back."""
    monkeypatch.setattr(mode_module, "_materialize_private_key", lambda: None)
    mgr = TradingMode(db_path=str(tmp_path / "nocreds.db"))
    account = await mgr.ensure_dry_account()
    assert account["cash"] == pytest.approx(200.0, abs=0.01)


async def test_ensure_never_reseeds_a_book_that_exists(tmp_path, monkeypatch):
    """The fixed start only applies at creation; history is never re-seeded."""
    _mirror_env(monkeypatch, 9999.0)
    db = await _db(tmp_path, name="existing.db")
    mgr = TradingMode(db_path=db.db_path)
    await mgr.record_fill(market_id="KXTEST-26", side="NO", action="buy", quantity=10, price=0.25)
    before = await mgr.dry_account()
    account = await mgr.ensure_dry_account()
    assert account["cash"] == pytest.approx(before["cash"], abs=0.01)
    assert account["starting_balance"] == pytest.approx(200.0, abs=0.01)


async def test_reset_reseeds_at_the_fixed_default(tmp_path, monkeypatch):
    """'Start over' restores the $200 figure, independent of the live balance."""
    _mirror_env(monkeypatch, 500.0)
    db = await _db(tmp_path, name="wipe.db")
    mgr = TradingMode(db_path=db.db_path)
    await mgr.record_fill(market_id="KXTEST-26", side="NO", action="buy", quantity=10, price=0.25)
    await db.add_position(_position())
    await db.add_trade_log(_trade())

    account = await mgr.reset_dry_account()

    assert account["starting_balance"] == pytest.approx(200.0, abs=0.01)
    assert account["cash"] == pytest.approx(200.0, abs=0.01)
    assert account["open_positions"] == 0
    assert account["closed_trades"] == 0


async def test_reset_keeps_the_starting_figure_when_live_is_unreachable(tmp_path, monkeypatch):
    """An unreachable live account must not change the reset semantics."""
    monkeypatch.setattr(mode_module, "_materialize_private_key", lambda: None)
    db = await _db(tmp_path, name="wipe2.db")
    mgr = TradingMode(db_path=db.db_path)
    await mgr.record_fill(market_id="KXTEST-26", side="NO", action="buy", quantity=10, price=0.25)

    account = await mgr.reset_dry_account()

    assert account["cash"] == pytest.approx(200.0, abs=0.01)
    assert account["open_positions"] == 0
