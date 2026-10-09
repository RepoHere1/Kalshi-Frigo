"""The certain-win pair trade: arithmetic, not prediction."""

from src.jobs import certain_win as cw


def test_pair_net_subtracts_both_taker_fees():
    # 0.30 + 0.62 = 0.92 cost; fees: ceil(1.47)=0.02 + ceil(1.6492)=0.02.
    net = cw.pair_net(0.30, 0.62)
    assert round(net, 4) == round(1.0 - 0.92 - 0.04, 4)


def test_evaluate_fires_only_past_the_min_net():
    ok, net = cw.evaluate(0.30, 0.62, min_net=0.015)
    assert ok is True
    assert net > 0.015
    # 0.40 + 0.55 = 0.95; fees 0.02 + 0.02 -> net 0.01, below the bar.
    ok2, net2 = cw.evaluate(0.40, 0.55, min_net=0.015)
    assert ok2 is False
    assert net2 < 0.015


def test_evaluate_refuses_missing_or_zero_quotes():
    assert cw.evaluate(0.0, 0.5) == (False, 0.0)
    assert cw.evaluate(None, 0.5) == (False, 0.0)  # type: ignore[arg-type]
    assert cw.evaluate(0.5, None) == (False, 0.0)  # type: ignore[arg-type]


def test_contracts_for_whole_pairs_only():
    assert cw.contracts_for(0.30, 0.62, 25.0) == 27  # 25 // 0.92
    assert cw.contracts_for(0.30, 0.62, 0.5) == 0
    assert cw.contracts_for(0.0, 0.0, 25.0) == 0


def test_taker_fee_rounds_up_like_the_venue():
    assert cw.taker_fee(0.50) == 0.02   # 1.75c -> 2c
    assert cw.taker_fee(0.10) == 0.01   # 0.63c -> 1c
    assert cw.taker_fee(0.90) == 0.01
