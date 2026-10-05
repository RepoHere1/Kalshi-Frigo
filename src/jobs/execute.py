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


async def _confirm_maker_fill(
    kalshi_client: KalshiClient,
    request,
    position: Position,
    wait_seconds: float,
    logger,
    placed_order_id: str = "",
) -> bool:
    """LIVE-only: did the resting entry actually fill? Never guess.

    Polls the resting-orders list for up to `wait_seconds`. An order that
    disappears from the book is checked against the account holding for the
    ticker -- the holding is the only authority, so a partial fill is still
    recorded (loudly, with the size mismatch named) rather than lost. A
    still-resting order is cancelled and the holding re-checked once more;
    nothing held means nothing happened and the caller deletes the row.
    """
    import asyncio as _asyncio

    order_id = str(placed_order_id or request.client_order_id)

    async def _resting() -> bool:
        try:
            resp = await kalshi_client.get_orders(
                ticker=position.market_id, status="resting"
            )
            rows = resp.get("orders") or [] if isinstance(resp, dict) else []
            for o in rows:
                if not isinstance(o, dict):
                    continue
                if str(o.get("client_order_id") or "") == str(
                    request.client_order_id
                ) or str(o.get("order_id") or "") == order_id:
                    return True
        except Exception:  # noqa: BLE001 - a failed read retries on the next poll
            pass
        return False

    async def _held_qty() -> float:
        try:
            raw = await kalshi_client.get_positions(ticker=position.market_id)
        except Exception:  # noqa: BLE001 - unknown: treat as filled, keep the row
            return -1.0
        for bucket in ("market_positions", "event_positions"):
            for entry in (raw or {}).get(bucket) or []:
                ticker = entry.get("ticker") or entry.get("event_ticker")
                if ticker != position.market_id:
                    continue
                try:
                    return float(entry.get("position_fp", 0.0) or 0.0)
                except (TypeError, ValueError):
                    return -1.0
        return 0.0

    deadline = wait_seconds
    while deadline > 0:
        if not await _resting():
            break
        step = min(2.5, deadline)
        await _asyncio.sleep(step)
        deadline -= step

    if await _resting():
        # Never filled: cancel so nothing can execute behind the local book.
        try:
            await kalshi_client.cancel_order(
                order_id, market_ticker=position.market_id
            )
        except Exception as exc:  # noqa: BLE001 - cancel is best-effort
            logger.warning(
                f"Maker entry cancel failed for {position.market_id}: {exc}"
            )

    held = await _held_qty()
    if held != 0.0:
        if held < 0:
            logger.warning(
                f"Maker entry holding unreadable for {position.market_id}; "
                f"keeping the row and reconciling from Kalshi truth"
            )
        elif abs(held - int(position.quantity or 0)) > 1e-9:
            logger.warning(
                f"Maker entry partial fill on {position.market_id}: "
                f"ordered {position.quantity}, holding {held:.2f} - "
                f"the position reconciles from Kalshi truth"
            )
        return True
    return False


async def execute_position(
    position: Position, live_mode: bool, db_manager: DatabaseManager, kalshi_client: KalshiClient,
    maker_wait_seconds: float = 0.0,
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
        # LIVE-ONLY maker entry (item 2): with time on the clock the buy rests
        # at the side's bid (maker fee, ~1/4 of taker) instead of crossing the
        # spread. position.entry_price carries the bid in that case; DRY never
        # takes this branch and keeps its market-style fill at the ask.
        _maker = bool(live_mode and maker_wait_seconds and maker_wait_seconds > 0)
        request, reason = build_order_request(
            market_id=position.market_id,
            side=position.side,
            action="buy",
            quantity=position.quantity,
            market=market,
            available_cents=available_cents,
            limit_price_dollars=(position.entry_price if _maker else None),
        )
        if request is None:
            logger.warning(f"⚠️  Skipping {position.market_id}: {reason}")
            return False

        if live_mode:
            logger.warning(
                f"💰 PLACING LIVE {'MAKER' if _maker else 'TAKER'} ORDER - real money - "
                f"{position.market_id} notional ${request.notional:.2f}"
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

        if live_mode and _maker:
            # A resting limit order is not a fill. Confirm against the book and
            # the account holding; cancel and report unfilled if nothing moved,
            # so the caller can drop the row instead of booking a phantom.
            filled = await _confirm_maker_fill(
                kalshi_client,
                request,
                position,
                float(maker_wait_seconds),
                logger,
                placed_order_id=str(order_id or ""),
            )
            if not filled:
                logger.warning(
                    f"⏳ Maker entry on {position.market_id} did not fill in "
                    f"{maker_wait_seconds:.0f}s; cancelled, nothing left behind"
                )
                return False
            await db_manager.update_position_to_live(position.id, fill_price)
            logger.info(
                f"✅ LIVE MAKER ENTRY FILLED for {position.market_id} @ {fill_price:.3f}"
            )
            return True

        if live_mode:
            await db_manager.update_position_to_live(position.id, fill_price)
            logger.info(
                f"✅ LIVE ORDER PLACED {position.market_id} @ {fill_price:.3f} "
                f"({request.count} contracts, ${request.notional:.2f} notional, "
                f"~${request.notional * 0.03:.2f} fee)"
            )
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


async def _live_holding_yes(kalshi_client, ticker: str):
    """Kalshi's net YES shares for a ticker, or None when it cannot be read.

    Kalshi expresses position_fp in YES contracts: positive is long YES,
    negative is long NO.
    """
    try:
        raw = await kalshi_client.get_positions(ticker=ticker)
    except Exception:  # noqa: BLE001 - unknown, treated as "cannot prove"
        return None
    for bucket in ("market_positions", "event_positions"):
        for entry in (raw or {}).get(bucket) or []:
            if (entry.get("ticker") or entry.get("event_ticker")) != ticker:
                continue
            try:
                return float(entry.get("position_fp") or 0.0)
            except (TypeError, ValueError):
                return None
    return 0.0


async def _confirm_live_sell(kalshi_client, position, before, wait_seconds=8.0, logger=None):
    """LIVE-only: did the sell actually leave the account?

    A resting limit sell is not a sale. Closing the local row on submission
    claimed P&L and freed the slot for a trade Kalshi had not made, which is
    how a real holding ended up with no local row tracking it at all. The
    holding is the only authority: it must shrink toward zero before the
    caller is told the exit happened. An unreadable holding is NOT a fill.
    """
    import asyncio as _asyncio

    deadline = float(wait_seconds)
    while True:
        held = await _live_holding_yes(kalshi_client, position.market_id)
        if held is None:
            if logger:
                logger.warning(
                    f"LIVE sell of {position.market_id}: holding unreadable, "
                    f"treated as NOT filled so the row stays open"
                )
            return False
        moved = (
            abs(held) < abs(before) - 1e-9
            if before is not None
            else abs(held) < int(position.quantity or 0) - 1e-9
        )
        if moved:
            return True
        if deadline <= 0:
            return False
        step = min(2.5, deadline)
        await _asyncio.sleep(step)
        deadline -= step


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

        # LIVE-ONLY: Kalshi accepts a sell of contracts the account does not
        # hold and turns it into a negative position - a real short created by
        # a local row that no longer has anything behind it. The holding is
        # read first and the sell refused unless it can cover the quantity.
        held_before = None
        if live_mode:
            held_before = await _live_holding_yes(kalshi_client, position.market_id)
            want = int(position.quantity or 0)
            if held_before is None:
                logger.warning(
                    f"⚠️  Refusing LIVE sell for {position.market_id}: Kalshi holding "
                    f"unreadable, so the sell cannot be proven safe"
                )
                return False
            held_side = "YES" if held_before > 0 else "NO"
            covered = held_before if side == "yes" else -held_before
            if covered < want:
                logger.warning(
                    f"⚠️  Refusing LIVE sell for {position.market_id}: local row says "
                    f"{want} {side.upper()} but Kalshi holds {abs(held_before):.2f} net "
                    f"{held_side}. Selling anyway would open a real short; the local "
                    f"row is stale and the reaper will close it."
                )
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

        if live_mode:
            # LIVE-only: a resting limit sell is not a sale. Callers credit
            # P&L and close the local row on a True return, so True now means
            # the holding actually shrank at Kalshi. Unfilled -> cancel the
            # resting order and report False, leaving the row open for the
            # next cycle to retry. DRY is untouched: its broker fills at once.
            filled = await _confirm_live_sell(
                kalshi_client, position, held_before, wait_seconds=8.0, logger=logger
            )
            if not filled:
                try:
                    await kalshi_client.cancel_order(
                        order_id, market_ticker=position.market_id
                    )
                    logger.warning(
                        f"⏳ LIVE sell of {position.market_id} did not fill in 8s; "
                        f"cancelled, position stays open"
                    )
                except Exception as exc2:  # noqa: BLE001 - best effort cancel
                    logger.warning(
                        f"LIVE sell cancel failed for {position.market_id}: {exc2}"
                    )
                return False
            logger.info(f"✅ LIVE SELL CONFIRMED FILLED for {position.market_id}")
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
                                # Item 4b: a winning position inside the
                                # settlement window rides to the $1.00 print
                                # instead of scalping out for a few cents and
                                # paying a second fee. Losing positions keep
                                # every exit they had.
                                if (
                                    _secs is not None
                                    and 0 < _secs <= _live_fees.SETTLEMENT_WINDOW_SECONDS
                                    and current_price > float(position.entry_price or 0.0)
                                ):
                                    logger.info(
                                        f"🏆 Riding {position.market_id} to settlement: "
                                        f"{_secs:.0f}s left, in profit at "
                                        f"{current_price:.3f} vs entry "
                                        f"{position.entry_price:.3f} - no scalp"
                                    )
                                    continue
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
