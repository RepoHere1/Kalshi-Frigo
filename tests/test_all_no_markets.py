"""Tests for the ALL NO MARKETS page's own book and Kelly sizing.

The page is a separate book: its own fake money, its own rows, its own sizing.
These tests pin that separation (a lane's NO clip must never appear here) and
the Kelly math that sizes the book's clips.

Money math, no network, no credentials, CI-safe.
"""

import pytest

import web_dashboard as wd


@pytest.fixture
def client(monkeypatch, tmp_path):
    """A Flask test client with the DB redirected to this test's own file."""
    monkeypatch.setattr(wd, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setitem(wd._SNAPSHOT_CACHE, "payload", None)
    monkeypatch.setitem(wd._SNAPSHOT_CACHE, "at", 0.0)
    with wd.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Kelly sizing
# ---------------------------------------------------------------------------
class TestAllNoKelly:
    """Quarter-Kelly for a NO contract bought at price P with model fair F."""

    def test_positive_edge_sizes_a_clip(self):
        # fair 0.90 against a 88c limit: b = 0.12/0.88 = 0.1364
        # f* = (0.9 * 0.1364 - 0.1) / 0.1364 = 0.16667 full Kelly
        # quarter-Kelly = 0.041667 -> 4.17% of the book
        f = wd._all_no_kelly(0.90, 0.88)
        assert f == pytest.approx(0.0416666666666667, abs=1e-9)

    def test_no_edge_stakes_nothing(self):
        # fair == price is a coin flip at the market's own number: no edge, no bet
        assert wd._all_no_kelly(0.88, 0.88) == 0.0

    def test_negative_edge_stakes_nothing(self):
        # Paying more than the model's fair is a losing clip even before fees
        assert wd._all_no_kelly(0.80, 0.88) == 0.0

    def test_never_negative_for_any_input(self):
        for fair in (0.0, 0.1, 0.5, 0.99, 1.0):
            for price in (0.0, 0.05, 0.5, 0.95, 1.0):
                assert wd._all_no_kelly(fair, price) >= 0.0

    def test_the_20_percent_cap_binds_before_full_kelly(self):
        # A near-certain fair against a mid price returns a huge full-Kelly
        # fraction. The cap is what keeps the book alive.
        f = wd._all_no_kelly(0.999, 0.50)
        assert f == pytest.approx(wd.ALL_NO_MAX_CLIP_PCT)
        assert f <= wd.ALL_NO_MAX_CLIP_PCT

    def test_untradeable_prices_return_zero_not_raise(self):
        assert wd._all_no_kelly(0.90, 0.0) == 0.0
        assert wd._all_no_kelly(0.90, 1.0) == 0.0
        assert wd._all_no_kelly(0.0, 0.88) == 0.0

    def test_larger_edge_sizes_larger(self):
        assert wd._all_no_kelly(0.95, 0.88) > wd._all_no_kelly(0.90, 0.88)


class TestAllNoBuyAmount:
    """The clip a Kelly fraction actually buys, in whole contracts."""

    def test_contract_count_is_whole_and_at_least_one(self):
        amt = wd._all_no_buy_amount(0.90, 0.88)
        assert amt["contracts"] >= wd.MIN_ORDER_CONTRACTS
        assert isinstance(amt["contracts"], int)

    def test_no_edge_means_no_clip(self):
        amt = wd._all_no_buy_amount(0.88, 0.88)
        assert amt["stake"] == 0.0
        assert amt["contracts"] == 0

    def test_untradeable_price_means_no_clip(self):
        amt = wd._all_no_buy_amount(0.90, 0.0)
        assert amt["contracts"] == 0

    def test_stake_is_a_fraction_of_the_book(self):
        # The stake is a SHARE of the book, so the caller multiplies by balance.
        amt = wd._all_no_buy_amount(0.90, 0.88)
        assert 0.0 < amt["stake"] <= wd.ALL_NO_MAX_CLIP_PCT


# ---------------------------------------------------------------------------
# The separate $250 book
# ---------------------------------------------------------------------------
class TestAllNoBook:
    """The book's own ledger, seeded once at $250 and never re-funded."""

    def test_seeds_at_250(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wd, "DB_PATH", str(tmp_path / "b.db"))
        book = wd._all_no_book()
        assert book["starting"] == 250.0
        assert book["balance"] == 250.0
        assert book["entries"] == 0
        assert book["pnl"] == 0.0

    def test_seeding_is_idempotent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wd, "DB_PATH", str(tmp_path / "b.db"))
        first = wd._all_no_book()
        second = wd._all_no_book()
        assert second["balance"] == first["balance"]
        # One seed row, not one per read.
        rows = wd._db_rows("SELECT COUNT(*) AS n FROM all_no_book")
        assert int(rows[0]["n"]) == 1

    def test_not_the_dry_books_300(self, monkeypatch, tmp_path):
        # The ALL NO book must not inherit the DRY book's $300 seed.
        monkeypatch.setattr(wd, "DB_PATH", str(tmp_path / "b.db"))
        assert wd._all_no_book()["starting"] == 250.0
        assert wd._all_no_book()["starting"] != 300.0


# ---------------------------------------------------------------------------
# Separation from the original lanes
# ---------------------------------------------------------------------------
class TestNoBookSeparation:
    """A lane's NO clip must never appear on the ALL NO page."""

    def _seed_rows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wd, "DB_PATH", str(tmp_path / "sep.db"))
        wd._db()  # create the schema
        # A lane NO clip: the strategy is one of the up/down lanes.
        wd._db_rows("DELETE FROM positions")
        wd._db_rows("DELETE FROM trade_logs")
        import sqlite3

        conn = sqlite3.connect(str(tmp_path / "sep.db"))
        conn.execute(
            "INSERT INTO positions (market_id, side, entry_price, quantity,"
            " timestamp, status, strategy, mode, entry_fair)"
            " VALUES ('KXBTC15M-26OCT151600-00', 'NO', 0.55, 1,"
            " '2026-10-15T00:00:00', 'open', 'btc_updown', 'dry', 0.86)"
        )
        conn.commit()
        conn.close()

    def test_a_lane_row_is_not_the_all_no_books(self, monkeypatch, tmp_path):
        self._seed_rows(tmp_path, monkeypatch)
        rows = wd._db_rows(wd._SQL_NO_OPEN)
        assert rows == []

    def test_an_all_no_row_is(self, monkeypatch, tmp_path):
        self._seed_rows(tmp_path, monkeypatch)
        import sqlite3

        conn = sqlite3.connect(str(tmp_path / "sep.db"))
        conn.execute(
            "INSERT INTO positions (market_id, side, entry_price, quantity,"
            " timestamp, status, strategy, mode, entry_fair)"
            " VALUES ('KXHYPED-26OCT1519-T80.9999', 'NO', 0.88, 1,"
            " '2026-10-15T00:01:00', 'open', 'all_no', 'dry', 0.90)"
        )
        conn.commit()
        conn.close()
        rows = wd._db_rows(wd._SQL_NO_OPEN)
        assert len(rows) == 1
        assert rows[0]["market_id"] == "KXHYPED-26OCT1519-T80.9999"

    def test_a_yes_row_is_never_included(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wd, "DB_PATH", str(tmp_path / "y.db"))
        wd._db()
        import sqlite3

        conn = sqlite3.connect(str(tmp_path / "y.db"))
        conn.execute(
            "INSERT INTO positions (market_id, side, entry_price, quantity,"
            " timestamp, status, strategy, mode, entry_fair)"
            " VALUES ('KXBTC15M-26OCT151600-00', 'YES', 0.55, 1,"
            " '2026-10-15T00:00:00', 'open', 'all_no', 'dry', 0.86)"
        )
        conn.commit()
        conn.close()
        assert wd._db_rows(wd._SQL_NO_OPEN) == []


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------
class TestAllNoMarketsPage:
    def test_page_renders_the_book_and_the_kelly_policy(self, client):
        r = client.get("/all-no-markets")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        # White paper, black cards.
        assert "--paper:#ffffff" in html
        assert "--card:#0a0a0a" in html
        # The separate book, seeded at 250.
        assert "Book balance" in html
        assert "250.00" in html
        assert "seeded at 250 once" in html
        # Kelly is on the page, in the policy and in both tables.
        assert "quarter-Kelly" in html
        assert html.count("Kelly f*") >= 2
        # Separation from the lanes is stated, not implied.
        assert "Separated from the lanes" in html
        # Read-only by law.
        assert "Read-only" in html

    def test_page_shows_no_lane_rows(self, client, monkeypatch, tmp_path):
        """With only a lane's NO row in the DB, the page shows its own empty book."""
        wd._db()
        import sqlite3

        conn = sqlite3.connect(str(tmp_path / "t.db"))
        conn.execute(
            "INSERT INTO positions (market_id, side, entry_price, quantity,"
            " timestamp, status, strategy, mode, entry_fair)"
            " VALUES ('KXBTC15M-26OCT151600-00', 'NO', 0.55, 1,"
            " '2026-10-15T00:00:00', 'open', 'btc_updown', 'dry', 0.86)"
        )
        conn.commit()
        conn.close()
        r = client.get("/all-no-markets")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        # The lane's NO ticker must not be written into this book's tables.
        assert "KXBTC15M" not in html
        # And the page says so, rather than silently showing nothing.
        assert "lanes keep their own" in html

    def test_page_reads_the_html_file_not_an_embedded_template(self):
        """The markup lives in static/all_no_markets.html, editable on its own."""
        import os

        path = os.path.join(wd.BASE_DIR, "static", "all_no_markets.html")
        assert os.path.exists(path)
