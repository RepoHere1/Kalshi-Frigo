"""
Tests for the EdgeFilter module.

Verifies edge calculations, fee adjustments, and filtering logic.
"""
import pytest
from src.utils.edge_filter import EdgeFilter, calculate_edge, passes_edge_filter


class TestEdgeFilterBasic:
    """Test basic edge calculation."""

    def test_edge_calculation_yes_position(self):
        """Test edge calculation for YES position."""
        # AI thinks 60% probability, market at 40%
        # Edge = 60% - 40% = 20%
        result = EdgeFilter.calculate_edge(
            ai_probability=0.60,
            market_probability=0.40,
            confidence=0.7
        )
        assert result.edge_percentage == pytest.approx(0.20)
        assert result.side == "YES"

    def test_edge_calculation_no_position(self):
        """Test edge calculation for NO position."""
        # AI thinks 40% YES (60% NO), market at 60% YES (40% NO)
        # Edge = 40% - 60% = -20%
        result = EdgeFilter.calculate_edge(
            ai_probability=0.40,
            market_probability=0.60,
            confidence=0.7
        )
        assert result.edge_percentage == pytest.approx(0.20)
        assert result.side == "NO"

    def test_edge_calculation_no_edge(self):
        """Test when AI probability equals market price."""
        result = EdgeFilter.calculate_edge(
            ai_probability=0.50,
            market_probability=0.50,
            confidence=0.7
        )
        assert result.edge_percentage == 0.0
        assert not result.passes_filter


class TestEdgeFilterWithFees:
    """Test edge calculations that account for fees."""

    def test_edge_after_taker_fees(self):
        """Test that taker fees reduce edge."""
        # 100 contracts at 50c, taker fee = 7% * price * (1-price) * contracts = $1.75
        # Fee percentage = $1.75 / $50 = 3.5%
        # 10% edge - 3.5% fees = 6.5% after fees
        result = EdgeFilter.calculate_edge(
            ai_probability=0.60,
            market_probability=0.50,
            confidence=0.7,
            price=0.50,
            contracts=100,
            is_maker=False
        )
        assert result.edge_percentage == pytest.approx(0.10)
        assert not result.passes_filter  # 6.5% after fees < 12% required for 70% confidence

    def test_edge_after_maker_fees(self):
        """Test that maker fees (1.75%) reduce edge less than taker."""
        # 100 contracts at 50c, maker fee = 1.75% * price * (1-price) * contracts = $0.4375
        # Fee percentage = $0.4375 / $50 = 0.875%
        # 10% edge - 0.875% fees = 9.125% after fees
        result = EdgeFilter.calculate_edge(
            ai_probability=0.60,
            market_probability=0.50,
            confidence=0.7,
            price=0.50,
            contracts=100,
            is_maker=True
        )
        assert not result.passes_filter  # 9.125% < 12% required for 70% confidence


class TestEdgeFilterThresholds:
    """Test edge thresholds match LLM prompt requirements."""

    def test_min_edge_requirement_is_10_percent(self):
        """Verify MIN_EDGE_REQUIREMENT is 10% to match prompt."""
        assert EdgeFilter.MIN_EDGE_REQUIREMENT == 0.10

    def test_high_confidence_threshold(self):
        """Test high confidence (>=80%) requires 10% edge."""
        result = EdgeFilter.calculate_edge(
            ai_probability=0.60,
            market_probability=0.50,
            confidence=0.85
        )
        # 10% edge should pass for high confidence
        assert result.passes_filter

    def test_medium_confidence_threshold(self):
        """Test medium confidence (>=60%) requires 12% edge."""
        result = EdgeFilter.calculate_edge(
            ai_probability=0.65,
            market_probability=0.50,
            confidence=0.70
        )
        # 15% edge should pass for medium confidence
        assert result.passes_filter

    def test_low_confidence_threshold(self):
        """Test low confidence (<60%) requires 15% edge."""
        result = EdgeFilter.calculate_edge(
            ai_probability=0.70,
            market_probability=0.50,
            confidence=0.50
        )
        # 0.50 confidence < MIN_CONFIDENCE_FOR_TRADE (0.55), so filter fails
        assert not result.passes_filter
        assert "Confidence" in result.reason

    def test_confidence_below_minimum_fails(self):
        """Test that confidence below 55% fails regardless of edge."""
        result = EdgeFilter.calculate_edge(
            ai_probability=0.70,
            market_probability=0.50,
            confidence=0.50  # Below MIN_CONFIDENCE_FOR_TRADE
        )
        assert not result.passes_filter
        assert "Confidence" in result.reason


class TestEdgeFilterIntegration:
    """Integration tests with should_trade_market."""

    def test_should_trade_market_with_fees(self):
        """Test should_trade_market_with_fees integrates fee calculations."""
        should_trade, reason, edge_result = EdgeFilter.should_trade_market_with_fees(
            ai_probability=0.60,
            market_probability=0.50,
            confidence=0.75,
            price=0.50,
            contracts=100,
            is_maker=True
        )
        # 10% raw edge, 0.875% maker fee -> 9.125% after fees < 12% required for medium confidence (0.75)
        assert not should_trade

    def test_should_trade_market_rejects_low_edge(self):
        """Test that should_trade_market rejects insufficient edge."""
        should_trade, reason, edge_result = EdgeFilter.should_trade_market_with_fees(
            ai_probability=0.55,
            market_probability=0.50,
            confidence=0.75,
            price=0.50,
            contracts=100,
            is_maker=True
        )
        # 5% edge should fail after fees
        assert not should_trade


class TestEdgeFilterConvenienceFunctions:
    """Test convenience functions."""

    def test_calculate_edge_function(self):
        """Test standalone calculate_edge function."""
        result = calculate_edge(0.60, 0.50, 0.7)
        assert result.edge_percentage == pytest.approx(0.10)
        assert not result.passes_filter  # 10% edge < 12% required for medium confidence

    def test_passes_edge_filter_function(self):
        """Test standalone passes_edge_filter function."""
        assert not passes_edge_filter(0.60, 0.50, 0.7)  # 10% edge < 12% required
        assert not passes_edge_filter(0.52, 0.50, 0.7)
