"""
Position Tracking Job

This job monitors open positions and implements smart exit strategies:
- Market resolution (original)
- Stop-loss exits
- Take-profit exits  
- Time-based exits
- Confidence-based exits
"""
import asyncio
from datetime import datetime, timedelta
from typing import Any, Optional

from src.clients.kalshi_client import KalshiClient
from src.config.settings import settings
from src.utils.database import DatabaseManager, Position, TradeLog
from src.utils.logging_setup import get_trading_logger, setup_logging
from src.utils.market_prices import get_market_prices
from src.utils.mode import MODE_LIVE

# In-memory throttle for the LIVE settlement reconciler: position id ->
# epoch seconds of the last receipt check with no result. Keeps unreceipted
# rows from costing a settlements call every few seconds. Process-local by
# design; a restart simply re-checks, which is safe (read-only until receipt).
_RECON_CHECKED_AT: dict = {}
_RECON_RECHECK_SECONDS = 3600.0


def _current_mode() -> str:
    """The book currently being traded, for stamping on writes."""
    import os as _os

    from src.utils.database import _resolve_current_mode

    return _resolve_current_mode(_os.getenv("DB_PATH", "trading_system.db"))


def _hours_since(ts: Optional[datetime]) -> float:
    """Hours since ts, matching its timezone-awareness.

    DB rows come back timezone-AWARE (ISO with +00:00) while this module
    mostly builds naive datetimes; subtracting across the two raises
    "can't subtract offset-naive and offset-aware datetimes" - which was
    crashing every tracking pass for those positions, leaving closes
    half-done and feeding a duplicate-sell loop.
    """
    if ts is None:
        return 0.0
    if getattr(ts, "tzinfo", None) is not None:
        from datetime import timezone

        now = datetime.now(timezone.utc)
    else:
        now = datetime.now()
    return (now - ts).total_seconds() / 3600


async def _confirm_fill(kalshi_client, position: Position, limit_price: float) -> bool:
    """Best-effort check that a submitted sell actually filled.

    Kalshi exposes the portfolio position rather than a per-order fill lookup,
    so this asks whether the contract is still held. If the holding has gone
    (or cannot be read), the position is treated as filled - a read failure must
    not strand a position that really did sell, so this errs toward closing.
    """
    try:
        raw = await kalshi_client.get_positions()
    except Exception as e:  # noqa: BLE001 - unknown state, do not block the exit
        get_trading_logger("position_tracking").warning(
            f"Could not verify fill for {position.market_id} ({e}); assuming filled."
        )
        return True

    for bucket in ("market_positions", "event_positions"):
        for entry in (raw or {}).get(bucket) or []:
            ticker = entry.get("ticker") or entry.get("event_ticker")
            if ticker != position.market_id:
                continue
            held = entry.get("position_fp", entry.get("total_cost_shares_fp"))
            try:
                # A zero/empty holding means the contract is gone: filled.
                return float(held or 0) <= 0
            except (TypeError, ValueError):
                return False
    # Not listed under this market at all -> no longer held.
    return True


def _infer_strategy(position: Position) -> str:
    """Best-effort strategy attribution for positions created before the column.

    New positions carry `strategy` directly. This only exists for legacy rows so
    a close is never filed as 'unattributed' with no explanation.
    """
    if position.strategy:
        return position.strategy
    rationale = (position.rationale or "").upper()
    for marker, name in (
        ("QUICK FLIP", "quick_flip_scalping"),
        ("SAFE COMPOUND", "safe_compounder"),
        ("MARKET MAK", "market_making"),
        ("AI ", "ai_directional"),
    ):
        if marker in rationale:
            return name
    return "unattributed"


async def _get_orderbook_volume(kalshi_client, market_id: str, side: str, price: float) -> float:
    """
    Get total volume available at or better than the given price level.
    Returns volume in contracts available for execution.
    """
    try:
        orderbook = await kalshi_client.get_orderbook(market_id, depth=50)
        orderbook_data = orderbook.get("orderbook", {})
        
        asks = orderbook_data.get("asks", [])
        bids = orderbook_data.get("bids", [])
        
        total_volume = 0.0
        side_lower = side.lower()
        
        if side_lower == "yes":
            # For YES, we're selling - need bids
            for bid in bids:
                bid_price = float(bid.get("price_dollars", 0) or bid.get("price", 0) or 0)
                if bid_price >= price:
                    total_volume += float(bid.get("quantity", 0))
        else:
            # For NO, we're selling - need bids
            for bid in bids:
                bid_price = float(bid.get("price_dollars", 0) or bid.get("price", 0) or 0)
                if bid_price >= price:
                    total_volume += float(bid.get("quantity", 0))
        
        return total_volume
    except Exception:
        # If we can't get orderbook, return 0 (conservative)
        return 0.0


async def should_exit_position(
    position: Position,
    current_yes_price: float,
    current_no_price: float,
    market_status: str,
    market_result: Optional[str] = None,
    kalshi_client: Optional[Any] = None,
) -> tuple[bool, str, float]:
    """
    Determine if position should be exited based on smart exit strategies.

    Returns:
        (should_exit, exit_reason, exit_price)
    """
    current_price = current_yes_price if position.side == "YES" else current_no_price

    # 1. Market resolution (original logic)
    if market_status == "closed":
        # If market resolved, use the result to determine exit price
        if market_result:
            exit_price = 1.0 if market_result == position.side else 0.0
        else:
            # Fallback to current price if no result available
            exit_price = current_price
        return True, "market_resolution", exit_price

    # 2. ENHANCED Stop-loss exit using proper logic for YES/NO positions
    #
    # Every exit below this line prices against `current_price`. An unquoted
    # market reports 0.0, and a NO position priced at 0.0 satisfies
    # `0.0 <= take_profit_price` on every single pass - so it fires take-profit
    # at exit_price 0.0, which the $0 sanity guard in track_positions then
    # refuses to write. The position is stranded open, re-detected and re-refused
    # every cycle, forever. That is what left the DRY book sitting at 17 open /
    # 0 closed.
    #
    # There is no exit to take against a $0 quote anyway: nothing can be sold at
    # zero. So hold, and let a later cycle act once the market is quoted again.
    if current_price <= 0.0:
        return False, "", current_price

    if position.stop_loss_price:
        from src.utils.stop_loss_calculator import StopLossCalculator

        should_trigger = StopLossCalculator.is_stop_loss_triggered(
            position_side=position.side,
            entry_price=position.entry_price,
            current_price=current_price,
            stop_loss_price=position.stop_loss_price,
        )

        if should_trigger:
            # Validate there's sufficient volume to exit at stop price.
            # The old code referenced a `kalshi_client` that was never a
            # parameter of this function - a NameError every time a stop
            # triggered, crashing the tracking pass for that position
            # ("name 'kalshi_client' is not defined"). The client arrives as
            # an argument now; with no client the volume filter is skipped
            # and the stop is allowed to fire - a stop that cannot be checked
            # must still be able to fire.
            if kalshi_client is None:
                expected_pnl = StopLossCalculator.calculate_pnl_at_stop_loss(
                    entry_price=position.entry_price,
                    stop_loss_price=position.stop_loss_price,
                    quantity=position.quantity,
                    side=position.side,
                )
                return True, f"stop_loss_triggered_pnl_{expected_pnl:.2f}", current_price
            min_volume_required = position.quantity * 5
            available_volume = await _get_orderbook_volume(
                kalshi_client, position.market_id, position.side, position.stop_loss_price
            )
            # Unknown depth (unreadable/empty book reads 0) must not block a
            # stop: a stranded loser is worse than a resting sell order.
            # Only a genuinely thin read (some depth, below the 5x rule)
            # holds the position back.
            if available_volume <= 0.0 or available_volume >= min_volume_required:
                # Calculate the actual loss to log it
                expected_pnl = StopLossCalculator.calculate_pnl_at_stop_loss(
                    entry_price=position.entry_price,
                    stop_loss_price=position.stop_loss_price,
                    quantity=position.quantity,
                    side=position.side,
                )
                return True, f"stop_loss_triggered_pnl_{expected_pnl:.2f}", current_price
            else:
                # Not enough volume at stop price - log and hold
                logger = get_trading_logger("exit_tracker")
                logger.warning(
                    f"Stop loss at ${position.stop_loss_price:.3f} for {position.market_id} "
                    f"blocked: only ${available_volume:.0f} contracts available (need ${min_volume_required:.0f})"
                )
                return False, "insufficient_volume_at_stop", current_price

    # 3. Take-profit exit (enhanced logic for YES/NO)
    if position.take_profit_price:
        take_profit_triggered = False

        if position.side == "YES":
            # For YES positions, take profit when price rises above target
            take_profit_triggered = current_price >= position.take_profit_price
        else:
            # For NO positions, take profit when price falls below target
            take_profit_triggered = current_price <= position.take_profit_price

        if take_profit_triggered:
            # Volume check only with a live client; no client must not crash
            # the exit path (same NameError bug the stop branch carried).
            if kalshi_client is None:
                return True, "take_profit", current_price
            min_volume_required = position.quantity * 5
            available_volume = await _get_orderbook_volume(
                kalshi_client, position.market_id, position.side, position.take_profit_price
            )
            # Same rule as the stop branch: no depth data -> let the exit
            # fire (the order can rest); only a truly thin book blocks.
            if available_volume <= 0.0 or available_volume >= min_volume_required:
                return True, "take_profit", current_price
            else:
                # Not enough volume - log and hold
                logger = get_trading_logger("exit_tracker")
                logger.warning(
                    f"Take profit at ${position.take_profit_price:.3f} for {position.market_id} "
                    f"blocked: only ${available_volume:.0f} contracts available (need ${min_volume_required:.0f})"
                )
                return False, "insufficient_volume_at_take_profit", current_price

    # 4. Time-based exit
    if position.max_hold_hours:
        hours_held = _hours_since(position.timestamp)
        if hours_held >= position.max_hold_hours:
            return True, "time_based", current_price

    # 5. Emergency exit for positions without stop-loss (legacy positions)
    if not position.stop_loss_price:
        # Calculate emergency stop-loss at 10% loss
        from src.utils.stop_loss_calculator import StopLossCalculator

        emergency_stop = StopLossCalculator.calculate_simple_stop_loss(
            entry_price=position.entry_price,
            side=position.side,
            stop_loss_pct=0.10,  # 10% emergency stop
        )

        emergency_triggered = StopLossCalculator.is_stop_loss_triggered(
            position_side=position.side,
            entry_price=position.entry_price,
            current_price=current_price,
            stop_loss_price=emergency_stop,
        )

        if emergency_triggered:
            return True, "emergency_stop_loss_10pct", current_price

    # 6. Confidence-based exit (placeholder - would need re-analysis)
    # This would require periodic re-analysis, which we're avoiding for cost reasons
    # Could be implemented as a separate, less frequent job

    return False, "", current_price


async def calculate_dynamic_exit_levels(position: Position) -> dict:
    """Calculate smart exit levels using Grok4 recommendations."""
    from src.utils.stop_loss_calculator import StopLossCalculator

    # Use the centralized stop-loss calculator
    exit_levels = StopLossCalculator.calculate_stop_loss_levels(
        entry_price=position.entry_price,
        side=position.side,
        confidence=position.confidence or 0.7,
        market_volatility=0.2,  # Default volatility estimate
        time_to_expiry_days=30.0,  # Default time estimate
    )

    return exit_levels


async def _reconcile_live_settled(db_manager, kalshi_client, logger) -> dict:
    """Close stale LIVE rows Kalshi confirms settled. LIVE-only, DRY untouched.

    Only acts on open mode='live' rows older than 3 hours, at most 10 per
    pass, and only when /portfolio/settlements returns a receipt for the
    exact ticker. No receipt means the row stays open -- a close is never
    invented. Closes are claimed first so the tracking loop below skips them.

    Rows already checked without a receipt are skipped for an hour
    (in-memory): without this the same unreceipted rows cost one settlements
    call every few seconds and the pass never progresses past them.
    """
    from datetime import timezone

    from src.jobs import live_fees as _live_fees

    checked_at = _RECON_CHECKED_AT

    result = {"reconciled": 0, "unreconciled": 0, "checked": 0}
    try:
        rows = await db_manager.get_open_positions(mode=MODE_LIVE)
    except Exception:  # noqa: BLE001
        return result
    now = datetime.now(timezone.utc)
    for position in rows:
        if result["checked"] >= 10:
            break
        try:
            ts = position.timestamp
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_hours = (now - ts).total_seconds() / 3600.0
        except Exception:  # noqa: BLE001 - unparsable age: leave open
            continue
        if age_hours < 3.0:
            continue
        # Throttle: a row checked within the hour with no receipt is skipped
        # until the window passes, so the pass reaches new rows instead of
        # re-burning API calls on the same unreceipted ones.
        try:
            import time as _time

            _last = float(checked_at.get(position.id, 0.0))
            if _time.time() - _last < _RECON_RECHECK_SECONDS:
                continue
        except Exception:  # noqa: BLE001 - throttle never blocks a check
            pass
        result["checked"] += 1
        try:
            if position.id is not None and not await db_manager.claim_position_for_close(
                position.id
            ):
                continue
            claimed = position.id is not None
            try:
                payload = await kalshi_client.get_settlements(ticker=position.market_id)
            except Exception:  # noqa: BLE001 - API error: release, retry later
                if claimed:
                    await db_manager.release_position_claim(position.id)
                continue
            receipt = _live_fees.parse_settlement_result(payload, position.market_id)
            if receipt is None:
                # A market Kalshi still returns with status closed is
                # closable right now: read its result (win pays 1.0, loss 0.0)
                # instead of waiting on a receipt that may never come.
                # Anything still trading, or gone without answers, is handled
                # by the expiry path below or left open.
                try:
                    _m = await kalshi_client.get_market(position.market_id)
                    _md = (_m or {}).get("market") or {}
                except Exception:  # noqa: BLE001 - gone from the API
                    _md = {}
                _status = str(_md.get("status", "") or "").lower()
                if _status == "closed":
                    try:
                        _res = str(_md.get("result", "") or "").lower()
                        _side = str(position.side or "").lower()
                        if _res in ("yes", "no"):
                            _exit = 1.0 if _res == _side else 0.0
                        else:
                            _exit = 0.0
                        _entry = float(position.entry_price or 0.0)
                        _qty = int(position.quantity or 0)
                        _fee = float(
                            _live_fees.roundtrip_fee_dollars(
                                _entry, _exit, _qty, maker_exit=False
                            )
                        )
                        _log = TradeLog(
                            market_id=position.market_id,
                            side=position.side,
                            entry_price=position.entry_price,
                            exit_price=_exit,
                            quantity=position.quantity,
                            pnl=round((_exit - _entry) * _qty - _fee, 2),
                            entry_timestamp=position.timestamp,
                            exit_timestamp=datetime.now(),
                            rationale=(
                                f"{position.rationale} | EXIT:"
                                f" market_resolution (closed book read)"
                                f" | LIVE fees ${_fee:.2f} (net PnL)"
                            ),
                            strategy=position.strategy
                            or _infer_strategy(position),
                            exit_reason="market_resolution",
                            mode="live",
                            fee_paid=round(_fee, 2),
                        )
                        await db_manager.add_trade_log(_log)
                        if position.id is not None:
                            await db_manager.update_position_status(
                                position.id, "closed"
                            )
                        result["reconciled"] += 1
                        logger.info(
                            f"LIVE closed-book {position.market_id} as"
                            f" market_resolution at {_exit:.2f}"
                            f" (net ${_log.pnl:.2f})"
                        )
                        continue
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            f"LIVE closed-book close failed for"
                            f" {position.market_id}: {exc}"
                        )
                # Last resort for ancient rows: older than 6h, no receipt,
                # AND the market itself unresolvable (expired off the API).
                # Kalshi auto-settled these long ago; the cash truth already
                # reflects the outcome, so the local pin is closed at 0.0 and
                # the cap is freed. Anything resolvable or younger stays open.
                if age_hours >= 6.0:
                    try:
                        _m = await kalshi_client.get_market(position.market_id)
                        _resolvable = bool((_m or {}).get("market"))
                    except Exception:  # noqa: BLE001 - gone from the API
                        _resolvable = False
                    if not _resolvable:
                        try:
                            _entry = float(position.entry_price or 0.0)
                            _qty = int(position.quantity or 0)
                            _fee = float(
                                _live_fees.taker_fee_dollars(_entry, _qty)
                            )
                            _log = TradeLog(
                                market_id=position.market_id,
                                side=position.side,
                                entry_price=position.entry_price,
                                exit_price=0.0,
                                quantity=position.quantity,
                                pnl=round(-_entry * _qty - _fee, 2),
                                entry_timestamp=position.timestamp,
                                exit_timestamp=datetime.now(),
                                rationale=(
                                    f"{position.rationale} | EXIT:"
                                    f" expired-unresolved (no receipt, market"
                                    f" gone >6h) | LIVE entry fee"
                                    f" ${_fee:.2f}"
                                ),
                                strategy=position.strategy
                                or _infer_strategy(position),
                                exit_reason="expired-unresolved",
                                mode="live",
                                fee_paid=round(_fee, 2),
                            )
                            await db_manager.add_trade_log(_log)
                            if position.id is not None:
                                await db_manager.update_position_status(
                                    position.id, "closed"
                                )
                            result["reconciled"] += 1
                            logger.info(
                                f"LIVE expired {position.market_id} closed at"
                                f" 0.0 (no receipt, market gone;"
                                f" net ${_log.pnl:.2f})"
                            )
                            continue
                        except Exception as exc:  # noqa: BLE001
                            logger.error(
                                f"LIVE expiry close failed for"
                                f" {position.market_id}: {exc}"
                            )
                if claimed:
                    await db_manager.release_position_claim(position.id)
                try:
                    import time as _time2

                    checked_at[position.id] = _time2.time()
                except Exception:  # noqa: BLE001
                    pass
                result["unreconciled"] += 1
                continue
            side = str(position.side or "").lower()
            exit_price = 1.0 if receipt["result"] == side else 0.0
            gross = (exit_price - float(position.entry_price or 0.0)) * int(
                position.quantity or 0
            )
            fee_paid = float(receipt.get("fees") or 0.0)
            if fee_paid <= 0:
                fee_paid = float(
                    _live_fees.roundtrip_fee_dollars(
                        float(position.entry_price or 0.0),
                        exit_price,
                        int(position.quantity or 0),
                        maker_exit=False,
                    )
                )
            trade_log = TradeLog(
                market_id=position.market_id,
                side=position.side,
                entry_price=position.entry_price,
                exit_price=exit_price,
                quantity=position.quantity,
                pnl=round(gross - fee_paid, 2),
                entry_timestamp=position.timestamp,
                exit_timestamp=datetime.now(),
                rationale=(
                    f"{position.rationale} | EXIT: market_resolution "
                    f"(LIVE settlement receipt) | LIVE fees ${fee_paid:.2f} (net PnL)"
                ),
                strategy=position.strategy or _infer_strategy(position),
                exit_reason="market_resolution",
                mode="live",
                fee_paid=round(fee_paid, 2),
            )
            await db_manager.add_trade_log(trade_log)
            if position.id is not None:
                await db_manager.update_position_status(position.id, "closed")
            result["reconciled"] += 1
            logger.info(
                f"LIVE reconciled {position.market_id} as market_resolution "
                f"at {exit_price:.2f} (receipt {receipt['result']}, "
                f"net ${trade_log.pnl:.2f})"
            )
        except Exception as exc:  # noqa: BLE001 - one bad row never stops recon
            logger.error(f"LIVE reconcile failed for {position.market_id}: {exc}")
            try:
                if position.id is not None:
                    await db_manager.release_position_claim(position.id)
            except Exception:  # noqa: BLE001
                pass
            continue
    if result["checked"]:
        logger.info(
            f"LIVE settlement reconciliation: {result['reconciled']} closed on "
            f"receipt, {result['unreconciled']} left open (no receipt) "
            f"of {result['checked']} stale rows checked"
        )
    return result


async def run_tracking(db_manager: Optional[DatabaseManager] = None):
    """
    Enhanced position tracking with smart exit strategies and sell limit orders.

    Args:
        db_manager: Optional DatabaseManager instance for testing.
    """
    logger = get_trading_logger("position_tracking")
    logger.info("Starting enhanced position tracking job with sell limit orders.")

    if db_manager is None:
        db_manager = DatabaseManager()
        await db_manager.initialize()

    kalshi_client = KalshiClient()

    try:
        # Step 1: Place sell limit orders for profit-taking and stop-loss
        from src.jobs.execute import place_profit_taking_orders, place_stop_loss_orders

        logger.info("🎯 Checking for profit-taking opportunities...")
        mode = _current_mode()
        is_live = mode == MODE_LIVE
        # No time-of-day lock: an exit is taken when the book says take it.
        profit_results = await place_profit_taking_orders(
            db_manager=db_manager,
            kalshi_client=kalshi_client,
            profit_threshold=0.20,  # 20% profit target
            live_mode=is_live,
        )

        logger.info("🛡️ Checking for stop-loss protection...")
        stop_loss_results = await place_stop_loss_orders(
            db_manager=db_manager,
            kalshi_client=kalshi_client,
            stop_loss_threshold=-0.15,  # 15% stop loss
            live_mode=is_live,
        )

        total_sell_orders = profit_results["orders_placed"] + stop_loss_results["orders_placed"]
        if total_sell_orders > 0:
            logger.info(f"📈 SELL LIMIT ORDERS SUMMARY: {total_sell_orders} orders placed")
            logger.info(f"   Profit-taking: {profit_results['orders_placed']} orders")
            logger.info(f"   Stop-loss: {stop_loss_results['orders_placed']} orders")

        # LIVE-ONLY stale-settlement reconciliation: local LIVE rows on
        # long-expired buckets never see an exit signal (their quote reads 0),
        # so they sit open forever and pin deployed capital above
        # max_open_notional -- which blocks every new LIVE entry. Kalshi's own
        # /portfolio/settlements receipt is ground truth: close rows it
        # confirms as market_resolution, leave everything else open. DRY is
        # never touched by this function.
        if is_live:
            try:
                await _reconcile_live_settled(db_manager, kalshi_client, logger)
            except Exception as exc:  # noqa: BLE001 - recon never blocks tracking
                logger.error(f"LIVE settlement reconciliation failed: {exc}")

        # Step 2: Continue with existing position tracking (market resolution, etc.)
        # Use get_open_live_positions() which resolves the current book mode
        # from the DB path, matching the mode computed above.
        open_positions = await db_manager.get_open_live_positions()

        if not open_positions:
            logger.info("No open positions to track.")
            return

        logger.info(f"Found {len(open_positions)} open positions to track.")

        resolution_exits = 0  # markets that auto-settled — no sell order needed
        exit_sell_orders_placed = 0  # sell orders we successfully placed
        exit_sell_failures = 0  # exits we tried to execute but couldn't place a sell
        for position in open_positions:
            try:
                # Get current market data
                market_response = await kalshi_client.get_market(position.market_id)
                market_data = market_response.get("market", {})

                if not market_data:
                    logger.warning(
                        f"Could not retrieve market data for {position.market_id}. Skipping."
                    )
                    continue

                # Get current prices.
                #
                # `yes_price` / `no_price` do not exist in Kalshi API v2 - the
                # quotes live in `*_dollars` string fields. Reading the old names
                # defaulted to 0, so every exit was priced at $0.00: NO positions
                # tripped take-profit on 0.0 <= target, YES positions tripped
                # stop-loss on 0.0, and the $0 sanity guard then refused to write
                # the close. Net effect - the entire exit system was dead and
                # positions accumulated open forever (17 open, 0 closed).
                # get_market_prices() handles both the v2 and legacy shapes.
                yes_bid, yes_ask, no_bid, no_ask = get_market_prices(market_data)
                current_yes_price = (yes_bid + yes_ask) / 2.0
                current_no_price = (no_bid + no_ask) / 2.0
                market_status = market_data.get("status", "unknown")
                market_result = market_data.get("result")  # Market resolution result

                # If position doesn't have exit strategy set, calculate defaults
                if not position.stop_loss_price and not position.take_profit_price:
                    logger.info(f"Setting up exit strategy for position {position.market_id}")
                    exit_levels = await calculate_dynamic_exit_levels(position)

                    # Update position with exit strategy (this would need a new DB method)
                    # For now, we'll apply them dynamically
                    position.stop_loss_price = exit_levels["stop_loss_price"]
                    position.take_profit_price = exit_levels["take_profit_price"]
                    position.max_hold_hours = exit_levels["max_hold_hours"]
                    position.target_confidence_change = exit_levels["target_confidence_change"]

                # Check if position should be exited (market resolution, time-based, etc.)
                should_exit, exit_reason, exit_price = await should_exit_position(
                    position,
                    current_yes_price,
                    current_no_price,
                    market_status,
                    market_result,
                    kalshi_client=kalshi_client,
                )

                if should_exit:
                    # Exactly one process may close a given position. Every
                    # strategy runs book-wide tracking, so without this claim two
                    # of them can read the same open row and both act on it -
                    # two sells, two trade logs, two ledger credits for one
                    # position. Claim before doing anything else.
                    if position.id is not None and not await db_manager.claim_position_for_close(
                        position.id
                    ):
                        logger.info(
                            f"Position {position.market_id} was already claimed for closing "
                            f"by another process; skipping."
                        )
                        continue

                    logger.info(
                        f"Exiting position {position.market_id} due to {exit_reason}. "
                        f"Entry: {position.entry_price:.3f}, Exit: {exit_price:.3f}"
                    )

                    # For non-resolution exits, place a real sell order on Kalshi
                    # before touching the DB. Kalshi auto-settles resolved markets,
                    # so we skip order placement only in the market_resolution case.
                    is_resolution = exit_reason == "market_resolution"
                    # LIVE-ONLY settled-book bypass: a 15-min book that has
                    # expired rolls into asks of $1.00/$1.00 (the collection
                    # shape) or status closed. Selling into it is refused by
                    # build_order_request, which used to strand LIVE capital
                    # open for hours (the 16:45 bucket still open at 23:52).
                    # LIVE closes those as resolutions with no sell order.
                    # DRY keeps the existing attempt-and-fail path untouched.
                    # Per-position book check (not the global mode): legacy and
                    # test rows with no mode set stay on the DRY path.
                    _pos_is_live = str(position.mode or "").lower() == "live"
                    # is_settled_market() check applies to BOTH books:
                    # a closed/rolled book cannot be sold in DRY either.
                    if not is_resolution and (_pos_is_live or str(position.mode or "").lower() == "dry"):
                        try:
                            from src.jobs import live_fees as _live_fees

                            if _live_fees.is_settled_market(market_data):
                                _res = str(market_result or "").lower()
                                _side = str(position.side or "").lower()
                                if _res in ("yes", "no"):
                                    exit_price = (
                                        1.0 if _res == _side else 0.0
                                    )
                                else:
                                    # No result published yet: settle at the
                                    # side's current quote if quoted, else 0.
                                    _cur = (
                                        current_yes_price
                                        if position.side == "YES"
                                        else current_no_price
                                    )
                                    exit_price = float(_cur or 0.0)
                                exit_reason = "market_resolution"
                                is_resolution = True
                                logger.info(
                                    f"LIVE settled-book bypass for "
                                    f"{position.market_id}: closing as "
                                    f"market_resolution at {exit_price:.3f} "
                                    f"instead of selling into a closed book"
                                )
                        except Exception:  # noqa: BLE001 - bypass never blocks
                            pass

                    if not is_resolution:
                        # Sanity guard: a $0 exit on an active market means we're
                        # working from bad market data. Refuse to write a phantom
                        # close — was the source of issue #49.
                        if exit_price <= 0.0:
                            logger.error(
                                f"Refusing to close {position.market_id}: exit_price={exit_price:.3f} "
                                f"on non-resolution exit ({exit_reason}). Likely missing market data; "
                                f"will retry next cycle."
                            )
                            exit_sell_failures += 1
                            if position.id is not None:
                                await db_manager.release_position_claim(position.id)
                            continue

                        # A "take_profit" close that nets zero or less after the
                        # round-trip fees is not a profit - it converts a live
                        # 1/0 resolution chance into a locked-in small loss.
                        # The live log showed take_profit exits realizing
                        # -$0.26/-$0.42/-$0.29 on two-contract clips. Hold to
                        # resolution instead; the market pays 1 or 0 at the
                        # window close regardless.
                        if exit_reason == "take_profit":
                            _entry = float(position.entry_price or 0.0)
                            _qty = int(position.quantity or 0)
                            _fees = (
                                0.07 * _entry * (1.0 - _entry) * _qty
                                + 0.07 * exit_price * (1.0 - exit_price) * _qty
                            )
                            _net = (exit_price - _entry) * _qty - _fees
                            if _net <= 0.0:
                                logger.info(
                                    f"Holding {position.market_id} through resolution: "
                                    f"'take_profit' exit at {exit_price:.3f} would net "
                                    f"{_net:+.2f} after fees ({_qty} @ {_entry:.3f}). "
                                    f"A losing 'profit' is a locked loss; resolution "
                                    f"pays 1 or 0."
                                )
                                if position.id is not None:
                                    await db_manager.release_position_claim(position.id)
                                continue

                        from src.jobs.broker import current_mode
                        from src.jobs.execute import place_sell_limit_order

                        # Exits had no live/paper flag and always placed a real
                        # order. The persisted mode is what decides now.
                        exit_mode = await current_mode()
                        sell_ok = await place_sell_limit_order(
                            position=position,
                            limit_price=exit_price,
                            db_manager=db_manager,
                            kalshi_client=kalshi_client,
                            live_mode=(exit_mode == MODE_LIVE),
                        )
                        if not sell_ok:
                            logger.error(
                                f"Sell order failed for {position.market_id} ({exit_reason}); "
                                f"position remains open. Will retry next cycle."
                            )
                            exit_sell_failures += 1
                            if position.id is not None:
                                await db_manager.release_position_claim(position.id)
                            continue
                        exit_sell_orders_placed += 1

                        # A submitted limit order is not a filled one. Closing the
                        # local position on submission made the DB claim a sale
                        # Kalshi had not made, so P&L and exposure both drifted.
                        # Only close on a confirmed fill; leave it open (and
                        # retry) while the order is merely resting.
                        if exit_mode == MODE_LIVE:
                            filled = await _confirm_fill(kalshi_client, position, exit_price)
                            if not filled:
                                logger.warning(
                                    f"Sell order submitted for {position.market_id} but not "
                                    f"confirmed filled; leaving the position open so the "
                                    f"next cycle can retry or reconcile."
                                )
                                exit_sell_failures += 1
                                continue

                    # Calculate PnL
                    pnl = (exit_price - position.entry_price) * position.quantity
                    # Fee-net accounting: estimate the round-trip
                    # Kalshi fee (taker entry + maker-or-taker exit)
                    # and net it out so trade_logs show what the
                    # account kept. Both LIVE and DRY (when
                    # DRY_FAKE_FEES=1) pay the same fee so the
                    # simulated P&L matches reality.
                    fee_paid = 0.0
                    _mode_str = str(position.mode or "").lower()
                    _fee_is_live = _mode_str == "live"
                    _fee_is_dry = _mode_str == "dry"
                    if _fee_is_live or _fee_is_dry:
                        try:
                            from src.jobs import live_fees as _live_fees2
                            import os as _os

                            _dry_fee_enabled = (
                                _fee_is_live
                                or _os.environ.get("DRY_FAKE_FEES", "1") == "1"
                            )
                            if _dry_fee_enabled:
                                _maker_exit = not is_resolution
                                fee_paid = float(
                                    _live_fees2.roundtrip_fee_dollars(
                                        position.entry_price,
                                        exit_price,
                                        int(position.quantity or 0),
                                        maker_exit=_maker_exit,
                                    )
                                )
                                pnl = round(pnl - fee_paid, 2)
                        except Exception:  # noqa: BLE001 - fees never block a close
                            fee_paid = 0.0

                    # Create trade log.
                    _rationale = f"{position.rationale} | EXIT: {exit_reason}"
                    if fee_paid > 0:
                        _fee_label = "LIVE" if _fee_is_live else "DRY"
                        _rationale += f" | {_fee_label} fees est ${fee_paid:.2f} (net PnL)"
                    trade_log = TradeLog(
                        market_id=position.market_id,
                        side=position.side,
                        entry_price=position.entry_price,
                        exit_price=exit_price,
                        quantity=position.quantity,
                        pnl=pnl,
                        entry_timestamp=position.timestamp,
                        exit_timestamp=datetime.now(),
                        rationale=_rationale,
                        strategy=position.strategy or _infer_strategy(position),
                        exit_reason=exit_reason,
                        mode=position.mode or _current_mode(),
                        fee_paid=round(fee_paid, 2),
                    )

                    # Record the exit. For non-resolution exits the sell order
                    # may still be resting unfilled — the local DB now optimistically
                    # treats it as closed, which mirrors the existing behavior of
                    # place_profit_taking_orders / place_stop_loss_orders.
                    await db_manager.add_trade_log(trade_log)
                    # `id` is None only for a position that was never saved;
                    # writing that back would fail loudly and lose the close entirely.
                    if position.id is None:
                        logger.error(
                            f"Cannot record the close for {position.market_id}: position has no id."
                        )
                        exit_sell_failures += 1
                        continue
                    await db_manager.update_position_status(position.id, "closed")
                    if is_resolution:
                        resolution_exits += 1
                    logger.info(
                        f"Position for market {position.market_id} closed via {exit_reason}. "
                        f"PnL: ${pnl:.2f}"
                    )
                else:
                    # Log current position status for monitoring
                    current_price = (
                        current_yes_price if position.side == "YES" else current_no_price
                    )
                    unrealized_pnl = (current_price - position.entry_price) * position.quantity
                    hours_held = _hours_since(position.timestamp)

                    logger.debug(
                        f"Position {position.market_id} status: "
                        f"Entry: {position.entry_price:.3f}, Current: {current_price:.3f}, "
                        f"Unrealized P&L: ${unrealized_pnl:.2f}, Hours held: {hours_held:.1f}"
                    )

            except Exception as e:
                logger.error(
                    f"Failed to process position for market {position.market_id}.", error=str(e)
                )

        logger.info(
            f"Position tracking completed. "
            f"Profit/SL sell orders: {total_sell_orders}, "
            f"Resolution exits: {resolution_exits}, "
            f"Exit sell orders placed: {exit_sell_orders_placed}, "
            f"Exit failures (still open): {exit_sell_failures}"
        )

    except Exception as e:
        logger.error("Error in position tracking job.", error=str(e), exc_info=True)
    finally:
        await kalshi_client.close()


if __name__ == "__main__":
    setup_logging()
    asyncio.run(run_tracking())
