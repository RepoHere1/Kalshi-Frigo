"""Real-time BTC market data for the dashboard and the 15-minute trader.

THE MARKET THAT ACTUALLY EXISTS
-------------------------------
Series `KXBTC15M`, event `KXBTC15M-26OCT011715`, market
`KXBTC15M-26OCT011715-15` - "BTC price up in next 15 mins?", a binary
`greater_or_equal` contract on CF Benchmarks' BRTI.

Three details of this feed are easy to get wrong, and each one silently
produced "no quote" before:

1. The horizon is a *suffix on the ticker* (`-15`, `-30`, ...). The bucket is
   the event ticker; one event carries several horizons. Filtering on
   `series_ticker` alone returns all of them.
2. Prices live in the `*_dollars` string fields (`yes_bid_dollars: "0.5200"`).
   The integer-cent `yes_bid`/`yes_ask` fields are null on this series, so
   reading those reports an empty book on a market that is actively trading.
3. `floor_strike` is the target price the contract resolves against.

SETTLEMENT, AND WHY THE EDGE IS NOT WHAT IT LOOKS LIKE
-----------------------------------------------------
Resolution is the simple average of sixty CF Benchmarks BRTI prints in the final
minute before expiry, compared against the same average for the previous window
(see `rules_primary`). So:

* The settlement price is a 60-second *average of a composite index*, not a spot
  print. A retail spot feed is a proxy for it, never the thing itself.
* Nothing here claims CF Benchmarks' index lags spot by any particular amount.
  The trader measures the gap between the live spot feed and Kalshi's own quote
  and refuses to trade when it cannot see one.
"""
import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

import aiohttp

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
BTC15M_SERIES = "KXBTC15M"

# A spot tick older than this is not used to make a trading decision. Trading on
# a stale "leading" price inverts the entire premise.
SPOT_MAX_AGE_SEC = 5.0
SERIES_POINTS = 180


def _now() -> float:
    return time.time()


def _f(v: Any) -> Optional[float]:
    """Parse a dollar field, which Kalshi sends as a string ('0.5200')."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ws_messages(ws: Any):
    """Yield websocket frames from an aiohttp response.

    `ws.messages` does not exist in aiohttp 3.9 - it was added later - so the
    obvious spelling raises AttributeError inside the connection handler and the
    socket silently falls back to the REST poller. Iterating `ws` directly is the
    supported form and works on every version.
    """
    return ws


def _parse_bucket(event_ticker: str, series_prefix: str = None) -> Optional[str]:
    """Return the `26OCT011715` quarter-hour stamp from a ticker or bare stamp.

    Accepts either form because the bucket appears in two places: on the event
    ticker (`KXBTC15M-26OCT011715`) and on the market ticker's middle segment
    (`KXBTC15M-26OCT011715-15`).

    YY(2) MON(3) DD(2) HH(2) MM(2) - the month is alphabetic, so the numeric
    fields cannot simply be sliced off the front as one digit run.
    """
    raw = (event_ticker or "").strip()
    if "-" in raw:
        parts = raw.split("-")
        prefix = parts[0]
        if series_prefix and not prefix.startswith(series_prefix):
            return None
        if len(parts) != 2:
            return None
        stamp = parts[1]
    else:
        stamp = raw
    digits = (slice(0, 2), slice(5, 7), slice(7, 9), slice(9, 11))
    if len(stamp) != 11 or not stamp[2:5].isalpha():
        return None
    if not all(stamp[s].isdigit() for s in digits):
        return None
    return stamp


def _iso(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class SpotTick:
    price: float
    ts: float

    @property
    def age(self) -> float:
        return max(0.0, _now() - self.ts)

    @property
    def fresh(self) -> bool:
        return self.age <= SPOT_MAX_AGE_SEC


@dataclass
class UpDownMarket:
    """One 15-minute up/down contract."""

    ticker: str
    event_ticker: str
    bucket: Optional[str]
    horizon: int
    title: str
    target: Optional[float]
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    no_bid: Optional[float] = None
    no_ask: Optional[float] = None
    last: Optional[float] = None
    volume: Optional[float] = None
    close_ts: Optional[float] = None
    open_ts: Optional[float] = None

    @property
    def seconds_left(self) -> Optional[float]:
        if self.close_ts is None:
            return None
        return max(0.0, self.close_ts - _now())

    @property
    def tradable(self) -> bool:
        return self.yes_bid is not None and self.yes_ask is not None

    @property
    def up_price(self) -> Optional[float]:
        """Price of one contract on UP (which is the YES side)."""
        return self.yes_ask

    @property
    def down_price(self) -> Optional[float]:
        """Price of one contract on DOWN (the NO side)."""
        return self.no_ask

    def side_ask(self, side: str) -> Optional[float]:
        return self.yes_ask if side == "up" else self.no_ask

    def side_bid(self, side: str) -> Optional[float]:
        return self.yes_bid if side == "up" else self.no_bid


class SpotFeed:
    """Coinbase spot ticker over websocket, with a REST fallback.

    The websocket is the point: it delivers in tens of milliseconds, so the spot
    side of the comparison is not itself the bottleneck. If the socket cannot be
    established the REST ticker keeps the charts alive at ~0.5 Hz and the feed
    reports itself as degraded, because a slow spot price must never be silently
    used as a leading indicator.

    `product` is the Coinbase exchange pair, e.g. BTC-USD, DOGE-USD.
    """

    def __init__(self, product: str = "BTC-USD") -> None:
        self.product = product
        self.price: float = 0.0
        self.ts: float = 0.0
        self.connected = False
        self.source = "none"
        self.last_error = ""
        self.history: Deque[Tuple[float, float]] = deque(maxlen=SERIES_POINTS)
        self._stop: Optional[asyncio.Event] = None

    @property
    def age(self) -> float:
        return max(0.0, _now() - self.ts) if self.ts else float("inf")

    @property
    def fresh(self) -> bool:
        return self.price > 0 and self.age <= SPOT_MAX_AGE_SEC

    def _record(self, price: float, source: str) -> None:
        self.price = price
        self.ts = _now()
        self.source = source
        self.history.append((self.ts, price))

    async def start(self) -> None:
        self._stop = asyncio.Event()
        asyncio.get_event_loop().create_task(self._ws_loop())  # noqa: RUF006
        asyncio.get_event_loop().create_task(self._rest_loop())  # noqa: RUF006

    async def stop(self) -> None:
        if self._stop:
            self._stop.set()

    async def _ws_loop(self) -> None:
        while not (self._stop and self._stop.is_set()):
            try:
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(COINBASE_WS) as ws:
                        await ws.send_str(
                            json.dumps(
                                {
                                    "type": "subscribe",
                                    "product_ids": [self.product],
                                    "channels": ["ticker"],
                                }
                            )
                        )
                        self.connected = True
                        self.last_error = ""
                        async for msg in _ws_messages(ws):
                            if self._stop and self._stop.is_set():
                                return
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            try:
                                data = json.loads(msg.data)
                            except json.JSONDecodeError:
                                continue
                            if data.get("type") == "ticker" and _f(data.get("price")):
                                self._record(float(data["price"]), "coinbase-ws")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(3)

    async def _rest_loop(self) -> None:
        url = f"https://api.exchange.coinbase.com/products/{self.product}/ticker"
        while not (self._stop and self._stop.is_set()):
            try:
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as r:
                        r.raise_for_status()
                        data = await r.json()
                price = _f(data.get("price"))
                if price:
                    # Never let a slower poll overwrite a fresher socket tick.
                    if not self.fresh:
                        self._record(price, "coinbase-rest")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(2)


class Btc15mFeed:
    """Kalshi up/down markets for any series, read over REST.

    One request returns every open horizon in the series, so a poll is a single
    call regardless of how many 15-minute buckets are live.
    """

    def __init__(self, series: str = BTC15M_SERIES):
        self.series = series
        self._prefix = series.split('-')[0] if series else "KXBTC15M"
        self.markets: List[UpDownMarket] = []
        self.ts: float = 0.0
        self.refreshes = 0
        self.last_error = ""

    @staticmethod
    def _parse(ticker: str, prefix: str = None) -> Tuple[Optional[str], Optional[str], int]:
        """`KXBTC15M-26OCT011715-15` -> (event_ticker, bucket, horizon_minutes)."""
        parts = (ticker or "").split("-")
        if len(parts) != 3 or not parts[0].startswith(prefix or "KX"):
            return None, None, 0
        try:
            horizon = int(parts[2])
        except ValueError:
            return None, None, 0
        event_ticker = f"{parts[0]}-{parts[1]}"
        return event_ticker, _parse_bucket(parts[1], parts[0]), horizon

    async def fetch(self, session: Optional[aiohttp.ClientSession] = None) -> List[UpDownMarket]:
        own = session is None
        session = session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "kalshi-frigo/1.0"},
        )
        try:
            async with session.get(
                f"{KALSHI_API}/markets",
                params={"limit": 500, "status": "open", "series_ticker": self.series},
            ) as r:
                r.raise_for_status()
                payload = await r.json()
        finally:
            if own:
                await session.close()

        markets: List[UpDownMarket] = []
        for m in payload.get("markets", []):
            ticker = m.get("ticker", "")
            event_ticker, bucket, horizon = self._parse(ticker, self._prefix)
            if event_ticker is None:
                continue
            markets.append(
                UpDownMarket(
                    ticker=ticker,
                    event_ticker=event_ticker,
                    bucket=bucket,
                    horizon=horizon,
                    title=m.get("title", ""),
                    target=_f(m.get("floor_strike")),
                    # The *_dollars fields are the only quotes this series sets.
                    yes_bid=_f(m.get("yes_bid_dollars")),
                    yes_ask=_f(m.get("yes_ask_dollars")),
                    no_bid=_f(m.get("no_bid_dollars")),
                    no_ask=_f(m.get("no_ask_dollars")),
                    last=_f(m.get("last_price_dollars")),
                    volume=_f(m.get("volume_fp")),
                    close_ts=_iso(m.get("close_time")),
                    open_ts=_iso(m.get("open_time")),
                )
            )
        # Soonest expiry first; that is the contract about to settle.
        markets.sort(key=lambda x: (x.close_ts or float("inf"), x.horizon))
        self.markets = markets
        self.ts = _now()
        self.refreshes += 1
        return markets

    def nearest(self) -> Optional[UpDownMarket]:
        """The next contract to expire - the one a strategy trades."""
        tradable = [m for m in self.markets if m.tradable]
        if not tradable:
            return None
        return tradable[0]

    def for_bucket(self, bucket: str) -> List[UpDownMarket]:
        return [m for m in self.markets if m.bucket == bucket]


@dataclass
class LeadReading:
    """A measured spot-vs-Kalshi comparison, not an assumed one."""

    spot: float = 0.0
    spot_age: float = 0.0
    ticker: str = ""
    target: Optional[float] = None
    up_price: Optional[float] = None
    down_price: Optional[float] = None
    seconds_left: Optional[float] = None
    spot_vs_target: Optional[float] = None
    ts: float = 0.0

    @property
    def usable(self) -> bool:
        """True only when the spot tick is fresh and the contract is quotable."""
        return (
            self.spot > 0
            and self.up_price is not None
            and self.spot_age <= SPOT_MAX_AGE_SEC
            and (self.seconds_left or 0) > 0
        )


class LeadEstimator:
    """Tracks the live spot feed against Kalshi's own up/down quote.

    Two distinct quantities, deliberately not conflated:

    * `spot_vs_target` - how far live spot sits from the contract's target. This
      is the settlement question, and it is arithmetic, not an edge.
    * the Kalshi quote itself - what the market charges for each side.

    When spot has already moved decisively past the target but Kalshi still
    prices the same side near even money, the quote is the stale number. That
    comparison is what the strategy trades; nothing about a fixed 45-second lead
    is assumed or encoded anywhere.
    """

    def __init__(self) -> None:
        self.history: Deque[Dict[str, Any]] = deque(maxlen=SERIES_POINTS)
        self.last: LeadReading = LeadReading()

    def observe(self, spot: SpotFeed, feed: Btc15mFeed) -> LeadReading:
        market = feed.nearest()
        reading = LeadReading(spot=spot.price, spot_age=spot.age, ts=_now())
        if market is not None:
            reading.ticker = market.ticker
            reading.target = market.target
            reading.up_price = market.up_price
            reading.down_price = market.down_price
            reading.seconds_left = (
                round(market.seconds_left, 1) if market.seconds_left is not None else None
            )
            if market.target:
                reading.spot_vs_target = round(spot.price - market.target, 2)
        self.last = reading
        self.history.append(
            {
                "ts": reading.ts,
                "spot": reading.spot,
                "target": reading.target,
                "up": reading.up_price,
                "down": reading.down_price,
                "ticker": reading.ticker,
            }
        )
        return reading


class MarketDataHub:
    """Everything the dashboard's market row needs, on one background task."""

    def __init__(self, spot_product: str = "BTC-USD", series: str = BTC15M_SERIES) -> None:
        self.spot = SpotFeed(product=spot_product)
        self.feed = Btc15mFeed(series=series)
        self.lead = LeadEstimator()
        self._task: Optional[asyncio.Task[Any]] = None
        self._stop: Optional[asyncio.Event] = None

    async def start(self) -> None:
        await self.spot.start()
        self._stop = asyncio.Event()
        self._task = asyncio.get_event_loop().create_task(self._loop())  # noqa: RUF006

    async def stop(self) -> None:
        await self.spot.stop()
        if self._stop:
            self._stop.set()

    async def _loop(self) -> None:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": "kalshi-frigo/1.0"}
        ) as session:
            while not (self._stop and self._stop.is_set()):
                try:
                    await self.feed.fetch(session)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.feed.last_error = f"{type(exc).__name__}: {exc}"
                self.lead.observe(self.spot, self.feed)
                await asyncio.sleep(2)

    def chart_payload(self) -> Dict[str, Any]:
        """Both charts, the contract, and the measured comparison."""
        spot_points = [{"t": int(ts), "p": round(px, 2)} for ts, px in self.spot.history]
        reading = self.last_reading()
        market = self.feed.nearest()

        contract: Dict[str, Any] = {
            "series": self.feed.series,
            "refreshes": self.feed.refreshes,
            "error": self.feed.last_error,
            "open_markets": len(self.feed.markets),
            "tradable": sum(1 for m in self.feed.markets if m.tradable),
            "buckets": sorted({m.bucket for m in self.feed.markets if m.bucket}),
        }
        if market is not None:
            contract["market"] = {
                "ticker": market.ticker,
                "event": market.event_ticker,
                "bucket": market.bucket,
                "horizon": market.horizon,
                "title": market.title,
                "target": market.target,
                "up_bid": market.yes_bid,
                "up_ask": market.yes_ask,
                "down_bid": market.no_bid,
                "down_ask": market.no_ask,
                "last": market.last,
                "volume": market.volume,
                "seconds_left": (
                    round(market.seconds_left, 1) if market.seconds_left is not None else None
                ),
                "close_time": (
                    datetime.fromtimestamp(market.close_ts, timezone.utc).isoformat(
                        timespec="seconds"
                    )
                    if market.close_ts
                    else None
                ),
            }

        return {
            "spot": {
                "source": self.spot.source,
                "connected": self.spot.connected,
                "error": self.spot.last_error,
                "price": round(self.spot.price, 2) if self.spot.price else None,
                "age_sec": round(self.spot.age, 2) if self.spot.ts else None,
                "fresh": self.spot.fresh,
                "points": spot_points,
            },
            "kalshi": contract,
            "lead": {
                "spot": reading.spot,
                "target": reading.target,
                "spot_vs_target": reading.spot_vs_target,
                "up_price": reading.up_price,
                "down_price": reading.down_price,
                "seconds_left": reading.seconds_left,
                "usable": reading.usable,
                "note": (
                    "Up/Down prices are Kalshi's own quotes for the next 15-minute "
                    "contract, polled live. Settlement is the 60-second average of "
                    "CF Benchmarks BRTI, so spot is a proxy for it - the comparison "
                    "is measured, not assumed."
                ),
            },
            "series": [
                {
                    "t": int(h["ts"]),
                    "spot": round(h["spot"], 2) if h["spot"] else None,
                    "target": h["target"],
                    "up": h["up"],
                    "down": h["down"],
                }
                for h in self.lead.history
            ],
        }

    def last_reading(self) -> LeadReading:
        return self.lead.last
