import os

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
# Positions response shape
# ---------------------------------------------------------------------------
def test_a_bare_list_of_positions_is_accepted():
    """`/portfolio/positions` has answered as a dict and as a bare list.

    Read as a dict unconditionally, a list response raised
    `'list' object has no attribute 'get'` on every refresh - a permanent
    "Kalshi connect" error on the page, while the account panel went on
    rendering whatever had been cached.
    """
    rows = [
        {"ticker": "KXTEST-26", "event_ticker": ""},
        {"event_ticker": "KXSB-27", "event_ticker": "KXSB-27"},
    ]
    out = wd._normalise_positions(rows)
    assert out["market_positions"] == [{"ticker": "KXTEST-26", "event_ticker": ""}]
    assert out["event_positions"] == [{"event_ticker": "KXSB-27"}]


def test_the_dict_shape_still_passes_through():
    payload = {"market_positions": [1], "event_positions": [2]}
    assert wd._normalise_positions(payload) is payload


@pytest.mark.parametrize("payload", [None, {}, "unexpected", 7])
def test_junk_responses_degrade_to_an_empty_dict(payload):
    assert wd._normalise_positions(payload) == {}


def test_a_bare_list_balance_is_accepted():
    """/portfolio/balance drifts shape the same way positions does."""
    assert wd._normalise_balance([{"balance": 1234}]) == {"balance": 1234}
    assert wd._normalise_balance({"balance": 99}) == {"balance": 99}
    assert wd._normalise_balance([]) == {}
    assert wd._normalise_balance(None) == {}
    assert wd._normalise_balance(500) == {"balance": 500}


def test_the_sync_survives_either_shape(monkeypatch):
    """The whole point: a shape change must not abort the sync."""

    calls = {"n": 0}
    real = wd._run_async

    def fake(coro):
        # Only the Kalshi fetch is faked; the DB read behind the title lookup
        # still runs for real.
        if calls["n"] == 0:
            calls["n"] += 1
            coro.close()
            return (
                [{"balance": 2500}],
                {"market_positions": [{"ticker": "KX"}], "event_positions": []},
            )
        return real(coro)

    monkeypatch.setattr(wd, "_run_async", fake)
    monkeypatch.setitem(wd.dashboard_state, "balance", None)
    monkeypatch.setitem(wd.dashboard_state, "positions", [])
    monkeypatch.setattr(wd, "_refresh_market_titles", lambda _p: None)

    wd._refresh_kalshi()

    assert wd.dashboard_state["balance"] == 25.0
    assert wd.dashboard_state["positions"] == [{"ticker": "KX"}]


# ---------------------------------------------------------------------------
# Every LIVE table must have a cell for every column it declares
# ---------------------------------------------------------------------------
def _table_cells(html: str, tbody_id: str):
    import re

    seg = html[html.index(tbody_id) :]
    seg = seg[: seg.index("</tbody>")]
    row = re.search(r"<tr>(.*?)</tr>", seg, re.S)
    return len(re.findall(r"<td", row.group(1))) if row else 0


def _header_cells(html: str, tbody_id: str):
    import re

    head = html[: html.index(tbody_id)]
    tables = re.findall(r"<thead>(.*?)</thead>", head, re.S)
    return len(re.findall(r"<th", tables[-1])) if tables else 0


def test_live_tables_line_up_with_their_headers():
    """A missing <td> shifts every value one column left.

    The Kalshi markets table declared a Title column and emitted no cell for it,
    so Shares rendered under Title, Exposure under Shares, and the title never
    appeared at all - which reads as "the titles are wrong" rather than "the row
    is short a cell".
    """
    import re
    from pathlib import Path

    src = Path(wd.__file__).read_text(encoding="utf-8")

    for tbody_id in ("kalshiMarkets", "kalshiEvents"):
        marker = 'id="' + tbody_id + '"'
        assert marker in src, tbody_id
        at = src.index(marker)

        head = src[:at]
        header = re.findall(r"<thead>(.*?)</thead>", head, re.S)[-1]
        n_head = len(re.findall(r"<th", header))

        loop = src.find("{%- for r in s.kalshi.", at)
        row = src[loop : loop + 900]
        n_cells = len(re.findall(r"<td", row[: row.index("</tr>")]))

        assert n_cells == n_head, f"{tbody_id}: {n_cells} cells for {n_head} columns"


def test_every_kalshi_row_renders_a_title_cell():
    from pathlib import Path

    src = Path(wd.__file__).read_text(encoding="utf-8")
    for key in ("s.kalshi.markets", "s.kalshi.events"):
        loop = src.find("{%- for r in " + key + " %}")
        assert loop > 0, key
        row = src[loop : loop + 900]
        assert "_market_title(r.ticker)" in row[: row.index("</tr>")], (
            key + " renders no title cell"
        )
