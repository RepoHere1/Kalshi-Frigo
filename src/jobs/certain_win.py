"""The certain-win pair trade (the operator's "take-larger-amounts" rule).

A binary contract's YES and NO sides together pay exactly $1.00 at
settlement. When the two ASKS cost less than $1.00 minus both taker fees and
the required margin, buying both sides is not a bet - it is arithmetic:
whatever the outcome, the pair pays $1.00. This is the only situation in
these markets where larger size is justified, because the win does not
depend on any prediction being right.

`evaluate` is pure: quotes in, "is this certain" plus the per-pair net out.
It is deliberately conservative (fees on BOTH legs are subtracted with the
exchange's cent-up rounding) and it never proposes size - the caller caps
contracts by cash and the open-notional budget.
"""

import math
from typing import Tuple

TAKER_FEE_RATE = 0.07


def taker_fee(price: float) -> float:
    """Kalshi taker fee per contract, rounded up to the cent like the venue."""
    raw = TAKER_FEE_RATE * float(price) * (1.0 - float(price))
    return math.ceil(raw * 100.0) / 100.0


def pair_net(up_ask: float, down_ask: float) -> float:
    """$ net per pair when buying both sides at these asks.

    1.00 payout - both asks - taker fees on both legs.
    """
    cost = float(up_ask) + float(down_ask)
    return 1.0 - cost - taker_fee(up_ask) - taker_fee(down_ask)


def evaluate(
    up_ask: float, down_ask: float, min_net: float = 0.015
) -> Tuple[bool, float]:
    """(is_certain_win, net_per_pair). Certain only when net >= min_net."""
    if not up_ask or not down_ask or float(up_ask) <= 0.0 or float(down_ask) <= 0.0:
        return False, 0.0
    net = pair_net(up_ask, down_ask)
    return net >= float(min_net), net


def contracts_for(up_ask: float, down_ask: float, budget_usd: float) -> int:
    """Whole pairs a budget can buy at these asks. 0 when it cannot buy one."""
    pair_cost = float(up_ask) + float(down_ask)
    if pair_cost <= 0.0 or float(budget_usd) <= 0.0:
        return 0
    return max(0, int(float(budget_usd) // pair_cost))
