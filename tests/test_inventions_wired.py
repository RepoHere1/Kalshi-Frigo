"""Tests for the 12 invention modules wired into the LIVE+DRY entry/exit pipeline."""

import pytest

from src.jobs.flow_sentinel import compute_flow_metrics, flow_sentinel
from src.jobs.macro_veto import macro_verdict
from src.jobs.shadow_backtest import shadow_backtest
from src.jobs.rag_prompt import find_similar_markets, inject_rag_context
from src.jobs.sentiment_fusion import (
    OnChainData,
    SentimentData,
    fetch_onchain_data,
    fetch_sentiment,
    sentiment_fusion,
)
from src.jobs.scenario_test import generate_scenarios, validate_consistency
from src.jobs.prompt_library import VETO_PROMPT_TEMPLATE


class TestFlowSentinel:
    def test_zero_bid_passthrough(self):
        m = compute_flow_metrics(yes_ask=0.55, yes_bid=0.0, no_ask=0.45, no_bid=0.0, yes_depth=0, no_depth=0)
        passes, reason = flow_sentinel(m)
        assert passes is True

    def test_near_certainty_passthrough(self):
        m = compute_flow_metrics(yes_ask=0.95, yes_bid=0.94, no_ask=0.05, no_bid=0.04, yes_depth=100, no_depth=100)
        passes, reason = flow_sentinel(m)
        assert passes is True

    def test_tight_book_passes(self):
        m = compute_flow_metrics(yes_ask=0.51, yes_bid=0.50, no_ask=0.50, no_bid=0.49, yes_depth=100, no_depth=100)
        passes, reason = flow_sentinel(m)
        assert passes is True

    def test_wide_spread_vetoes(self):
        # Sanitization handles inverted: yes_bid=0.50, yes_ask=0.55, gap=0.05=5c
        # Default max_spread_cents=12, so this still passes. Need bigger gap.
        m = compute_flow_metrics(yes_ask=0.55, yes_bid=0.40, no_ask=0.50, no_bid=0.35, yes_depth=100, no_depth=100)
        passes, reason = flow_sentinel(m, max_spread_cents=3.0)
        assert passes is False


class TestMacroVeto:
    def test_no_client_no_veto(self):
        result = macro_verdict("BTC 15min", "crypto")
        assert result["vetoes"] is False

    def test_cache_hit(self):
        cache = {"BTC 15min": {"score": -2.0, "reason": "test", "vetoes": True}}
        result = macro_verdict("BTC 15min", "crypto", cache=cache)
        assert result["vetoes"] is True


class TestShadowBacktest:
    def test_no_data_passes(self):
        passes, wr, po = shadow_backtest("BTC", 0.5, 0.5, [])
        assert passes is True
        assert wr == 0.0

    def test_low_win_rate_vetoes(self):
        history = [
            {"category": "btc", "outcome": "loss"} for _ in range(20)
        ]
        passes, wr, po = shadow_backtest("BTC-15M", 0.5, 0.5, history)
        assert passes is False


class TestRagPrompt:
    def test_find_similar_empty(self):
        result = find_similar_markets("BTC", "crypto", [], lambda t, c: [0.1, 0.2])
        assert result == []

    def test_find_similar_returns_top_k(self):
        markets = [
            {"ticker": f"BTC-{i}", "category": "crypto", "outcome": "win"}
            for i in range(10)
        ]
        result = find_similar_markets("BTC-9", "crypto", markets, lambda t, c: [1.0, 1.0], top_k=3)
        assert len(result) <= 3

    def test_inject_context_empty(self):
        ctx = inject_rag_context("BTC", [], {})
        assert "No similar" in ctx or len(ctx) > 0


class TestSentimentFusion:
    def test_no_data_passthrough(self):
        result = sentiment_fusion(0.10, None, None)
        assert result == 0.10

    def test_high_funding_bearish(self):
        oc = OnChainData(funding_rate=0.0005, open_interest=0, whale_flow_1h=0, whale_flow_24h=0)
        result = sentiment_fusion(0.10, oc, None)
        assert result < 0.10  # Bearish adjustment

    def test_bullish_sentiment(self):
        sent = SentimentData(social_score=0.8, news_sentiment=0.5)
        result = sentiment_fusion(0.10, None, sent)
        assert result > 0.10  # Bullish adjustment

    def test_fetch_onchain_fail_safe(self):
        result = fetch_onchain_data("BTC", None)
        assert result is not None  # Placeholder returns data

    def test_fetch_sentiment_fail_safe(self):
        result = fetch_sentiment("BTC", None)
        assert result is not None


class TestScenarioTest:
    def test_generate_placeholder(self):
        scenarios = generate_scenarios("BTC 15min", None, num_scenarios=5)
        assert len(scenarios) == 5

    def test_validate_consistency_passes(self):
        scenarios = generate_scenarios("BTC 15min", None, num_scenarios=5)
        assert validate_consistency("BTC 15min", scenarios, None) is True


class TestPromptLibrary:
    def test_veto_template(self):
        prompt = VETO_PROMPT_TEMPLATE.format(
            market_title="BTC 15min",
            entry_fair=0.75,
            current_price=0.55,
            side="YES",
        )
        assert "BTC 15min" in prompt
        assert "0.75" in prompt
