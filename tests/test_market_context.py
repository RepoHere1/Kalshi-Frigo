"""Tests for the shared market context (range, calendar, headlines, breakout)."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from src.jobs import market_context as mc

NY = ZoneInfo("America/New_York")
# 2026-10-13 12:00:00 UTC, aligned to a five-minute boundary.
NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc).timestamp()
END = int(NOW // mc.BAR_S) * mc.BAR_S


def _fill(bars, start, high, low, count=48):
    for i in range(count):
        ts = int(start) + i * mc.BAR_S
        bars[ts] = mc.Bar(ts, high, low)


def _history(current=(101.0, 99.0), baseline=((101.0, 99.0),) * 5, end=END):
    bars = {}
    _fill(bars, end - mc.WINDOW_S, *current)
    for k, (hi, lo) in enumerate(baseline):
        _fill(bars, end - mc.WINDOW_S * (k + 2), hi, lo)
    return bars


# ---------------------------------------------------------------------------
# Four-hour range
# ---------------------------------------------------------------------------
def test_range_is_normal_when_width_matches_baseline():
    rc = mc.range_context(_history(), NOW, 100.0)
    assert rc.available and rc.regime == "normal"
    assert rc.compression_ratio == pytest.approx(1.0)
    assert rc.position_pct == pytest.approx(50.0)
    assert mc.range_modifiers(rc) == (0.0, 1.0, "")


def test_compressed_mid_range_adds_edge_and_cuts_size():
    rc = mc.range_context(_history(current=(100.5, 99.5)), NOW, 100.0)
    assert rc.regime == "compressed"
    assert mc.range_modifiers(rc) == (0.02, 0.75, "range compressed mid-range")


def test_compression_near_the_edge_is_not_penalised():
    rc = mc.range_context(_history(current=(100.5, 99.5)), NOW, 100.45)
    assert rc.regime == "compressed"
    assert mc.range_modifiers(rc) == (0.0, 1.0, "")


def test_expanding_range_adds_small_edge_and_cuts_size():
    rc = mc.range_context(_history(current=(102.0, 98.0)), NOW, 100.0)
    assert rc.regime == "expanding"
    assert mc.range_modifiers(rc) == (0.01, 0.75, "range expanding")


def test_live_partial_bar_never_counts_toward_the_range():
    bars = _history()
    bars[END] = mc.Bar(END, 500.0, 1.0)  # the live, still-forming bar
    rc = mc.range_context(bars, NOW, 100.0)
    assert rc.available and rc.high == pytest.approx(101.0)


def test_incomplete_current_window_is_unavailable():
    bars = _history()
    for ts in list(bars):
        if ts >= END - 10 * mc.BAR_S:
            del bars[ts]  # leaves 38 of 48 current bars
    rc = mc.range_context(bars, NOW, 100.0)
    assert not rc.available and "incomplete" in rc.reason


def test_fewer_than_four_valid_baseline_windows_is_unavailable():
    bars = _history(baseline=((101.0, 99.0),) * 3)
    rc = mc.range_context(bars, NOW, 100.0)
    assert not rc.available and "baseline" in rc.reason


def test_one_invalid_baseline_window_is_excluded_not_fatal():
    bars = _history(baseline=((101.0, 99.0),) * 4 + ((101.0, 99.0),))
    for ts in list(bars):
        if END - 8 * 3600 <= ts < END - 4 * 3600 and ts % 3 == 0:
            del bars[ts]  # first baseline window drops below 39 bars
    rc = mc.range_context(bars, NOW, 100.0)
    assert rc.available


def test_parse_candles_keeps_settled_bars_in_the_trailing_day_and_overwrites_duplicates():
    rows = [
        [NOW - 24 * 3600 - 600, 99, 101, 100, 100, 1],  # outside 24h
        [NOW - 900, 98, 102, 100, 100, 1],  # settled
        [NOW - 900, 97, 103, 100, 100, 1],  # duplicate: later copy wins
        [NOW - 300, 98, 102, 100, 100, 1],  # still forming: excluded
        [NOW + 300, 98, 102, 100, 100, 1],  # future bar
        [NOW - 1200, 99, 98, 100, 100, 1],  # high below low: rejected
    ]
    bars = mc.parse_candles(rows, NOW)
    assert list(bars) == [int(NOW - 900)]
    assert bars[int(NOW - 900)].high == 103


# ---------------------------------------------------------------------------
# Breakout state machine
# ---------------------------------------------------------------------------
def _rc(high=101.0, low=99.0):
    return mc.RangeContext(True, high, low, 50.0, 1.0, "normal", "")


def test_breakout_confirms_only_after_thirty_seconds_beyond_the_boundary():
    tracker = mc.BreakoutTracker()
    t0 = 1000.0
    assert tracker.update(t0, 101.2, 0.0, _rc()) == 0
    assert tracker.update(t0 + 29, 101.2, 0.0, _rc()) == 0
    assert tracker.update(t0 + 31, 101.2, 0.0, _rc()) == 1


def test_breakout_resets_when_price_returns_inside_the_frozen_range():
    tracker = mc.BreakoutTracker()
    t0 = 1000.0
    tracker.update(t0, 101.2, 0.0, _rc())
    tracker.update(t0 + 31, 101.2, 0.0, _rc())
    assert tracker.update(t0 + 40, 100.0, 0.0, _rc()) == 0
    assert tracker.frozen is None


def test_stale_spot_resets_the_pending_timer():
    tracker = mc.BreakoutTracker()
    t0 = 1000.0
    tracker.update(t0, 101.2, 0.0, _rc())
    tracker.update(t0 + 20, 101.2, 9.0, _rc())  # stale feed
    assert tracker.update(t0 + 40, 101.2, 0.0, _rc()) == 0
    assert tracker.update(t0 + 75, 101.2, 0.0, _rc()) == 1


def test_confirmed_breakout_expires_and_latches_until_reentry():
    tracker = mc.BreakoutTracker()
    t0 = 1000.0
    tracker.update(t0, 101.2, 0.0, _rc())
    assert tracker.update(t0 + 31, 101.2, 0.0, _rc()) == 1
    assert tracker.update(t0 + 31 + mc.BREAK_ACTIVE_S + 1, 101.2, 0.0, _rc()) == 0
    assert tracker.latched
    assert tracker.update(t0 + 4000, 101.2, 0.0, _rc()) == 0  # still outside: no re-arm
    tracker.update(t0 + 4010, 100.0, 0.0, _rc())  # back inside clears the latch
    assert not tracker.latched


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------
ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Gross Domestic Product\\, 4th Quarter and Year 2025 (Advance Estima
 te)
DTSTART;VALUE=DATE-TIME:20260129T133000Z
END:VEVENT
BEGIN:VEVENT
SUMMARY:Personal Income and Outlays\\, December 2026
DTSTART;VALUE=DATE-TIME:20260130T133000Z
END:VEVENT
BEGIN:VEVENT
SUMMARY:U.S. International Trade in Goods and Services\\, November 2024
DTSTART;VALUE=DATE-TIME:20260107T133000Z
END:VEVENT
END:VCALENDAR
"""


def test_bea_ics_keeps_mapped_releases_and_unfolds_titles():
    events = mc.parse_bea_ics(ICS)
    assert [e.tier for e in events] == ["high", "high"]
    assert events[0].title == "Gross Domestic Product, 4th Quarter and Year 2025 (Advance Estimate)"
    assert events[0].ts == datetime(2026, 1, 29, 13, 30, tzinfo=timezone.utc).timestamp()


def test_timed_event_window_is_inclusive_at_both_ends():
    ev = mc.Event("BEA", "GDP", "high", ts=NOW + 900)  # 15 minutes ahead
    assert mc.event_modifier([ev], NOW)[0] == "high"  # exactly pre-window start
    assert mc.event_modifier([ev], NOW - 1)[0] is None  # one second before
    late = mc.Event("BEA", "GDP", "high", ts=NOW - 600)  # 10 minutes ago
    assert mc.event_modifier([late], NOW)[0] == "high"  # exactly post-window end
    assert mc.event_modifier([late], NOW + 1)[0] is None


def test_overlapping_events_apply_the_highest_tier_once():
    high = mc.Event("BEA", "GDP", "high", ts=NOW)
    medium = mc.Event("BEA", "GDP second", "medium", ts=NOW)
    tier, edge, size, _ = mc.event_modifier([medium, high], NOW)
    assert (tier, edge, size) == ("high", 0.02, 0.50)


def test_fomc_date_only_risk_covers_the_whole_new_york_day():
    fomc = mc.Event("FED", "FOMC meeting (final day)", "high", day=date(2026, 1, 28))
    noon = datetime(2026, 1, 28, 12, 0, tzinfo=NY).timestamp()
    next_day = datetime(2026, 1, 29, 0, 30, tzinfo=NY).timestamp()
    tier, _, _, text = mc.event_modifier([fomc], noon)
    assert tier == "high" and "date-level" in text
    assert mc.event_modifier([fomc], next_day)[0] is None


def test_parse_fomc_meetings_reads_final_days_and_ignores_minutes():
    html = """
    <h4>2026 FOMC Meetings</h4>
    <div>January 27-28 Statement: PDF</div>
    <div>March 17-18 Statement</div>
    <div>April 28-May 1 Statement</div>
    <div>February 18 Minutes</div>
    <h4>2027 FOMC Meetings</h4>
    <div>January 26-27</div>
    """
    days = [e.day for e in mc.parse_fomc_meetings(html)]
    assert days == [date(2026, 1, 28), date(2026, 3, 18), date(2026, 5, 1), date(2027, 1, 27)]


def test_classify_event_maps_only_the_approved_titles():
    assert mc.classify_event("Gross Domestic Product, 2nd Quarter (Second Estimate)") == "medium"
    assert mc.classify_event("Gross Domestic Product, 1st Quarter (Advance Estimate)") == "high"
    assert mc.classify_event("U.S. International Trade in Goods") is None


# ---------------------------------------------------------------------------
# Headlines
# ---------------------------------------------------------------------------
def _h(sent=0.8, conf=0.9, rel=1.0, age=0.0, url="u"):
    return mc.Headline(url, "t", NOW - age, rel, "src", sent, conf)


def test_relevance_separates_asset_terms_macro_and_noise():
    assert mc.relevance("Gold rallies on bullion demand", "PAXG-USD") == 1.0
    assert mc.relevance("Fed signals a pause", "PAXG-USD") == 0.8
    assert mc.relevance("Bitcoin ETF inflows", "PAXG-USD") == 0.0
    assert mc.relevance("Ethereum upgrade ships", "BTC-USD") == 0.0


def test_news_score_weights_by_freshness_and_bounds_the_delta():
    items = [_h(sent=0.8, conf=0.9, age=0.0), _h(sent=0.4, conf=0.8, age=450.0, url="v")]
    res = mc.score_headlines(items, NOW)
    expected = (0.9 * 0.8 * 1.0 + 0.8 * 0.4 * 0.5) / (1.0 + 0.5)
    assert res.status == "headline_bias"
    assert res.score == pytest.approx(expected)
    assert res.delta == pytest.approx(0.02 * expected)


def test_news_excludes_stale_low_confidence_and_low_relevance_items():
    items = [
        _h(conf=0.7),
        _h(rel=0.6, url="b"),
        _h(age=900.0, url="c"),
        _h(age=-120.0, url="d"),
    ]
    assert mc.score_headlines(items, NOW).status == "no_fresh_headlines"


def test_news_tolerates_a_small_clock_skew_only():
    assert mc.score_headlines([_h(age=-30.0)], NOW).status == "headline_bias"


def test_opposing_headlines_are_conflicting_with_no_adjustment():
    items = [_h(sent=0.9, url="a"), _h(sent=-0.9, url="b")]
    res = mc.score_headlines(items, NOW)
    assert res.status == "conflicting_headlines" and res.delta == 0.0


def test_weak_sentiment_is_neutral():
    res = mc.score_headlines([_h(sent=0.1)], NOW)
    assert res.status == "neutral_headlines" and res.delta == 0.0


def test_sentiment_output_must_cite_supplied_urls_and_valid_ranges():
    raw = (
        'Result: [{"source_url": "u1", "sentiment": 0.5, "confidence": 0.8},'
        ' {"source_url": "evil", "sentiment": 1, "confidence": 1},'
        ' {"source_url": "u2", "sentiment": 2, "confidence": 0.5}] thanks'
    )
    assert mc.parse_sentiment(raw, ["u1", "u2"]) == {"u1": (0.5, 0.8)}
    assert mc.parse_sentiment("no json here", ["u1"]) == {}


def test_rss_parsing_keeps_only_dated_relevant_items():
    xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title>Gold rises as Fed signals pause</title><link>https://x/1</link>
      <pubDate>Tue, 13 Oct 2026 12:00:00 GMT</pubDate></item>
    <item><title>Bitcoin ETF inflows</title><link>https://x/2</link>
      <pubDate>Tue, 13 Oct 2026 12:00:00 GMT</pubDate></item>
    <item><title>Gold miners</title><link>https://x/3</link></item>
    </channel></rss>"""
    items = mc.parse_feed(xml, "PAXG-USD", "feed")
    assert [h.url for h in items] == ["https://x/1"]
    assert items[0].published_ts == NOW


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def test_kill_switch_yields_a_neutral_state(monkeypatch):
    monkeypatch.setenv("MARKET_CONTEXT_ENABLED", "0")
    ctx = mc.MarketContext("PAXG-USD")
    state = ctx.state(NOW, 100.0, 0.0, 0.5)
    assert not state.enabled
    assert (state.edge_add, state.size_mult) == (0.0, 1.0)


def _candle_rows(now=NOW, end=END, low=99.0, high=101.0):
    return [[ts, low, high, 100, 100, 1] for ts in range(int(end) - 24 * 3600, int(end), 300)]


def test_refresh_then_state_combines_range_calendar_and_cited_news():
    feed = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title>Gold climbs as bullion demand rises</title><link>https://x/1</link>
      <pubDate>Tue, 13 Oct 2026 12:00:00 GMT</pubDate></item></channel></rss>"""
    ics = ICS.replace("20260129T133000Z", "20261013T121500Z")  # release 15m ahead

    async def fake_get(url, params):
        if "candles" in url:
            return json.dumps(_candle_rows())
        if url.endswith(".ics"):
            return ics
        if "fomc" in url:
            return "<h4>2026 FOMC Meetings</h4><div>October 1-2</div>"
        return feed if "coindesk" in url else (_ for _ in ()).throw(RuntimeError("down"))

    async def classify(prompt):
        assert "https://x/1" in prompt and "untrusted" in prompt
        return json.dumps([{"source_url": "https://x/1", "sentiment": 0.8, "confidence": 0.9}])

    ctx = mc.MarketContext(
        "PAXG-USD",
        enabled=True,
        clock=lambda: NOW,
        http_get=fake_get,
        classify=classify,
        feeds=("https://www.coindesk.com/arc/outboundfeeds/rss/", "https://dead.example/rss"),
    )
    ctx._first_due = 0.0
    asyncio.run(ctx.refresh_now())
    state = ctx.state(NOW, 100.0, 0.0, 0.5)

    assert not hasattr(state, "fair_delta")
    assert state.edge_add == pytest.approx(0.02)
    assert state.size_mult == pytest.approx(0.50)
    assert state.event_tier == "high"
    assert state.calendar_status == "fresh"
    assert "headline bias" in state.text and "calendar high" in state.text
    assert "dead.example" in ctx.last_error


def test_sentiment_failure_adds_no_headline_risk():
    async def fake_get(url, params):
        if "candles" in url:
            return json.dumps(_candle_rows())
        if url.endswith(".ics"):
            return ICS
        if "fomc" in url:
            return ""
        return """<?xml version="1.0"?><rss version="2.0"><channel><item>
        <title>Gold climbs</title><link>https://x/9</link>
        <pubDate>Tue, 13 Oct 2026 12:00:00 GMT</pubDate></item></channel></rss>"""

    async def broken(prompt):
        raise RuntimeError("model down")

    ctx = mc.MarketContext(
        "PAXG-USD", enabled=True, clock=lambda: NOW,
        http_get=fake_get, classify=broken,
        feeds=("https://www.coindesk.com/arc/outboundfeeds/rss/",),
    )
    ctx._first_due = 0.0
    asyncio.run(ctx.refresh_now())
    state = ctx.state(NOW, 100.0, 0.0, 0.5)
    assert "headlines no_fresh_headlines" in state.text
    assert "sentiment" in ctx.last_error


def _drop_current(bars, count):
    current = sorted(ts for ts in bars if END - mc.WINDOW_S <= ts < END)
    for ts in current[:count]:
        del bars[ts]


def test_thirty_nine_bars_is_valid_and_thirty_eight_is_not():
    bars = _history()
    _drop_current(bars, 9)  # 39 of 48 remain
    assert mc.range_context(bars, NOW, 100.0).available
    bars = _history()
    _drop_current(bars, 10)  # 38 of 48 remain
    assert not mc.range_context(bars, NOW, 100.0).available


def test_breakout_candidate_does_not_start_on_stale_spot():
    tracker = mc.BreakoutTracker()
    assert tracker.update(1000.0, 101.2, 60.0, _rc()) == 0
    assert tracker.frozen is None


def test_calendar_that_parses_to_nothing_is_unavailable_not_fresh():
    async def fake_get(url, params):
        if url.endswith(".ics"):
            return "BEGIN:VCALENDAR\nEND:VCALENDAR"
        return "<html>maintenance</html>"

    ctx = mc.MarketContext(
        "PAXG-USD", enabled=True, clock=lambda: NOW,
        http_get=fake_get, classify=None, feeds=(),
    )
    ctx._first_due = 0.0
    asyncio.run(ctx.refresh_now())
    state = ctx.state(NOW, 100.0, 0.0, 0.5)
    assert state.calendar_status == "unavailable"
    assert state.edge_add == pytest.approx(mc.UNKNOWN_CALENDAR_EDGE)


def test_state_faults_degrade_to_base_scoring(monkeypatch):
    ctx = mc.MarketContext("PAXG-USD", enabled=True, clock=lambda: NOW, classify=None)

    def boom(*_args, **_kwargs):
        raise RuntimeError("bad bars")

    monkeypatch.setattr(mc, "range_context", boom)
    state = ctx.state(NOW, 100.0, 0.0, 0.5)
    assert state.enabled
    assert (state.edge_add, state.size_mult) == (0.0, 1.0)
    assert "internal error" in state.text
    assert "state:" in ctx.last_error


def test_conflicting_headlines_raise_the_bar_without_moving_price():
    ctx = mc.MarketContext("PAXG-USD", enabled=True, clock=lambda: NOW, classify=None)
    ctx._articles = {
        "feed": (NOW, [
            mc.Headline("https://x/a", "Gold surges", NOW - 60, 1.0, "feed"),
            mc.Headline("https://x/b", "Gold slumps", NOW - 60, 1.0, "feed"),
        ]),
    }
    ctx._sentiment = {"https://x/a": (0.9, 0.9), "https://x/b": (-0.9, 0.9)}
    ctx._events = {
        "BEA": (NOW, [mc.Event("BEA", "later release", "medium", ts=NOW + 10**6)]),
        "FED": (NOW, [mc.Event("FED", "later meeting", "high", day=date(2030, 1, 1))]),
    }
    state = ctx.state(NOW, 100.0, 0.0, 0.5)
    assert state.calendar_status == "fresh"
    assert state.edge_add == pytest.approx(mc.NEWS_CONFLICT_EDGE)
    assert state.size_mult == 1.0
    assert "headlines conflicting" in state.text
