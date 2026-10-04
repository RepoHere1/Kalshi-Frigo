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
from typing import Optional

from src.clients.kalshi_client import KalshiClient
from src.config.settings import settings
from src.utils.database import DatabaseManager, Position, TradeLog
from src.utils.logging_setup import get_trading_logger, setup_logging
from src.utils.market_prices import get_market_prices
from src.utils.mode import MODE_LIVE


def _current_mode() -> str:
    """The book currently being traded, for stamping on writes."""
    import os as _os

    from src.utils.database import _resolve_current_mode

    return _resolve_current_mode(_os.getenv("DB_PATH", "trading_system.db"))


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


async def should_exit_position(
    position: Position,
    current_yes_price: float,
    current_no_price: float,
    market_status: str,
    market_result: Optional[str] = None,
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
            # Calculate the actual loss to log it
            expected_pnl = StopLossCalculator.calculate_pnl_at_stop_loss(
                entry_price=position.entry_price,
                stop_loss_price=position.stop_loss_price,
                quantity=position.quantity,
                side=position.side,
            )
            return True, f"stop_loss_triggered_pnl_{expected_pnl:.2f}", current_price

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
            return True, "take_profit", current_price

    # 4. Time-based exit
    if position.max_hold_hours:
        hours_held = (datetime.now() - position.timestamp).total_seconds() / 3600
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
        mode = await _current_mode()
        is_live = mode == MODE_LIVE
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

        # Step 2: Continue with existing position tracking (market resolution, etc.)
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
                    position, current_yes_price, current_no_price, market_status, market_result
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

                    # Create trade log.
                    # strategy / exit_reason / mode were all omitted, so every
                    # close landed with strategy NULL (making per-strategy P&L
                    # permanently "unattributed") and with no record of why it
                    # exited or which book it belonged to.
                    trade_log = TradeLog(
                        market_id=position.market_id,
                        side=position.side,
                        entry_price=position.entry_price,
                        exit_price=exit_price,
                        quantity=position.quantity,
                        pnl=pnl,
                        entry_timestamp=position.timestamp,
                        exit_timestamp=datetime.now(),
                        rationale=f"{position.rationale} | EXIT: {exit_reason}",
                        strategy=position.strategy or _infer_strategy(position),
                        exit_reason=exit_reason,
                        mode=position.mode or _current_mode(),
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
                    hours_held = (datetime.now() - position.timestamp).total_seconds() / 3600

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
