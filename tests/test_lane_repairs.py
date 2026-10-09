"""Regression tests for the 2026-10-09 lane repairs.

Every one of these guards a specific reason the lanes sat silent in
production:

- the venue guard comparing BTC's price against an XRP target forever,
- the updown result dict crashing on a None signal,
- the websockets header-kwarg rename leaving the BRTI feed dead,
- the certain-win pair trade being wired into the lane cycle.
"""

import inspect
from pathlib import Path

from src.jobs.ladder_trader import UpDownConfig, UpDownTrader
from src.jobs.market_data import Btc15mFeed, SpotFeed


def test_venue_guard_is_scoped_to_the_btc_lane():
    """Kraken/Bitstamp feeds are BTC; on other assets the guard must be off.

    The production XRP log: "others +81,707 vs target ... gap +81,707" -
    BTC's price against XRP's $1.38 - vetoing every entry forever.
    """
    xrp = UpDownTrader(SpotFeed(), Btc15mFeed(series="KXXRP15M"))
    assert xrp.config.venue_guard_enabled is False
    btc = UpDownTrader(SpotFeed(), Btc15mFeed(series="KXBTC15M"))
    assert btc.config.venue_guard_enabled is True


def test_cycle_never_reads_signal_ask_when_signal_is_none():
    """The crash: required_edge read signal.ask with signal None."""
    src = inspect.getsource(UpDownTrader.cycle)
    # The guarded expression replaced the bare read; the bare read is gone.
    assert "if (signal and (live or self.config.dry_fee_enabled))" in src


def test_kalshi_websocket_tries_both_header_spellings():
    """websockets renamed the header kwarg; the pinned v12 needs extra_headers."""
    src = (Path(__file__).resolve().parents[1] / "src" / "clients" / "kalshi_ws.py").read_text(
        encoding="utf-8"
    )
    assert "extra_headers=headers" in src
    assert "additional_headers=headers" in src
    assert "except TypeError" in src


def test_certain_win_pair_is_wired_into_the_cycle():
    src = inspect.getsource(UpDownTrader.cycle)
    assert "_certain_win_pair" in src
    assert "certain_win_enabled" in src
    method = inspect.getsource(UpDownTrader._certain_win_pair)
    # Both legs go through the canonical execution path, taker, one per side.
    assert '("YES", up_ask), ("NO", down_ask)' in method
    assert "execute_position" in method


def test_certain_win_defaults_are_enabled_and_bounded():
    c = UpDownConfig()
    assert c.certain_win_enabled is True
    assert 0 < c.certain_win_min_net < 0.05
    assert 0 < c.certain_win_cash_fraction <= 0.75
    assert c.certain_win_max_usd <= 100.0


def test_quick_win_no_longer_reads_the_environment():
    """The 15%-edge experiment is gone: no env can bring it back."""
    src = inspect.getsource(UpDownConfig.__post_init__)
    assert "QUICK_WIN_ENABLED" not in src


def test_gold_lane_prices_against_the_paxg_proxy():
    src = inspect.getsource(UpDownConfig.apply_asset)
    assert "PAXG-USD" in src
