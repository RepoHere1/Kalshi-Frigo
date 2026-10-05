"""
Trade Execution Job

This job takes a position and executes it as a trade.
"""
import asyncio
import uuid
from datetime import datetime
from typing import Dict, Optional

from src.clients.kalshi_client import KalshiAPIError, KalshiClient
from src.config.settings import settings
from src.jobs.broker import broker_for_mode, build_order_request, default_mode_manager
from src.utils.database import DatabaseManager, Position
from src.utils.logging_setup import get_trading_logger
from src.utils.market_prices import get_market_prices, is_tradeable_market
from src.utils.mode import MODE_DRY, MODE_LIVE


def _mode_manager():
    """The persisted DRY/LIVE mode store, for the simulated funding source.

    Re-read per call so a mode switch takes effect for the next order without
    needing a restart. Mirrors how database.py resolves its own path.
    """
    import os

    return default_mode_manager(os.getenv("DB_PATH", "trading_system.db"))


async def execute_position(
    position: Position, live_mode: bool, db_manager: DatabaseManager, kalshi_client: KalshiClient
) -> bool:
    """
    Executes a single trade position.

    Args:
        position: The position to execute.
        live_mode: Whether to execute a live or simulated trade.
        db_manager: The database manager instance.
        kalshi_client: The Kalshi client instance.

    Returns:
        True if execution was successful, False otherwise.

    Both branches run the same validation - tradeability, the 1-99c Kalshi
    range, the minimum order size and an affordability check against the
    active funding source. Only the submission differs, and only LiveBroker is
    able to reach the order endpoint. DRY previously skipped all of this and
    unconditionally marked the position live, so a paper "success" proved
    nothing about whether the real order would have been accepted.
    """
    logger = get_trading_logger("trade_execution")
    logger.info(f"🎯 Executing position for market: {position.market_id}")
    logger.info(f"🎛️ {'LIVE' if live_mode else 'DRY'} mode")

    mode = MODE_LIVE if live_mode else MODE_DRY
    mode_manager = _mode_manager()

    # Fail closed BEFORE anything is submitted. The order used to go first and the
    # id check second, so a caller that forgot to persist its position still got a
    # fill: the DRY ledger was debited (and, under LIVE, real money spent) and only
    # then did this function discover there was no row to record the fill against.
    # The cash was gone and the position did not exist, which is unrecoverable and
    # silent. Refusing up front costs nothing and makes an unrecorded fill
    # impossible rather than merely unlikely.
    if position.id is None:
        logger.error(
            f"❌ Refusing to submit {position.market_id}: the position has no id, so "
            f"the fill could not be recorded. Persist it (add_position) before executing."
        )
        return False

    # Per-strategy ceiling, enforced here so it binds every strategy rather than
    # only the ones that remember to check. Each strategy process sizes its own
    # clip against the *global* headroom, so six of them sharing one book filled
    # it between them and one strategy reached 23 concurrent positions.
    strategy_name = (position.strategy or "").strip()
    if strategy_name:
        cap = int(getattr(settings.trading, "max_positions_per_strategy", 0) or 0)
        if cap > 0:
            try:
                already_open = [
                    p
                    for p in await db_manager.get_open_positions(mode=mode)
                    if (p.strategy or "").strip() == strategy_name
                ]
                # This position's own row is already inserted at this point.
                if len(already_open) > cap:
                    logger.warning(
                        f"❌ Refusing {position.market_id}: {strategy_name} already holds "
                        f"{len(already_open) - 1} open position(s), cap is {cap}."
                    )
                    return False
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Per-strategy cap check failed for {position.market_id}: {exc}")
                return False

    try:
        # Live market data drives validation in both modes - a DRY run must see
        # the same prices the exchange would.
        market_data = await kalshi_client.get_market(position.market_id)
        market = market_data.get("market", {})

        broker = broker_for_mode(
            mode,
            kalshi_client=kalshi_client if live_mode else None,
            mode_manager=mode_manager,
        )

        available_cents = await broker.available_cents()
        request, reason = build_order_request(
            market_id=position.market_id,
            side=position.side,
            action="buy",
            quantity=position.quantity,
            market=market,
            available_cents=available_cents,
        )
        if request is None:
            logger.warning(f"⚠️  Skipping {position.market_id}: {reason}")
            return False

        if live_mode:
            logger.warning(
                f"💰 PLACING LIVE ORDER - real money - {position.market_id} "
                f"notional ${request.notional:.2f}"
            )
        else:
            logger.info(
                f"📝 PLACING DRY ORDER - no real money - {position.market_id} "
                f"notional ${request.notional:.2f} against ${available_cents / 100:.2f} "
                f"simulated cash"
            )

        response = await broker.submit(request)
        if response.get("error"):
            logger.error(f"❌ Order rejected for {position.market_id}: {response['error']}")
            return False

        order_id = response.get("order", {}).get("order_id", request.client_order_id)
        fill_price = position.entry_price or request.fill_price

        if live_mode:
            await db_manager.update_position_to_live(position.id, fill_price)
            logger.info(f"✅ LIVE ORDER PLACED for {position.market_id}. Order ID: {order_id}")
            logger.info(f"💰 Real money used: ${request.notional:.2f}")
        else:
            # Deliberately NOT promoted to live. `live` is what distinguishes a
            # real position from a simulated one everywhere downstream - the
            # dashboard's Kalshi fallback, per-strategy P&L and the Kalshi
            # exposure figures all read it. Marking DRY fills as live=1 made the
            # simulated book indistinguishable from the real one.
            logger.info(
                f"✅ DRY ORDER SIMULATED for {position.market_id}. Simulated order ID: {order_id}"
            )
            logger.info(f"📊 Simulated spend: ${request.notional:.2f}")
        return True

    except KalshiAPIError as e:
        logger.error(
            f"❌ FAILED to place {'LIVE' if live_mode else 'DRY'} order "
            f"for {position.market_id}: {e}"
        )
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error executing {position.market_id}: {e}")
        return False


async def place_sell_limit_order(
    position: Position,
    limit_price: float,
    db_manager: DatabaseManager,
    kalshi_client: KalshiClient,
    live_mode: bool = False,
) -> bool:
    """
    Place a sell limit order to close an existing position.

    Routed through the broker like buys, so a DRY run simulates the exit too.
    It previously called place_order unconditionally, which meant DRY only
    guarded entries: an exit would have hit the real endpoint.

    Args:
        position: The position to close
        limit_price: The limit price for the sell order (in dollars)
        db_manager: Database manager
        kalshi_client: Kalshi API client

    Returns:
        True if order placed successfully, False otherwise
    """
    logger = get_trading_logger("sell_limit_order")

    try:
        side = position.side.lower()  # "YES" -> "yes", "NO" -> "no"
        mode = MODE_LIVE if live_mode else MODE_DRY
        # LIVE-ONLY sell dedupe: refuse a sell when no open LIVE quantity is
        # held for this ticker/side. Profit-taking, stop-loss and tracking all
        # scan the same book, and without this each of them credits/sells the
        # same row every cycle (the 962-fills-vs-300-closes inflation seen in
        # DRY). DRY skips this check entirely so its historic ledger behaviour
        # is byte-identical.
        if live_mode:
            try:
                _live_open = await db_manager.get_open_positions(mode=MODE_LIVE)
                _held = sum(
                    int(p.quantity or 0)
                    for p in _live_open
                    if str(p.market_id) == str(position.market_id)
                    and str(p.side or "").upper()
                    == str(position.side or "").upper()
                )
                if _held <= 0:
                    logger.warning(
                        f"Skipping LIVE sell for {position.market_id}: "
                        f"no open LIVE quantity held on {position.side}"
                    )
                    return False
            except Exception:  # noqa: BLE001 - dedupe must not block a real exit
                pass
        mode_manager = _mode_manager()

        # A sell needs the live book so the limit price can be validated the same
        # way a buy is, and so DRY exercises identical checks.
        market_data = await kalshi_client.get_market(position.market_id)
        market = market_data.get("market", {})

        broker = broker_for_mode(
            mode,
            kalshi_client=kalshi_client if live_mode else None,
            mode_manager=mode_manager,
        )
        available_cents = await broker.available_cents()

        request, reason = build_order_request(
            market_id=position.market_id,
            side=side,
            action="sell",
            quantity=position.quantity,
            market=market,
            available_cents=available_cents,
            limit_price_dollars=limit_price,
        )
        if request is None:
            logger.warning(f"⚠️  Skipping sell for {position.market_id}: {reason}")
            return False

        verb = "LIVE SELL LIMIT ORDER" if live_mode else "DRY SELL LIMIT ORDER"
        logger.info(
            f"🎯 Placing {verb}: {position.quantity} {side.upper()} at "
            f"{request.no_price or request.yes_price}¢ for {position.market_id} "
            f"(proceeds ${request.notional:.2f})"
        )

        # The only difference between DRY and LIVE from here down is which broker
        # owns the KalshiClient. DryBroker has no place_order at all.
        response = await broker.submit(request)

        if response.get("error"):
            logger.error(f"❌ Sell rejected for {position.market_id}: {response['error']}")
            return False

        order_id = response.get("order", {}).get("order_id", request.client_order_id)
        logger.info(f"✅ {verb} accepted. Order ID: {order_id}")
        logger.info(f"   Market: {position.market_id}")
        logger.info(f"   Side: {side.upper()} (selling {position.quantity} shares)")
        logger.info(f"   Limit Price: {request.no_price or request.yes_price}¢")
        logger.info(f"   Proceeds: ${request.notional:.2f}")
        return True

    except Exception as e:
        logger.error(f"❌ Error placing sell limit order for {position.market_id}: {e}")
        return False


async def place_profit_taking_orders(
    db_manager: DatabaseManager,
    kalshi_client: KalshiClient,
    profit_threshold: float = 0.25,  # 25% profit target
    live_mode: bool = False,
) -> Dict[str, int]:
    """
    Place sell limit orders for positions that have reached profit targets.

    Args:
        db_manager: Database manager
        kalshi_client: Kalshi API client
        profit_threshold: Minimum profit percentage to trigger sell order

    Returns:
        Dictionary with results: {'orders_placed': int, 'positions_processed': int}
    """
    logger = get_trading_logger("profit_taking")

    results = {"orders_placed": 0, "positions_processed": 0}

    try:
        # Get all open live positions
        positions = await db_manager.get_open_live_positions()

        if not positions:
            logger.info("No open positions to process for profit taking")
            return results

        logger.info(f"📊 Checking {len(positions)} positions for profit-taking opportunities")

        for position in positions:
            try:
                results["positions_processed"] += 1

                # Get current market data
                market_response = await kalshi_client.get_market(position.market_id)
                market_data = market_response.get("market", {})

                if not market_data:
                    logger.warning(f"Could not get market data for {position.market_id}")
                    continue

                # Get current price based on position side.
                #
                # `yes_price` / `no_price` were removed from Kalshi API v2; quotes
                # are `*_dollars` strings. Reading the old names defaulted to 0,
                # and `if current_price > 0` then skipped the position entirely -
                # so profit-taking and stop-losses silently did nothing, forever.
                yes_bid, yes_ask, no_bid, no_ask = get_market_prices(market_data)
                if position.side == "YES":
                    current_price = (yes_bid + yes_ask) / 2.0
                else:
                    current_price = (no_bid + no_ask) / 2.0

                # Calculate current profit
                if current_price > 0:
                    profit_pct = (current_price - position.entry_price) / position.entry_price
                    unrealized_pnl = (current_price - position.entry_price) * position.quantity

                    logger.debug(
                        f"Position {position.market_id}: Entry=${position.entry_price:.3f}, Current=${current_price:.3f}, Profit={profit_pct:.1%}, PnL=${unrealized_pnl:.2f}"
                    )

                    # Check if we should place a profit-taking sell order
                    if profit_pct >= profit_threshold:
                        # DRY keeps the historic 2% discount (taker-style, fast
                        # fill, simulated anyway). LIVE rests at the quote when
                        # time allows (maker fee, ~1/4 the cost) and only takes
                        # when urgent. DRY behaviour is byte-identical.
                        if live_mode:
                            try:
                                from src.jobs import live_fees as _live_fees

                                _secs = None
                                try:
                                    _close_ts = market_data.get("close_time")
                                    if _close_ts:
                                        from datetime import datetime, timezone

                                        _ct = datetime.fromisoformat(
                                            str(_close_ts).replace("Z", "+00:00")
                                        )
                                        if _ct.tzinfo is None:
                                            _ct = _ct.replace(tzinfo=timezone.utc)
                                        _secs = (
                                            _ct - datetime.now(timezone.utc)
                                        ).total_seconds()
                                except Exception:  # noqa: BLE001
                                    _secs = None
                                sell_price = _live_fees.live_exit_limit_price(
                                    position.side, current_price, _secs
                                )
                            except Exception:  # noqa: BLE001
                                sell_price = current_price * 0.98
                        else:
                            sell_price = (
                                current_price * 0.98
                            )  # 2% below current price for quick execution

                        logger.info(
                            f"💰 PROFIT TARGET HIT: {position.market_id} - {profit_pct:.1%} profit (${unrealized_pnl:.2f})"
                        )

                        # Place sell limit order
                        success = await place_sell_limit_order(
                            position=position,
                            limit_price=sell_price,
                            live_mode=live_mode,
                            db_manager=db_manager,
                            kalshi_client=kalshi_client,
                        )

                        if success:
                            results["orders_placed"] += 1
                            logger.info(f"✅ Profit-taking order placed for {position.market_id}")
                        else:
                            logger.error(
                                f"❌ Failed to place profit-taking order for {position.market_id}"
                            )

            except Exception as e:
                logger.error(
                    f"Error processing position {position.market_id} for profit taking: {e}"
                )
                continue

        logger.info(
            f"🎯 Profit-taking summary: {results['orders_placed']} orders placed from {results['positions_processed']} positions"
        )
        return results

    except Exception as e:
        logger.error(f"Error in profit-taking order placement: {e}")
        return results


async def place_stop_loss_orders(
    db_manager: DatabaseManager,
    kalshi_client: KalshiClient,
    stop_loss_threshold: float = -0.10,  # 10% stop loss
    live_mode: bool = False,
) -> Dict[str, int]:
    """
    Place sell limit orders for positions that need stop-loss protection.

    Args:
        db_manager: Database manager
        kalshi_client: Kalshi API client
        stop_loss_threshold: Maximum loss percentage before triggering stop loss

    Returns:
        Dictionary with results: {'orders_placed': int, 'positions_processed': int}
    """
    logger = get_trading_logger("stop_loss_orders")

    results = {"orders_placed": 0, "positions_processed": 0}

    try:
        # Get all open live positions
        positions = await db_manager.get_open_live_positions()

        if not positions:
            logger.info("No open positions to process for stop-loss orders")
            return results

        logger.info(f"🛡️ Checking {len(positions)} positions for stop-loss protection")

        for position in positions:
            try:
                results["positions_processed"] += 1

                # Get current market data
                market_response = await kalshi_client.get_market(position.market_id)
                market_data = market_response.get("market", {})

                if not market_data:
                    logger.warning(f"Could not get market data for {position.market_id}")
                    continue

                # Get current price based on position side (API v2 `*_dollars`
                # fields - `yes_price` no longer exists; see place_profit_taking).
                yes_bid, yes_ask, no_bid, no_ask = get_market_prices(market_data)
                if position.side == "YES":
                    current_price = (yes_bid + yes_ask) / 2.0
                else:
                    current_price = (no_bid + no_ask) / 2.0

                # Calculate current loss
                if current_price > 0:
                    loss_pct = (current_price - position.entry_price) / position.entry_price
                    unrealized_pnl = (current_price - position.entry_price) * position.quantity

                    # Check if we need stop-loss protection
                    if loss_pct <= stop_loss_threshold:  # Negative loss percentage
                        # Calculate stop-loss sell price
                        stop_price = position.entry_price * (
                            1 + stop_loss_threshold * 1.1
                        )  # Slightly more aggressive
                        stop_price = max(0.01, stop_price)  # Ensure price is at least 1¢

                        logger.info(
                            f"🛡️ STOP LOSS TRIGGERED: {position.market_id} - {loss_pct:.1%} loss (${unrealized_pnl:.2f})"
                        )

                        # Place stop-loss sell order
                        success = await place_sell_limit_order(
                            position=position,
                            limit_price=stop_price,
                            live_mode=live_mode,
                            db_manager=db_manager,
                            kalshi_client=kalshi_client,
                        )

                        if success:
                            results["orders_placed"] += 1
                            logger.info(f"✅ Stop-loss order placed for {position.market_id}")
                        else:
                            logger.error(
                                f"❌ Failed to place stop-loss order for {position.market_id}"
                            )

            except Exception as e:
                logger.error(f"Error processing position {position.market_id} for stop loss: {e}")
                continue

        logger.info(
            f"🛡️ Stop-loss summary: {results['orders_placed']} orders placed from {results['positions_processed']} positions"
        )
        return results

    except Exception as e:
        logger.error(f"Error in stop-loss order placement: {e}")
        return results
