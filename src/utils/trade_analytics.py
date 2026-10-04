"""Perpetual analysis of every closed trade in the book.

The trade log is a forever database: every close is appended and never pruned
(trade_logs lives on the Railway volume at DB_PATH). These functions read that
log and turn it into numbers and plain-English recommendations. Nothing here
places orders or changes state; it is pure read-only analysis, so the
"ANALYSIS OF" button is safe to press at any time.

Recommendations are deterministic rules derived from the data actually present
- every claim cites its own number - and never invent advice when there is not
enough data to justify it.
"""
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

PRICE_BANDS = [
    (0.0, 0.10, "under $0.10"),
    (0.10, 0.25, "$0.10 - $0.25"),
    (0.25, 0.50, "$0.25 - $0.50"),
    (0.50, 0.75, "$0.50 - $0.75"),
    (0.75, 0.90, "$0.75 - $0.90"),
    (0.90, 1.01, "$0.90 and up"),
]

MIN_TRADES_FOR_ADVICE = 5


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _band(price: float) -> str:
    for lo, hi, label in PRICE_BANDS:
        if lo <= price < hi:
            return label
    return "unknown"


def analyze_trades(
    trades: List[Dict[str, Any]], name: Optional[str] = None
) -> Dict[str, Any]:
    """Turn closed trades into stats and recommendations.

    `name` is the strategy under analysis; when None the whole book is
    analysed. Every trade is expected to carry at least: strategy, side,
    entry_price, exit_price, pnl, exit_timestamp, exit_reason.
    """
    rows = [t for t in trades if isinstance(t, dict)]
    if not rows:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "best": 0.0,
            "worst": 0.0,
            "by_strategy": [],
            "by_side": [],
            "by_price_band": [],
            "by_hour": [],
            "by_exit_reason": [],
            "hold_seconds_avg_win": 0.0,
            "hold_seconds_avg_loss": 0.0,
            "recommendations": [
                "No closed trades recorded yet - nothing to analyse."
            ],
            "scope": name or "all strategies",
        }

    wins = [t for t in rows if _f(t.get("pnl")) > 0]
    losses = [t for t in rows if _f(t.get("pnl")) <= 0]
    total_pnl = sum(_f(t.get("pnl")) for t in rows)
    best = max(rows, key=lambda t: _f(t.get("pnl")))
    worst = min(rows, key=lambda t: _f(t.get("pnl")))

    # --- by strategy ------------------------------------------------------
    by_strategy: List[Dict[str, Any]] = []
    strat: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in rows:
        strat[t.get("strategy") or "unattributed"].append(t)
    for sname, srows in sorted(
        strat.items(), key=lambda kv: -sum(_f(t.get("pnl")) for t in kv[1])
    ):
        swins = sum(1 for t in srows if _f(t.get("pnl")) > 0)
        by_strategy.append(
            {
                "strategy": sname,
                "trades": len(srows),
                "wins": swins,
                "losses": len(srows) - swins,
                "win_rate": round(swins / len(srows) * 100, 1),
                "pnl": round(sum(_f(t.get("pnl")) for t in srows), 2),
                "avg_pnl": round(sum(_f(t.get("pnl")) for t in srows) / len(srows), 2),
            }
        )

    # --- by side ----------------------------------------------------------
    by_side: List[Dict[str, Any]] = []
    side: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in rows:
        side[str(t.get("side") or "?").upper()].append(t)
    for sname, srows in sorted(side.items(), key=lambda kv: -len(kv[1])):
        swins = sum(1 for t in srows if _f(t.get("pnl")) > 0)
        by_side.append(
            {
                "side": sname,
                "trades": len(srows),
                "win_rate": round(swins / len(srows) * 100, 1),
                "pnl": round(sum(_f(t.get("pnl")) for t in srows), 2),
            }
        )

    # --- by entry price band ----------------------------------------------
    band: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in rows:
        band[_band(_f(t.get("entry_price")) )].append(t)
    by_price_band: List[Dict[str, Any]] = []
    for lo, _hi, label in PRICE_BANDS:
        brows = band.get(label, [])
        if not brows:
            continue
        bwins = sum(1 for t in brows if _f(t.get("pnl")) > 0)
        by_price_band.append(
            {
                "band": label,
                "trades": len(brows),
                "win_rate": round(bwins / len(brows) * 100, 1),
                "pnl": round(sum(_f(t.get("pnl")) for t in brows), 2),
            }
        )

    # --- by hour of exit ---------------------------------------------------
    hour: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for t in rows:
        ts = str(t.get("exit_timestamp") or t.get("entry_timestamp") or "")
        if len(ts) >= 13:
            try:
                hour[int(ts[11:13])].append(t)
            except ValueError:
                pass
    by_hour: List[Dict[str, Any]] = []
    for h in sorted(hour):
        hrows = hour[h]
        hwins = sum(1 for t in hrows if _f(t.get("pnl")) > 0)
        by_hour.append(
            {
                "hour": f"{h:02d}:00",
                "trades": len(hrows),
                "win_rate": round(hwins / len(hrows) * 100, 1),
                "pnl": round(sum(_f(t.get("pnl")) for t in hrows), 2),
            }
        )

    # --- by exit reason -----------------------------------------------------
    reason: Counter = Counter(
        str(t.get("exit_reason") or "unknown").strip() or "unknown" for t in rows
    )
    by_exit_reason: List[Dict[str, Any]] = [
        {"reason": r, "trades": c} for r, c in reason.most_common()
    ]

    # --- hold time (seconds) ------------------------------------------------
    def _hold(t: Dict[str, Any]) -> float:
        try:
            from datetime import datetime

            entry = str(t.get("entry_timestamp") or "")[:19]
            exit_ = str(t.get("exit_timestamp") or "")[:19]
            if len(entry) >= 19 and len(exit_) >= 19:
                return (
                    datetime.fromisoformat(exit_) - datetime.fromisoformat(entry)
                ).total_seconds()
        except Exception:  # noqa: BLE001
            pass
        return 0.0

    holds_win = [_hold(t) for t in wins if _hold(t) > 0]
    holds_loss = [_hold(t) for t in losses if _hold(t) > 0]
    avg_hold_win = sum(holds_win) / len(holds_win) if holds_win else 0.0
    avg_hold_loss = sum(holds_loss) / len(holds_loss) if holds_loss else 0.0

    # --- recommendations ----------------------------------------------------
    recs: List[str] = []
    n = len(rows)
    win_rate = len(wins) / n * 100

    if by_strategy:
        top = by_strategy[0]
        bottom = by_strategy[-1]
        if len(by_strategy) > 1:
            recs.append(
                f"Best strategy: {top['strategy']} (+${top['pnl']:.2f} over "
                f"{top['trades']} trades, {top['win_rate']:.0f}% win rate)."
            )
            if bottom["pnl"] < 0:
                recs.append(
                    f"Worst strategy: {bottom['strategy']} (${bottom['pnl']:.2f} over "
                    f"{bottom['trades']} trades, {bottom['win_rate']:.0f}% win rate). "
                    f"Consider stopping it until its setup improves."
                )

    worst_band = min(
        by_price_band, key=lambda b: (b["win_rate"], b["pnl"])
    ) if by_price_band else None
    best_band = max(
        by_price_band, key=lambda b: (b["win_rate"], b["pnl"])
    ) if by_price_band else None
    if worst_band and worst_band["trades"] >= MIN_TRADES_FOR_ADVICE and worst_band["win_rate"] < 40:
        recs.append(
            f"Avoid entries {worst_band['band']}: {worst_band['trades']} trades, "
            f"{worst_band['win_rate']:.0f}% win rate (${worst_band['pnl']:.2f})."
        )
    if best_band and best_band["trades"] >= MIN_TRADES_FOR_ADVICE and best_band["win_rate"] > 60:
        recs.append(
            f"Sweet spot is entries {best_band['band']}: {best_band['trades']} trades, "
            f"{best_band['win_rate']:.0f}% win rate (${best_band['pnl']:.2f})."
        )

    if by_side and len(by_side) > 1:
        better_side = max(by_side, key=lambda s: (s["win_rate"], s["pnl"]))
        other_side = min(by_side, key=lambda s: (s["win_rate"], s["pnl"]))
        if better_side["win_rate"] > other_side["win_rate"] + 10:
            recs.append(
                f"{better_side['side']} trades win {better_side['win_rate']:.0f}% vs "
                f"{other_side['side']} at {other_side['win_rate']:.0f}% - "
                f"prefer the {better_side['side']} side."
            )

    if holds_win and holds_loss and avg_hold_loss > avg_hold_win * 2:
        recs.append(
            f"Losers are held ~{avg_hold_loss / 60:.0f}m vs winners ~{avg_hold_win / 60:.0f}m - "
            f"cut losses sooner."
        )
    elif holds_win and holds_loss and avg_hold_win > avg_hold_loss * 2:
        recs.append(
            f"Winners are held ~{avg_hold_win / 60:.0f}m vs losers ~{avg_hold_loss / 60:.0f}m - "
            f"let winners run a little longer."
        )

    if win_rate < 40 and n >= MIN_TRADES_FOR_ADVICE:
        recs.append(
            f"Overall win rate is {win_rate:.0f}% over {n} trades - edge is negative; "
            f"reduce size or raise the confidence bar."
        )
    elif win_rate > 60 and n >= MIN_TRADES_FOR_ADVICE:
        recs.append(
            f"Overall win rate is {win_rate:.0f}% over {n} trades - the edge is real; "
            f"consider scaling size up slowly."
        )

    if not recs:
        recs.append(
            f"{n} closed trade(s) so far - too few to recommend changes. "
            f"Analysis updates automatically as the forever log grows."
        )

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / n, 2),
        "best": round(_f(best.get("pnl")), 2),
        "worst": round(_f(worst.get("pnl")), 2),
        "best_market": best.get("market_id"),
        "worst_market": worst.get("market_id"),
        "by_strategy": by_strategy,
        "by_side": by_side,
        "by_price_band": by_price_band,
        "by_hour": by_hour,
        "by_exit_reason": by_exit_reason,
        "hold_seconds_avg_win": round(avg_hold_win, 0),
        "hold_seconds_avg_loss": round(avg_hold_loss, 0),
        "recommendations": recs,
        "scope": name or "all strategies",
    }
