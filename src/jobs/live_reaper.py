"""LIVE-only book reaper: kill dead rows and liquidate orphan holdings.

The account view showed 31 "positions" and almost none of them were anything.
Kalshi keeps a zero-share row for every market the account has ever touched
(23 of them here) -- those are not positions, cost nothing, and cannot be
deleted through the API. What DID cost money and API calls:

  1. Phantom local LIVE rows. Both remaining rows (a settled BTC bucket and a
     KXINSURRECTION clip) had no Kalshi holding behind them at all: nothing
     was ever bought, or it is long gone, so the local row could never close
     and burned a strategy slot plus a failed sell attempt every cycle.

  2. Orphan holdings. Real contracts Kalshi still carried with no local row
     tracking them: a JAHAR clip, an LMARKKANEN clip and a DJIA short. Real
     money, unmanaged, invisible to the exits.

This module does exactly two things, both LIVE-only, both bounded:

  * Phantom close: a local LIVE row whose ticker has NO Kalshi holding, after
    a grace period (so a fresh row that has not propagated yet is never
    killed), is closed at its entry price for zero P&L with the reason
    `no_kalshi_position`. No receipt is invented -- there was no trade.

  * Liquidation: a Kalshi holding with no open local LIVE row protecting it
    is sold at the bid. Shorts are reported, not covered, unless
    LIVE_REAP_SHORTS=1 -- covering a short is a certain cash outflow and that
    call belongs to the operator.

DRY is never touched: this is only ever called with the LIVE book.
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.jobs.broker import broker_for_mode, build_order_request
from src.utils.database import TradeLog
from src.utils.market_prices import get_market_prices
from src.utils.mode import MODE_LIVE

logger = logging.getLogger("live_reaper")

# How often the reaper runs.
REAP_INTERVAL_SEC = 900.0
# A row younger than this is never called phantom: a buy placed seconds ago
# may not be visible in /portfolio/positions yet, and killing it would drop a
# real position's record on the floor.
PHANTOM_GRACE_SEC = 600.0
# Bounded work per pass: one sell per orphan, at most this many.
MAX_LIQUIDATIONS_PER_PASS = 10
# Covering a short spends real cash, so it stays an operator decision.
ALLOW_SHORTS = str(os.environ.get("LIVE_REAP_SHORTS", "0")).lower() in (
    "1",
    "true",
    "yes",
)

_LAST_SUMMARY: Dict[str, Any] = {}


def _summary_path() -> str:
    """Where the pass is written so the dashboard process can read it.

    The reaper runs in the trader process and the journal endpoint runs in the
    web process; in-memory state is invisible across them even on one box.
    """
    import os

    db_path = os.environ.get("DB_PATH", "trading_system.db")
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "live_reaper_summary.json")


def _persist(summary: Dict[str, Any]) -> None:
    try:
        import json

        with open(_summary_path(), "w", encoding="utf-8") as fh:
            json.dump(summary, fh, default=str)
    except Exception as exc:  # noqa: BLE001 - visibility never blocks a pass
        logger.warning(f"Reaper summary not persisted: {type(exc).__name__}: {exc}")


def last_summary() -> Dict[str, Any]:
    """The most recent pass, for /api/live/journal. File first: the reaper and
    the endpoint are different processes."""
    try:
        import json

        with open(_summary_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 - no pass yet, or unreadable
        return dict(_LAST_SUMMARY)


async def _holdings(client: Any) -> tuple[Dict[str, float], int]:
    """Ticker -> net YES shares held (nonzero only), plus the flat-row count.

    Kalshi reports `position_fp` in YES contracts: positive is long YES,
    negative is long NO. Both are holdings to flatten by SELLING that side -
    a negative number is not a naked short. Kalshi also returns a row per
    market the account ever touched; `position_fp` of 0.00 is not a position,
    is dropped from the map so it can never be mistaken for something to sell,
    and is only counted.
    """
    payload = await client.get_positions()
    held: Dict[str, float] = {}
    flat = 0
    for bucket in ("market_positions", "event_positions"):
        for row in payload.get(bucket) or []:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or row.get("event_ticker") or "").strip()
            if not ticker:
                continue
            try:
                qty = float(row.get("position_fp") or 0.0)
            except (TypeError, ValueError):
                continue
            if abs(qty) <= 1e-9:
                flat += 1
                continue
            held[ticker] = qty
    return held, flat


async def _newest_fill(client: Any, ticker: str) -> Optional[Dict[str, Any]]:
    """The newest fill on a ticker, or None. The side/action pair is how a
    genuinely naked short is told apart from an ordinary long NO holding:
    a long NO's last fill is a BUY of no, a naked short's is a SELL of yes.
    """
    try:
        payload = await client.get_fills(ticker=ticker, limit=50)
    except Exception as exc:  # noqa: BLE001 - unknown history, not a short
        logger.warning(f"Reaper cannot read fills for {ticker}: {exc}")
        return None
    rows = [r for r in (payload.get("fills") or []) if isinstance(r, dict)]
    if not rows:
        return None
    return max(rows, key=lambda r: str(r.get("created_time") or ""))


async def _orphan_sell(client: Any, ticker: str, side: str, contracts: int) -> Dict[str, Any]:
    """Sell `contracts` at the bid: the best price a seller can get, now."""
    market_data = await client.get_market(ticker)
    market = (market_data or {}).get("market") or {}
    yes_bid, yes_ask, no_bid, no_ask = get_market_prices(market)
    bid = yes_bid if side == "yes" else no_bid
    ask = yes_ask if side == "yes" else no_ask
    if bid <= 0.0:
        bid = (ask or 0.0) * 0.98
    broker = broker_for_mode(MODE_LIVE, kalshi_client=client)
    available_cents = await broker.available_cents()
    request, reason = build_order_request(
        market_id=ticker,
        side=side,
        action="sell",
        quantity=contracts,
        market=market,
        available_cents=available_cents,
        limit_price_dollars=bid,
    )
    if request is None:
        return {"ok": False, "reason": reason}
    response = await broker.submit(request)
    if response.get("error"):
        return {"ok": False, "reason": str(response.get("error"))}
    return {
        "ok": True,
        "price": round(request.fill_price, 4),
        "contracts": contracts,
        "order_id": response.get("order", {}).get("order_id", ""),
        "side": side,
    }


async def _short_cover(client: Any, ticker: str, side: str, contracts: int) -> Dict[str, Any]:
    """Cover a short by buying the same side back at the ask."""
    market_data = await client.get_market(ticker)
    market = (market_data or {}).get("market") or {}
    yes_bid, yes_ask, no_bid, no_ask = get_market_prices(market)
    ask = yes_ask if side == "yes" else no_ask
    if ask <= 0.0:
        ask = (yes_bid if side == "yes" else no_bid) * 1.02
    broker = broker_for_mode(MODE_LIVE, kalshi_client=client)
    available_cents = await broker.available_cents()
    request, reason = build_order_request(
        market_id=ticker,
        side=side,
        action="buy",
        quantity=contracts,
        market=market,
        available_cents=available_cents,
        limit_price_dollars=ask,
    )
    if request is None:
        return {"ok": False, "reason": reason}
    response = await broker.submit(request)
    if response.get("error"):
        return {"ok": False, "reason": str(response.get("error"))}
    return {
        "ok": True,
        "price": round(request.fill_price, 4),
        "contracts": contracts,
        "side": side,
    }


async def reap_live_book(
    db_manager: Any,
    client: Any,
    *,
    allow_shorts: Optional[bool] = None,
    max_liquidations: Optional[int] = None,
) -> Dict[str, Any]:
    """One bounded reaping pass. Returns (and logs) what it killed."""
    allow_shorts = ALLOW_SHORTS if allow_shorts is None else bool(allow_shorts)
    max_liquidations = (
        MAX_LIQUIDATIONS_PER_PASS if max_liquidations is None else int(max_liquidations)
    )
    summary: Dict[str, Any] = {
        "holdings_seen": 0,
        "flat_rows_on_kalshi": 0,
        "rows_checked": 0,
        "phantom_closed": 0,
        "phantom_tickers": [],
        "liquidated": [],
        "shorts_flagged": [],
        "dust": 0,
        "dust_tickers": [],
        "protected": [],
        "errors": [],
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    held, flat = await _holdings(client)
    summary["holdings_seen"] = len(held)
    summary["flat_rows_on_kalshi"] = flat

    # mode="live" EXPLICITLY: with no argument this resolves the current
    # trading mode, so a DRY session would have handed the reaper the DRY book
    # to close. This module only ever looks at the LIVE book.
    rows = await db_manager.get_open_live_positions(mode="live")
    summary["rows_checked"] = len(rows)
    now = datetime.now(timezone.utc)

    # Pass 1: phantom local rows. No Kalshi holding behind them, old enough
    # that propagation cannot be the explanation -> close at zero P&L.
    protected: set = set()
    for row in rows:
        ticker = str(row.market_id or "")
        qty = held.get(ticker, 0.0)
        if abs(qty) > 1e-9:
            protected.add(ticker)
            summary["protected"].append(ticker)
            continue
        stamped = row.timestamp
        if isinstance(stamped, str):
            try:
                stamped = datetime.fromisoformat(stamped.replace("Z", "+00:00"))
            except ValueError:
                stamped = None
        if stamped is not None and stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=timezone.utc)
        if stamped is None:
            continue
        if (now - stamped).total_seconds() < PHANTOM_GRACE_SEC:
            continue
        log = TradeLog(
            market_id=ticker,
            side=row.side,
            entry_price=row.entry_price,
            exit_price=row.entry_price,
            quantity=row.quantity,
            pnl=0.0,
            entry_timestamp=stamped,
            exit_timestamp=datetime.now(),
            rationale=(
                f"{row.rationale or ''} | EXIT: no_kalshi_position "
                "(LIVE reaper: Kalshi holds no shares of this ticker, so no "
                "trade existed to settle - closed at entry for zero P&L)"
            ).strip(" |"),
            strategy=row.strategy or "unknown",
            exit_reason="no_kalshi_position",
            mode="live",
            fee_paid=0.0,
        )
        try:
            await db_manager.add_trade_log(log)
            if row.id is not None:
                await db_manager.update_position_status(row.id, "closed")
            summary["phantom_closed"] += 1
            summary["phantom_tickers"].append(ticker)
            logger.warning(
                f"REAPER closed phantom LIVE row {ticker} ({row.side} "
                f"{row.quantity} @ {row.entry_price}): Kalshi holds nothing, "
                f"so the slot is free again"
            )
        except Exception as exc:  # noqa: BLE001 - one bad row never stops the pass
            summary["errors"].append(f"{ticker}: {type(exc).__name__}: {exc}")

    # Pass 2: Kalshi holdings with no local row protecting them. Real money,
    # unmanaged, invisible to every exit path.
    #
    # Kalshi expresses position_fp in YES contracts, so a negative number is a
    # long NO holding, not a naked short: it is flattened by SELLING NO. A
    # genuine naked short (YES sold without ever holding it) leaves the same
    # negative number, so the newest fill is the tiebreak - a last fill of
    # "sell yes" against a negative position is the short, and covering it
    # spends real cash, so it is reported unless the operator opted in.
    orphans = {t: q for t, q in held.items() if t not in protected}
    for ticker, net_yes in sorted(orphans.items())[:max_liquidations]:
        held_side = "yes" if net_yes > 0 else "no"
        contracts = int(abs(net_yes))
        if contracts < 1:
            # Under one whole contract (a dust position from rounding): there
            # is nothing to sell, so say so instead of pretending to act.
            summary["dust"] += 1
            summary["dust_tickers"].append(
                {"ticker": ticker, "shares": net_yes, "side": held_side}
            )
            logger.info(
                f"REAPER left dust on {ticker}: {net_yes:.2f} net {held_side.upper()} "
                f"(< 1 contract, unsellable)"
            )
            continue
        fill = await _newest_fill(client, ticker)
        fill_action = str((fill or {}).get("action") or "").strip().lower()
        fill_side = str((fill or {}).get("side") or "").strip().lower()
        naked_short = net_yes < 0 and fill_action == "sell" and fill_side == "yes"
        if naked_short:
            if not allow_shorts:
                summary["shorts_flagged"].append(
                    {
                        "ticker": ticker,
                        "net_yes": net_yes,
                        "detail": "last fill was a SELL of yes against a negative position",
                    }
                )
                logger.warning(
                    f"REAPER flagged NAKED SHORT {ticker}: {net_yes:.2f} net YES "
                    f"with no local row (last fill: sold YES we did not hold). "
                    f"Set LIVE_REAP_SHORTS=1 to buy it back automatically."
                )
                continue
            result = await _short_cover(client, ticker, "yes", contracts)
        else:
            result = await _orphan_sell(client, ticker, held_side, contracts)
        if result.get("ok"):
            summary["liquidated"].append(
                {
                    "ticker": ticker,
                    "side": result.get("side"),
                    "contracts": result.get("contracts"),
                    "price": result.get("price"),
                    "notional": round(
                        float(result.get("contracts") or 0)
                        * float(result.get("price") or 0.0),
                        2,
                    ),
                }
            )
            logger.warning(
                f"REAPER liquidated orphan {ticker}: sold {result.get('contracts')} "
                f"{str(result.get('side')).upper()} @ {result.get('price')} "
                f"(no local row was tracking it)"
            )
        else:
            summary["errors"].append(f"{ticker}: {result.get('reason')}")

    # Confirm: what Kalshi still carries after the pass.
    try:
        after, flat_after = await _holdings(client)
        summary["holdings_after"] = len(after)
        summary["flat_rows_after"] = flat_after
        summary["remaining"] = {t: q for t, q in sorted(after.items())}
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"verify: {type(exc).__name__}: {exc}")

    global _LAST_SUMMARY
    _LAST_SUMMARY = summary
    _persist(summary)
    logger.info(
        f"LIVE reaper pass: {summary['phantom_closed']} phantom rows closed, "
        f"{len(summary['liquidated'])} orphan holdings liquidated, "
        f"{len(summary['shorts_flagged'])} shorts flagged, "
        f"{summary.get('holdings_after', '?')} Kalshi holdings left"
    )
    return summary


class LiveReaper:
    """Periodic reaper bound to the trader's run loop."""

    def __init__(
        self,
        db_manager: Any,
        client: Any,
        interval_sec: float = REAP_INTERVAL_SEC,
        allow_shorts: Optional[bool] = None,
    ) -> None:
        self.db_manager = db_manager
        self.client = client
        self.interval_sec = float(interval_sec)
        self.allow_shorts = allow_shorts
        self._next_at = 0.0

    def due(self, now_ts: float) -> bool:
        return now_ts >= self._next_at

    async def maybe_run(self, force: bool = False) -> Optional[Dict[str, Any]]:
        if not force and not self.due(_now_ts()):
            return None
        self._next_at = _now_ts() + self.interval_sec
        try:
            return await reap_live_book(
                self.db_manager, self.client, allow_shorts=self.allow_shorts
            )
        except Exception as exc:  # noqa: BLE001 - the reaper never kills the loop
            logger.error(f"LIVE reaper pass failed: {type(exc).__name__}: {exc}")
            return None


def _now_ts() -> float:
    import time

    return time.time()