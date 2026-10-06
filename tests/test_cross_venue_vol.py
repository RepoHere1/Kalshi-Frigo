"""The vol-edge machinery and the Polymarket same-event scanner.

Everything here is pure math or cache-level logic: no network on the test
path. The law holds across both books - these are shared code paths; only
real-money ORDERING on Hyperliquid stays behind an operator-armed gate.
"""
import math
import time
from datetime import datetime, timezone

import pytest

from src.jobs.cross_venue_poly import (
    PolyScanner,
    PolyWindow,
    bucket_to_utc,
    et_to_utc,
    _parse_prices,
)
from src.jobs.ladder_trader import UpDownConfig, UpDownMarket, UpDownTrader
from src.jobs.market_data import Btc15mFeed, SpotFeed
from src.jobs.vol_edge import (
    HyperliquidMark,
    hedge_btc_qty,
    implied_sigma,
    probit,
)


# ---------------------------------------------------------------------------
# ET clock math
# ---------------------------------------------------------------------------
def test_et_to_utc_handles_edt():
    """October is EDT (-4): 9:00AM ET = 13:00Z."""
    assert et_to_utc(10, 6, 9, 0, "AM") == datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc)


def test_et_to_utc_handles_est():
    """January is EST (-5): 2:00AM ET = 07:00Z (vs 06:00Z in DST)."""
    assert et_to_utc(1, 15, 2, 0, "AM").hour == 7
    assert et_to_utc(7, 15, 2, 0, "AM").hour == 6  # July: always EDT


def test_pm_12am_is_midnight():
    assert et_to_utc(10, 6, 12, 0, "AM").hour in (4, 5)


def test_bucket_to_utc_parses_kalshi_stamps():
    assert bucket_to_utc("26OCT060200") == datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc)
    assert bucket_to_utc("garbage") is None
    assert bucket_to_utc(None) is None


# ---------------------------------------------------------------------------
# Polymarket parsing
# ---------------------------------------------------------------------------
def test_parse_prices_list_and_string():
    assert _parse_prices(["0.51", "0.49"]) == (0.51, 0.49)
    assert _parse_prices('["0.51", "0.49"]') == (0.51, 0.49)
    assert _parse_prices("junk") == (None, None)


def test_window_question_is_parsed_into_a_priced_bucket():
    scanner = PolyScanner()
    events = [
        {
            "markets": [
                {
                    "question": "Bitcoin Up or Down - October 6, 8:45AM-9:00AM ET",
                    "outcomePrices": ["0.62", "0.38"],
                }
            ]
        }
    ]
    scanner._parse_events(events)
    end = et_to_utc(10, 6, 9, 0, "AM")
    w = scanner.window_for(end)
    assert w is not None
    assert w.up == 0.62 and w.down == 0.38


def test_strike_question_is_parsed():
    scanner = PolyScanner()
    events = [
        {
            "markets": [
                {
                    "question": "Bitcoin above 87,400 on October 6, 4AM ET?",
                    "outcomePrices": ["0.5", "0.5"],
                }
            ]
        }
    ]
    scanner._parse_events(events)
    end = et_to_utc(10, 6, 4, 0, "AM")
    got = scanner.strike_price(end, 87400.0)
    assert got is not None
    assert got[0] == 0.5


def test_same_event_arb_when_the_two_venues_undercut_a_dollar():
    scanner = PolyScanner()
    end = et_to_utc(10, 6, 9, 0, "AM")
    scanner._windows = {scanner._bucket_key(end): PolyWindow(end, 0.40, 0.55, "q")}
    scanner._updated = time.monotonic()
    # Kalshi UP 0.40 + Poly DOWN 0.55 = 0.95 -> 2c locked after fee budget.
    arb = scanner.arb_vs_kalshi(end, 0.40)
    assert arb is not None
    assert arb["edge"] == pytest.approx(0.02)
    assert arb["legs"] == "kalshi_up+poly_down"


def test_no_arb_when_the_book_is_efficient():
    scanner = PolyScanner()
    end = et_to_utc(10, 6, 9, 0, "AM")
    scanner._windows = {scanner._bucket_key(end): PolyWindow(end, 0.50, 0.56, "q")}
    scanner._updated = time.monotonic()
    assert scanner.arb_vs_kalshi(end, 0.45) is None


def test_divergence_threshold():
    scanner = PolyScanner()
    end = et_to_utc(10, 6, 9, 0, "AM")
    scanner._windows = {scanner._bucket_key(end): PolyWindow(end, 0.60, 0.40, "q")}
    scanner._updated = time.monotonic()
    assert "gap +0.20" in (scanner.diverges_from(end, 0.40) or "")
    assert scanner.diverges_from(end, 0.55) is None


def test_dark_scanner_is_a_noop():
    scanner = PolyScanner()
    end = et_to_utc(10, 6, 9, 0, "AM")
    assert scanner.window_for(end) is None
    assert scanner.arb_vs_kalshi(end, 0.40) is None
    assert scanner.diverges_from(end, 0.40) is None


# ---------------------------------------------------------------------------
# Implied sigma and the hedge
# ---------------------------------------------------------------------------
def test_probit_is_the_normal_inverse():
    assert probit(0.975) == pytest.approx(1.96, abs=1e-3)
    assert probit(0.025) == pytest.approx(-1.96, abs=1e-3)
    assert probit(0.5) is None
    assert probit(0.0) is None


def test_implied_sigma_round_trips_the_diffusion():
    """Invert, then re-price: the same price must come back out."""
    sigma = 200.0
    price = 0.928
    back = implied_sigma(price, 84900.0, 84609.34, 890.0)
    assert back is not None
    # Re-price with the implied sigma and compare.
    t_scale = math.sqrt(890.0 / 900.0)
    z = (84900.0 - 84609.34) / (back * t_scale)
    from src.jobs.realized_vol import gaussian_cdf

    assert gaussian_cdf(z) == pytest.approx(price, abs=0.002)
    assert back == pytest.approx(sigma, rel=0.15)


def test_implied_sigma_rejects_degenerate_prices():
    assert implied_sigma(0.5, 84900.0, 84609.34, 300.0) is None
    assert implied_sigma(0.999, 84900.0, 84609.34, 300.0) is None
    assert implied_sigma("x", 84900.0, 84609.34, 300.0) is None


def test_hedge_size_scales_with_delta_and_shrinks_with_distance():
    # Near the target the binary is a coin flip: max delta, biggest hedge.
    q_atm, d_atm = hedge_btc_qty("up", 10, 20.0, 85000.0, 200.0, 60.0)
    q_far, _ = hedge_btc_qty("up", 10, 300.0, 85000.0, 200.0, 60.0)
    assert q_atm > q_far
    assert d_atm == "short"  # long YES -> short perp
    q_no, d_no = hedge_btc_qty("down", 10, 20.0, 85000.0, 200.0, 60.0)
    assert d_no == "long"  # long NO -> long perp
    assert q_no == pytest.approx(q_atm)
    assert hedge_btc_qty("up", 0, 20.0, 85000.0, 200.0, 60.0) == (0.0, "")


async def test_hyperliquid_mark_reads_the_public_mid():
    hl = HyperliquidMark()

    async def _fetch():
        return {"BTC": "85309.5"}

    await hl.refresh(fetch=_fetch)
    assert hl.mid == 85309.5


# ---------------------------------------------------------------------------
# Trader wiring
# ---------------------------------------------------------------------------
BUCKET = "26OCT011715"


def _dollars(**kw):
    return UpDownMarket(
        ticker=kw.pop("ticker", f"KXBTC15M-{BUCKET}-15"),
        event_ticker=f"KXBTC15M-{BUCKET}",
        bucket=kw.pop("bucket", BUCKET),
        horizon=15,
        title="BTC price up in next 15 mins?",
        target=kw.pop("target", 84609.34),
        close_ts=time.time() + kw.pop("seconds_left", 600),
        **kw,
    )


def _trader():
    spot = SpotFeed()
    spot.price = 84900.0
    spot.ts = time.time()
    spot.source = "test"
    return UpDownTrader(spot, Btc15mFeed(), UpDownConfig())


def test_reason_names_implied_sigma_when_both_curves_exist():
    trader = _trader()
    t0 = time.monotonic()
    for i in range(60):
        trader.rv.observe(84000.0 + 0.1 * i + (1.0 if i % 2 else -1.0), now=t0 - 300.0 + i * 5.0)
    market = _dollars(
        seconds_left=300, yes_bid=0.40, yes_ask=0.41, no_bid=0.59, no_ask=0.60
    )
    signal = trader.evaluate(market, live=False)
    assert signal is not None and signal.side
    assert "rv-sigma" in signal.reason
    # A 0.41 quote on a +$291 spot implies a huge sigma -> annotated either way.
    assert "impl-sigma" in signal.reason


def test_poly_guard_vetoes_when_the_other_venue_disagrees():
    trader = _trader()
    end = bucket_to_utc(BUCKET)
    trader.poly._windows = {
        PolyScanner._bucket_key(end): PolyWindow(end, 0.75, 0.25, "q")
    }
    trader.poly._updated = time.monotonic()
    market = _dollars(
        seconds_left=120, yes_bid=0.40, yes_ask=0.41, no_bid=0.59, no_ask=0.60
    )
    signal = trader.evaluate(market, live=False)
    assert signal is not None
    assert signal.side == ""
    assert "poly divergence" in signal.reason
    assert trader.book.skipped_poly_divergence == 1


def test_poly_arb_is_logged_when_the_two_venues_undercut_a_dollar():
    trader = _trader()
    end = bucket_to_utc(BUCKET)
    # Kalshi up ~0.41 (ask); Poly down 0.55 -> combined 0.96 -> arb.
    trader.poly._windows = {
        PolyScanner._bucket_key(end): PolyWindow(end, 0.44, 0.55, "q")
    }
    trader.poly._updated = time.monotonic()
    market = _dollars(
        seconds_left=120, yes_bid=0.40, yes_ask=0.41, no_bid=0.59, no_ask=0.60
    )
    signal = trader.evaluate(market, live=False)
    assert trader.poly_arb_last is not None
    assert signal is not None


def test_hedge_plan_is_annotated_when_mark_is_known():
    trader = _trader()
    t0 = time.monotonic()
    for i in range(60):
        trader.rv.observe(84000.0 + 0.1 * i + (1.0 if i % 2 else -1.0), now=t0 - 300.0 + i * 5.0)
    trader.hl.mid = 85000.0
    market = _dollars(
        seconds_left=120, yes_bid=0.40, yes_ask=0.41, no_bid=0.59, no_ask=0.60
    )
    signal = trader.evaluate(market, live=False)
    assert signal is not None and signal.side
    assert "hl-hedge:" in signal.reason


def test_vol_edge_disabled_restores_the_plain_reason():
    trader = _trader()
    trader.config.vol_edge_enabled = False
    market = _dollars(
        seconds_left=300, yes_bid=0.40, yes_ask=0.41, no_bid=0.59, no_ask=0.60
    )
    signal = trader.evaluate(market, live=False)
    assert signal is not None
    assert "impl-sigma" not in signal.reason
