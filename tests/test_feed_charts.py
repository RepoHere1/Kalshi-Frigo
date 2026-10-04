"""The feed charts must be readable at a glance.

Two things made them not:

1. A fixed amber line. Direction - the single most important thing a price chart
   conveys - had to be read off the slope against a grid. The convention every
   price chart uses is green when the window closed higher, red when lower.

2. One shared time window. The spot chart is context ("where is BTC today"), the
   15-minute contract chart is a quarter-hour ("where is it versus this
   contract's target"). Sharing a window meant the contract chart spent most of
   its width on history from contracts that had already settled.

These assert the constants and the rule, against the rendered page, because the
chart code is JavaScript embedded in a Jinja template.
"""
import pytest

import web_dashboard as wd


@pytest.fixture()
def html(monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "DB_PATH", str(tmp_path / "charts.db"))
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path / "logs")
    wd.app.config["TESTING"] = True
    return wd.app.test_client().get("/").get_data(as_text=True)


def test_the_two_charts_have_their_own_windows(html):
    assert "SPOT_WINDOW = 86400" in html, "spot should read as a day"
    assert "K15_WINDOW = 900" in html, "the 15-minute contract should read as a quarter-hour"


def test_spot_is_coloured_by_direction_not_a_fixed_amber(html):
    assert "'#2ee6a8'" in html  # green, closing higher
    assert "'#a10000'" in html  # blood red, closing lower
    assert "label: 'BTC-USD spot'," not in html, "the fixed-amber series is gone"


def test_a_flat_or_short_series_stays_neutral_rather_than_lying(html):
    """One point cannot show direction; it must not claim to."""
    assert "if (clean.length < 2) return" in html


def test_the_target_line_stays_neutral(html):
    """The target is a reference, not a direction - it must not read up or down."""
    assert "label: 'target', borderColor: '#8fa3bd'" in html


def test_chart_text_is_legible(html):
    """9-10px grey ticks on a dark panel are not readable.

    Scoped to the feed charts: the per-card sparklines are deliberately minimal
    and share no options with them.
    """
    feed_opts = html[html.index("function drawFeed") : html.index("function paintMarket")]
    assert "font: { size: 9 }" not in feed_opts
    assert "font: { size: 10 }" not in feed_opts
    assert "boxWidth: 12, font: { size: 12 }" in feed_opts
    assert "'#5b6a80'" not in feed_opts, "tick grey is too dim against the panel"
    # Card figures and labels.
    assert "font-size:16px;font-weight:640" in html
    assert "font-size:10.5px" in html


def test_negative_figures_use_true_red(html):
    """#ff5c7a is pink. Losses must read as the blood red they are."""
    assert "--down:#a10000" in html
    assert "--down:#ff5c7a" not in html


def test_the_trend_rule_is_direction_of_the_visible_window():
    """Documented behaviour: last vs first of whatever is on screen."""

    def trend(values):
        clean = [v for v in values if v is not None]
        if len(clean) < 2:
            return "neutral"
        return "up" if clean[-1] >= clean[0] else "down"

    assert trend([100, 101, 102]) == "up"
    assert trend([102, 101, 100]) == "down"
    assert trend([100, 100]) == "up", "flat is not a fall"
    assert trend([]) == "neutral"
    assert trend([None]) == "neutral"


def test_the_window_filter_keeps_only_recent_points():
    now = 1_000_000.0

    def within(points, secs):
        return [p for p in points if now - p["t"] <= secs]

    pts = [
        {"t": now - 60, "p": 1.0},  # inside 15 minutes
        {"t": now - 600, "p": 2.0},  # inside 15 minutes
        {"t": now - 1200, "p": 3.0},  # outside 15 minutes, inside a day
    ]
    assert len(within(pts, 900)) == 2
    assert len(within(pts, 86400)) == 3


def test_the_feed_can_still_render_without_chartjs(html):
    """A CDN failure must not stop the numbers updating - it did before."""
    assert "const CHARTS_OK = typeof Chart !== 'undefined';" in html
    assert "if (typeof Chart === 'undefined') return;" in html


def test_charts_still_guard_each_construction_site(html):
    assert html.count("typeof Chart === 'undefined'") >= 3


# ---------------------------------------------------------------------------
# Supervisor: the two vocabularies for "paper"
# ---------------------------------------------------------------------------
def _supervisor_source() -> str:
    """The supervisor is Python, so it is asserted against source, not the page."""
    import inspect

    return inspect.getsource(wd._strategy_supervisor_loop)


def test_the_supervisor_accepts_both_spellings_of_paper():
    """Resuming after a deploy dropped five of six strategies.

    The runtime store records `paper`; the book says `dry`. The old supervisor
    matched one exact spelling, treated every row carrying the other as LIVE,
    cleared the operator's intent, and left the strategy down after every
    deploy. That whole vocabulary check is gone now: the supervisor no longer
    inspects the recorded mode spelling at all, so there is no string it can
    get wrong.
    """
    src = _supervisor_source()
    assert 'not in ("paper", "dry")' not in src
    assert "row.get('mode')" not in src


def test_live_lanes_are_resumed_like_any_other_lane():
    """A LIVE strategy is resumed across a restart, exactly like a paper one.

    The old rule dropped LIVE intent on every redeploy "for safety", which is
    what turned the whole book off overnight: every deploy silently stopped the
    armed strategies and the account sat idle. An armed lane means the operator
    already said yes; that intent now survives the process that recorded it,
    and only a Stop given in the current instance takes a lane down.
    """
    src = _supervisor_source()
    assert "was running LIVE before a restart and was not" not in src
    assert "_CUR_INSTANCE" in src


def test_the_supervisor_manages_only_the_current_book():
    """DRY and LIVE are separate books with separate runtime rows.

    The supervisor reads and writes one book's rows only, so a Stop on the
    LIVE page can never clear DRY's intent or kill DRY's process.
    """
    src = _supervisor_source()
    assert "book_mode = _runtime_mode()" in src
    assert "store.desired(mode=book_mode)" in src
    assert "store.snapshot(mode=book_mode)" in src


def test_a_recovered_strategy_resets_the_failure_budget():
    """A brief outage must not permanently disable a strategy."""
    src = _supervisor_source()
    assert "_SUPERVISOR_STABLE_SECONDS" in src
    assert "recovery confirmed" in src
