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


def test_every_trade_log_carries_the_entry_fair():
    """CALIBRATION invariant: every close path must stamp the model's entry
    fair onto the trade log, or the win-rate-by-band report card is blind.
    And the lane must record the CHOSEN SIDE's fair, not the up-fair - the
    inversion that put strong DOWN trades in the 'below 0.55' band."""
    from src.jobs import execute as ex
    from src.jobs import track as tk
    from src.jobs.ladder_trader import UpDownTrader

    src = inspect.getsource(tk) + inspect.getsource(ex)
    assert src.count("entry_fair=getattr(position") >= 5
    lane_src = inspect.getsource(UpDownTrader)
    assert "entry_fair=float(" in lane_src
    assert "1.0 - signal.fair" in lane_src  # side-aware: chosen-side prob
    assert 'getattr(signal, "side"' in lane_src


def test_no_strategy_reads_the_env_live_gate():
    """The dashboard switch is the money law: no strategy CODE may read the
    LIVE_TRADING_ENABLED env var. Spawned children have it scrubbed, so any
    reader silently runs DRY inside a LIVE child - the exact bug fixed in
    market_making, unified_trading_system, and immediate. Comments that
    explain the old bug are fine; executable reads are not."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "strategies"
    offenders = []
    for p in root.rglob("*.py"):
        for line in p.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            code = stripped.split("#", 1)[0]
            reads_env = (
                "settings.trading.live_trading_enabled" in code
                or "getattr(settings.trading, 'live_trading_enabled'" in code
                or 'getattr(settings.trading, "live_trading_enabled"' in code
                or 'getenv("LIVE_TRADING_ENABLED"' in code
                or "getenv('LIVE_TRADING_ENABLED'" in code
                or 'environ.get("LIVE_TRADING_ENABLED"' in code
                or "environ.get('LIVE_TRADING_ENABLED'" in code
                or 'environ["LIVE_TRADING_ENABLED"]' in code
            )
            if reads_env:
                offenders.append(f"{p.relative_to(root)}: {code[:70]}")
    assert offenders == [], offenders


def test_every_crypto_lane_carries_its_own_series_and_feed():
    """No lane may inherit another lane's default series again.

    The btc_updown command shipped without --series/--spot-product, so it
    silently traded XRP (the argparse defaults). Every lane now names its
    own market explicitly.
    """
    from web_dashboard import STRATEGY_COMMANDS as cmds

    assert "KXBTC15M" in cmds["btc_updown"]
    assert "BTC-USD" in cmds["btc_updown"]
    for lane, series in (
        ("xrp_updown", "KXXRP15M"),
        ("xau_updown", "KXGOLD15M"),
        ("btc_1h_updown", "KXETH15M"),
        ("hyperliquid_updown", "KXHYPE15M"),
    ):
        assert series in cmds[lane], lane
        assert "--spot-product" in cmds[lane], lane
        assert cmds[lane].count("--series") == 1, lane


def test_series_normalization_never_mangles_real_tickers():
    from cli import _normalize_updown_series

    assert _normalize_updown_series("KXBTC15M") == "KXBTC15M"
    assert _normalize_updown_series("KXETH15M") == "KXETH15M"
    assert _normalize_updown_series("KXXAU15M") == "KXXAU15M"
    assert _normalize_updown_series("btc15m") == "KXBTC15M"
    assert _normalize_updown_series("XRP15M") == "KXXRP15M"
    assert _normalize_updown_series("") == "KXBTC15M"


def test_exit_helpers_claim_before_selling_and_close_after():
    """The DRY money printer: helpers sold+credited without claim or close.

    Every process re-found the same open row every few seconds and sold it
    again - cash tripled off duplicate credits with zero closes. Both
    helpers must claim first, close on a booked sell, and release only when
    nothing booked.
    """
    import inspect

    from src.jobs import execute as ex

    for fn in (ex.place_profit_taking_orders, ex.place_stop_loss_orders):
        src = inspect.getsource(fn)
        assert "claim_position_for_close" in src, fn.__name__
        assert src.index("claim_position_for_close") < src.index("place_sell_limit_order"), fn.__name__
        assert "_close_after_sell" in src, fn.__name__
        assert "release_position_claim(_pid)" in src, fn.__name__
        # Release is conditional on nothing having booked.
        assert "claimed and not booked" in src, fn.__name__


def test_hours_since_matches_timestamp_awareness():
    """Naive vs aware subtraction crashed every tracking pass for DB rows."""
    from datetime import datetime, timedelta, timezone

    from src.jobs.track import _hours_since

    naive = datetime.now() - timedelta(hours=2)
    aware = datetime.now(timezone.utc) - timedelta(hours=2)
    assert 1.9 < _hours_since(naive) < 2.1
    assert 1.9 < _hours_since(aware) < 2.1
    assert _hours_since(None) == 0.0
