"""Readable market titles, and per-strategy card attribution.

Two related faults made the dashboard look broken:

1. Every market showed its raw ticker (`KXDJIA-26DEC31-54000`) in the Title
   column. The English title comes from the Kalshi markets endpoint, which the
   dashboard's own polling pushes into HTTP 429, so the lookup usually failed and
   the code fell back to echoing the ticker. `_prettify_ticker` decodes what is
   reliably decodable and is used whenever the API did not supply a title.

2. Every strategy card read 0 trades / $0.00 realized / 0 open while the bot was
   demonstrably trading. The AI directional strategy records its rows under the
   name of the routine that produced them (`immediate_portfolio_optimization`),
   which was not in the alias table, so its entire history landed in
   "unattributed".
"""
import pytest

import web_dashboard as wd


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ticker,expected",
    [
        (
            "KXDJIA-26DEC31-54000",
            "Dow Jones Industrial Average above 54,000 · Dec 31 2026",
        ),
        ("KXSB-27-LV", "Super Bowl · LV · 2027"),
        ("KXWNBA-26-LV", "NBA Championship · LV · 2026"),
        ("KXNFLAFCCHAMP-27-LV", "NFL AFC Championship · LV · 2027"),
        ("KXBTC15M-26OCT022215-15", "BTC 15-minute · Oct 2 2026 22:15"),
        (
            "KXNASDAQ-26DEC31-20000",
            "Nasdaq 100 above 20,000 · Dec 31 2026",
        ),
        ("KXMLB-26-NYY", "MLB · NYY · 2026"),
        ("KXMLB-26", "MLB · 2026"),
    ],
)
def test_tickers_decode_to_readable_titles(ticker, expected):
    assert wd._prettify_ticker(ticker) == expected


@pytest.mark.parametrize("ticker", ["garbage", "", "PLAIN", "KX-"])
def test_undecodable_tickers_come_back_unchanged(ticker):
    """A wrong label is worse than the ticker it replaced."""
    assert wd._prettify_ticker(ticker) == (ticker or "")


def test_a_real_api_title_wins_over_the_prettifier():
    wd.dashboard_state["market_titles"]["KXTEST-26"] = "Will LA hit 100 degrees today?"
    wd.dashboard_state["market_titles"]["KXMLB-26-NYY"] = "KXMLB-26-NYY"
    try:
        assert wd._market_title("KXTEST-26") == "Will LA hit 100 degrees today?"
        # A failed lookup caches the ticker itself; the prettifier still beats
        # echoing that back at the user.
        assert wd._market_title("KXMLB-26-NYY") == "MLB · NYY · 2026"
    finally:
        wd.dashboard_state["market_titles"].clear()


def test_no_title_leaks_the_raw_ticker_when_it_can_be_decoded():
    for ticker in ("KXDJIA-26DEC31-54000", "KXSB-27-LV", "KXMLB-26-NYY"):
        assert wd._market_title(ticker) != ticker


# ---------------------------------------------------------------------------
# Card attribution
# ---------------------------------------------------------------------------
def test_internal_strategy_names_reach_the_right_card():
    """ai_directional books its rows as immediate_portfolio_optimization."""
    assert wd._strategy_bucket("immediate_portfolio_optimization") == "ai_directional"
    assert wd._strategy_bucket("portfolio_optimization") == "ai_directional"
    assert wd._strategy_bucket("btc_updown") == "btc_updown"
    assert wd._strategy_bucket("ai_directional") == "ai_directional"


def test_every_button_has_a_card_and_a_command():
    for name in wd.STRATEGY_COMMANDS:
        assert name in wd.strategy_state, f"{name} has no card"
        assert wd.STRATEGY_DOCS.get(name), f"{name} has no description"


def test_no_strategy_command_runs_without_loop():
    """A command with no --loop runs one pass and exits - the button then reads
    'exited on its own' forever."""
    for name, cmd in wd.STRATEGY_COMMANDS.items():
        assert "--paper" in cmd, f"{name} is not pinned to paper"
        assert "--loop" in cmd, f"{name} would exit after a single pass"
