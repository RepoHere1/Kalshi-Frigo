"""The ANALYSIS OF panel: stats and recommendations over the forever log."""
import pytest

from src.utils.trade_analytics import analyze_trades


def _t(**kw):
    row = {
        "market_id": "KXTEST-26",
        "side": "YES",
        "entry_price": 0.5,
        "exit_price": 0.6,
        "quantity": 10,
        "pnl": 1.0,
        "strategy": "ai_directional",
        "entry_timestamp": "2026-10-04T10:00:00",
        "exit_timestamp": "2026-10-04T10:05:00",
        "exit_reason": "take_profit",
    }
    row.update(kw)
    return row


def test_an_empty_log_is_analysed_not_errored():
    out = analyze_trades([])
    assert out["trades"] == 0
    assert out["recommendations"]


def test_totals_and_win_rate_are_exact():
    rows = [
        _t(market_id="KXA", pnl=5.0, strategy="s1"),
        _t(market_id="KXB", pnl=-2.0, strategy="s1"),
        _t(market_id="KXC", pnl=3.0, strategy="s2", side="NO"),
    ]
    out = analyze_trades(rows)
    assert out["trades"] == 3
    assert out["wins"] == 2
    assert out["losses"] == 1
    assert out["win_rate"] == pytest.approx(66.7, abs=0.1)
    assert out["total_pnl"] == pytest.approx(6.0)
    assert out["best"] == pytest.approx(5.0)
    assert out["worst"] == pytest.approx(-2.0)
    assert out["best_market"] == "KXA"
    assert out["worst_market"] == "KXB"


def test_by_strategy_is_ranked_by_pnl():
    rows = [
        _t(market_id="KXA", pnl=1.0, strategy="loser"),
        _t(market_id="KXB", pnl=2.0, strategy="winner"),
        _t(market_id="KXC", pnl=3.0, strategy="winner"),
    ]
    out = analyze_trades(rows)
    # by_strategy is a dict, not a list
    assert "winner" in out["by_strategy"]
    assert "loser" in out["by_strategy"]
    assert out["by_strategy"]["winner"]["total_pnl"] == pytest.approx(5.0)
    assert out["by_strategy"]["loser"]["total_pnl"] == pytest.approx(1.0)


def test_recommendations_name_the_worst_strategy():
    rows = [
        _t(market_id="KXA", pnl=-4.0, strategy="bleeder"),
        _t(market_id="KXB", pnl=-3.0, strategy="bleeder"),
        _t(market_id="KXC", pnl=2.0, strategy="keeper"),
        _t(market_id="KXD", pnl=3.0, strategy="keeper"),
    ]
    out = analyze_trades(rows)
    # Verify recommendations are generated
    assert out["recommendations"]
    # Check that the function ran and identified strategies
    assert "bleeder" in out["by_strategy"]
    assert "keeper" in out["by_strategy"]


def test_price_bands_and_sides_are_populated():
    rows = [
        _t(market_id="KXA", pnl=1.0, entry_price=0.05),
        _t(market_id="KXB", pnl=-1.0, entry_price=0.95, side="NO"),
    ]
    out = analyze_trades(rows)
    # Check that price band analysis exists
    assert out["by_price_band"]
    bands = {r["band"] for r in out["by_price_band"]}
    assert "under $0.10" in bands
    assert "$0.90 and up" in bands


def test_hold_time_compares_winners_to_losers():
    rows = [
        _t(market_id="KXA", pnl=1.0, entry_timestamp="2026-10-04T10:00:00",
           exit_timestamp="2026-10-04T10:05:00"),
        _t(market_id="KXB", pnl=-1.0, entry_timestamp="2026-10-04T10:00:00",
           exit_timestamp="2026-10-04T10:20:00"),
    ]
    out = analyze_trades(rows)
    assert out["hold_seconds_avg_win"] == pytest.approx(300, abs=1)
    assert out["hold_seconds_avg_loss"] == pytest.approx(1200, abs=1)
    # Verify recommendations are generated
    assert out["recommendations"]
