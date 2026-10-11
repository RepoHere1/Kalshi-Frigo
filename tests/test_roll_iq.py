"""Roll-forward IQ: the next-window read and its venue gating."""

import time

from src.jobs.market_data import UpDownMarket
from src.jobs.roll_iq import (
    RollTracker,
    drift_read,
    next_window_market,
    window_seconds_for_series,
)


def _market(ticker, event, target, close_in_sec, **kw):
    return UpDownMarket(
        ticker=ticker,
        event_ticker=event,
        bucket=event.split("-")[-1],
        horizon=kw.pop("horizon", 0),
        title="",
        target=target,
        close_ts=time.time() + close_in_sec,
        yes_bid=kw.pop("yes_bid", 0.49),
        yes_ask=kw.pop("yes_ask", 0.52),
        no_bid=kw.pop("no_bid", 0.47),
        no_ask=kw.pop("no_ask", 0.50),
        **kw,
    )


def test_window_seconds_come_from_the_series_name():
    assert window_seconds_for_series("KXBTC15M") == 900.0
    assert window_seconds_for_series("KXXRP15M") == 900.0
    assert window_seconds_for_series("KXBTCD") == 3600.0  # hourly ladder
    assert window_seconds_for_series("KXETHD") == 3600.0
    assert window_seconds_for_series("KXXRPD") == 3600.0
    assert window_seconds_for_series("KXBTC1H") == 3600.0  # legacy phantom
    assert window_seconds_for_series("KXUNKNOWN") is None


def test_drift_up_reads_up():
    now = 1_000_000.0
    samples = [(now - 300.0, 100.0), (now - 150.0, 103.0), (now, 106.0)]
    read = drift_read(samples, 300.0, now, 900.0, sigma_win=50.0)
    assert read.side == "up"
    assert read.p_up > 0.56
    assert read.drift_frac > 0


def test_drift_down_reads_down():
    now = 1_000_000.0
    samples = [(now - 300.0, 100.0), (now - 100.0, 96.5), (now, 94.0)]
    read = drift_read(samples, 300.0, now, 900.0, sigma_win=50.0)
    assert read.side == "down"
    assert read.p_up < 0.44


def test_flat_tape_reads_nothing():
    now = 1_000_000.0
    samples = [(now - 300.0, 100.0), (now - 10.0, 100.1)]
    read = drift_read(samples, 300.0, now, 900.0, sigma_win=50.0)
    assert read.side == ""


def test_cold_read_is_silent():
    read = drift_read([], 300.0, 1_000_000.0, 900.0, sigma_win=50.0)
    assert read.side == ""


def test_big_sigma_humbles_the_drift():
    # A strong 15-minute trend, but hourly vol swamps it -> no call.
    now = 1_000_000.0
    samples = [(now - 300.0, 100.0), (now, 106.0)]
    read = drift_read(samples, 300.0, now, 3600.0, sigma_win=400.0)
    assert read.side == ""


def test_no_listing_yet_means_no_roll():
    now = time.time()
    cur = _market("A-1", "EV-1", 100.0, 60)
    assert next_window_market([cur], 900.0, now, 101.0) is None


def test_successor_listing_is_the_roll_target():
    now = time.time()
    cur = _market("A-1", "EV-1", 100.0, 60)
    nxt = _market("B-2", "EV-2", 100.0, 960)
    got = next_window_market([cur, nxt], 900.0, now, 101.0)
    assert got is not None and got.ticker == "B-2"


def test_far_ahead_listings_are_not_the_next_window():
    now = time.time()
    cur = _market("L-1", "EV-A", 100.0, 60)
    far = _market("L-F", "EV-F", 100.0, 86400)  # 24h away: not adjacent
    assert next_window_market([cur, far], 3600.0, now, 101.0) is None


def test_ladder_picks_the_strike_nearest_spot():
    now = time.time()
    cur = _market("L-1", "EV-A", 100.0, 60)
    s1 = _market("L-T101", "EV-B", 101.0, 3660)
    s2 = _market("L-T130", "EV-B", 130.0, 3660)
    s3 = _market("L-T107", "EV-B", 107.0, 3660)
    got = next_window_market([cur, s3, s2, s1], 3600.0, now, 106.0)
    assert got is not None and got.ticker == "L-T107"


def test_unquoted_successor_is_not_buyable():
    now = time.time()
    cur = _market("A-1", "EV-1", 100.0, 60)
    nxt = _market("B-2", "EV-2", 100.0, 960, yes_bid=None, yes_ask=None)
    assert next_window_market([cur, nxt], 900.0, now, 101.0) is None


def test_tracker_limits_one_clip_per_event():
    tr = RollTracker("KXBTC15M")
    assert tr.window_sec == 900.0
    assert not tr.already("EV-9")
    tr.mark("EV-9", "up")
    assert tr.already("EV-9")
    tr.prune([_market("A-1", "EV-8", 100.0, 60)])
    assert not tr.already("EV-9"), "settled events rotate off the open feed"