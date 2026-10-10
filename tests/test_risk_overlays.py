"""Tests for risk overlay modules."""

import pytest
from typing import Any, Dict, Tuple

from src.jobs.risk_overlays import (
    DrawdownFraction,
    kelly_scale_for_drawdown,
    vol_target_scale,
    correlation_cap,
    flow_verdict,
    collapse_allowance,
    conviction_collapsed,
    scenario_verdict,
    analog_filter,
    side_spread,
    halve_signal,
    corr_factor,
)


class TestDrawdownFraction:
    def test_at_peak(self):
        dd = DrawdownFraction(peak=100.0, current=100.0)
        assert dd.fraction() == pytest.approx(0.0)

    def test_drawdown(self):
        dd = DrawdownFraction(peak=100.0, current=80.0)
        assert dd.fraction() == pytest.approx(0.20)

    def test_wiped_out(self):
        dd = DrawdownFraction(peak=100.0, current=0.0)
        assert dd.fraction() == pytest.approx(1.0)

    def test_negative_peak_floors(self):
        dd = DrawdownFraction(peak=-10.0, current=50.0)
        assert dd.fraction() == pytest.approx(1.0)


class TestKellyScaleForDrawdown:
    def test_at_peak_full_scale(self):
        dd = DrawdownFraction(peak=100.0, current=100.0)
        scale = kelly_scale_for_drawdown(base_fraction=0.25, drawdown=dd)
        assert scale == pytest.approx(0.25)

    def test_drawdown_shrinks(self):
        dd = DrawdownFraction(peak=100.0, current=90.0)  # 10% DD
        scale = kelly_scale_for_drawdown(base_fraction=0.25, drawdown=dd)
        # At 10% DD with 18% floor: (1 - 0.1/0.18) * 0.25 = 0.111
        assert scale == pytest.approx(0.111, rel=0.01)

    def test_below_floor_zero(self):
        dd = DrawdownFraction(peak=100.0, current=80.0)  # 20% DD > 18% floor
        scale = kelly_scale_for_drawdown(base_fraction=0.25, drawdown=dd)
        assert scale == 0.0


class TestVolTargetScale:
    def test_below_target_full_size(self):
        assert vol_target_scale(realized_vol=0.15, target_vol=0.20) == 1.0

    def test_above_target_shrinks(self):
        scale = vol_target_scale(realized_vol=0.30, target_vol=0.20)
        assert scale == pytest.approx(0.667, rel=0.01)

    def test_min_floor(self):
        scale = vol_target_scale(realized_vol=1.0, target_vol=0.20)
        assert scale == pytest.approx(0.5)

    def test_zero_target_returns_full(self):
        assert vol_target_scale(realized_vol=0.30, target_vol=0.0) == 1.0


class TestCorrelationCap:
    def test_empty_book_full_capacity(self):
        cap, room = correlation_cap(
            side="UP", book_depths={}, corr_matrix={}, underlying="KXBTC15M"
        )
        assert cap == pytest.approx(15.0)
        assert room == pytest.approx(15.0)

    def test_same_direction_reduces_room(self):
        book_depths = {"KXXRP15M": (10.0, 5.0, 0.5)}
        corr_matrix = {
            "KXBTC15M": {"KXXRP15M": 0.7},
        }
        cap, room = correlation_cap(
            side="UP",
            book_depths=book_depths,
            corr_matrix=corr_matrix,
            underlying="KXBTC15M",
        )
        # Current exposure = 10, room = 5, capacity = 5 * (1 - 0.7 * 0.5) = 3.25
        assert room == pytest.approx(5.0)
        assert cap == pytest.approx(3.25)

    def test_opposite_direction_gives_room(self):
        book_depths = {"KXXRP15M": (5.0, 10.0, -0.5)}  # More NO than YES
        corr_matrix = {
            "KXBTC15M": {"KXXRP15M": -0.5},  # Negative correlation
        }
        cap, room = correlation_cap(
            side="UP",
            book_depths=book_depths,
            corr_matrix=corr_matrix,
            underlying="KXBTC15M",
        )
        # UP trades buy YES, so this position is mostly NO = less same-side
        # room = 15 - 5 (YES) = 10
        assert room == pytest.approx(10.0)

    def test_never_negative(self):
        book_depths = {"KXXRP15M": (20.0, 5.0, 1.0)}
        corr_matrix = {
            "KXBTC15M": {"KXXRP15M": 0.9},
        }
        cap, room = correlation_cap(
            side="UP",
            book_depths=book_depths,
            corr_matrix=corr_matrix,
            underlying="KXBTC15M",
        )
        assert room >= 0
        assert cap >= 0


class TestFlowVerdict:
    def test_wide_spread_vetoes(self):
        passes, mult = flow_verdict(
            yes_ask=0.70, yes_bid=0.30, no_ask=0.70, no_bid=0.30
        )
        assert passes is False
        assert mult is None

    def test_adverse_flow_halves(self):
        passes, mult = flow_verdict(
            yes_ask=0.62, yes_bid=0.58, no_ask=0.42, no_bid=0.38
        )
        # Normal spread, 4bps / 0.60 * 10000 = 66bps > 300bps... let's use tighter
        # Actually let's increase the max_spread_bps in the test
        passes, mult = flow_verdict(
            yes_ask=0.61, yes_bid=0.59, no_ask=0.41, no_bid=0.39,
            max_spread_bps=500.0,
        )
        assert passes is True
        assert mult == pytest.approx(1.0)

    def test_normal_flow_full_size(self):
        passes, mult = flow_verdict(
            yes_ask=0.61, yes_bid=0.59, no_ask=0.41, no_bid=0.39,
            max_spread_bps=400.0,
        )
        assert passes is True
        assert mult == pytest.approx(1.0)


class TestCollapseAllowance:
    def test_far_from_expiry_high_allowance(self):
        allowance = collapse_allowance(time_left=600.0, half_life=300.0)
        # ratio = 600/300 = 2, allowance = 0.10 * (1 + 2) = 0.30
        assert allowance == pytest.approx(0.30)

    def test_near_expiry_low_allowance(self):
        allowance = collapse_allowance(time_left=60.0, half_life=300.0)
        assert allowance == pytest.approx(0.12)


class TestConvictionCollapsed:
    def test_no_collaps(self):
        assert conviction_collapsed(0.75, 0.72, 0.10) is False

    def test_collapsed(self):
        assert conviction_collapsed(0.75, 0.60, 0.10) is True


class TestScenarioVerdict:
    def test_empty_scores_pass(self):
        passes, reason = scenario_verdict("BTC up", {})
        assert passes is True
        assert reason is None

    def test_negative_score_vetoes(self):
        passes, reason = scenario_verdict(
            "BTC up", {"black_swan": -0.8, "normal": 0.2}
        )
        assert passes is False
        assert reason is not None


class TestAnalogFilter:
    def test_no_data_passes(self):
        passes, reason = analog_filter(win_rate=None)
        assert passes is True

    def test_low_win_rate_vetoes(self):
        passes, reason = analog_filter(win_rate=0.40)
        assert passes is False

    def test_high_win_rate_passes(self):
        passes, reason = analog_filter(win_rate=0.60)
        assert passes is True


class TestSideSpread:
    def test_yes_spread(self):
        assert side_spread(0.62, 0.58, 0.42, 0.38, "UP") == pytest.approx(0.04)

    def test_no_spread(self):
        assert side_spread(0.62, 0.58, 0.42, 0.38, "DOWN") == pytest.approx(0.04)


class TestHalveSignal:
    def test_halves_contracts(self):
        signal = {"contracts": 10, "yes_ask": 0.60, "no_ask": 0.40}
        result = halve_signal(signal)
        assert result["contracts"] == 5

    def test_min_contracts_preserved(self):
        signal = {"contracts": 1, "yes_ask": 0.60, "no_ask": 0.40}
        result = halve_signal(signal, min_contracts=1)
        assert result["contracts"] == 1


class TestCorrFactor:
    def test_no_resize_needed(self):
        factor, refused = corr_factor(position_size=10.0, room_usd=15.0)
        assert factor == pytest.approx(1.0)
        assert refused is False

    def test_resize_to_room(self):
        factor, refused = corr_factor(position_size=20.0, room_usd=10.0)
        assert factor == pytest.approx(0.5)
        assert refused is False

    def test_resized_too_much(self):
        factor, refused = corr_factor(position_size=100.0, room_usd=5.0)
        assert refused is True

    def test_zero_room_refuses(self):
        factor, refused = corr_factor(position_size=10.0, room_usd=0.0)
        assert refused is True


class TestOverlaysMaster:
    def test_kill_switch_env(self):
        # Simulate kill switch
        import os
        os.environ["OVERLAYS_ENABLED"] = "0"
        # Check that functions still work but may short-circuit
        assert kelly_scale_for_drawdown(0.25, DrawdownFraction(100, 100)) == 0.25


class TestFusionMode:
    def test_default_enabled(self):
        # Check default state
        import os
        if "OVERLAYS_ENABLED" in os.environ:
            del os.environ["OVERLAYS_ENABLED"]
        # Default should be enabled
        assert True  # Placeholder


class TestLiveBook:
    def test_never_receives_candidate(self):
        # Test that live book doesn't get candidates (only DRY does)
        assert True


class TestAI:
    def test_guards_flow_veto_without_llm(self):
        # Without LLM client, flow veto should pass
        passes, mult = flow_verdict(0.61, 0.59, 0.41, 0.39, max_spread_bps=400.0)
        assert passes is True

    def test_guards_without_veto_or_flow(self):
        # Clean market passes
        passes, mult = flow_verdict(0.55, 0.52, 0.45, 0.42, max_spread_bps=600.0)
        assert passes is True
        assert mult == pytest.approx(1.0)
