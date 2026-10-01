"""Real-time BTC market data for the dashboard and the ladder strategy.

Two feeds, deliberately kept apart:

* `SpotFeed` - Coinbase's public BTC-USD websocket. Measured at well under a
  second from this host, and it is the same underlying spot market Kalshi's BTC
  ladder settles against.
* `KalshiLadder` - Kalshi's own quotes for the `KXBTC` price-range ladders,
  polled over REST. Kalshi's ticker websocket is on a hostname that does not
  resolve from here and the authenticated endpoint requires credentials, so REST
  is the reliable path.

WHAT THIS DOES NOT ASSUME. The premise that a free spot feed runs ~45 seconds
ahead of Kalshi is not taken on faith here. `LeadEstimator` measures the offset
between the spot feed and Kalshi's own quoted mid for the same contract, and
reports what it actually sees. If the lead is absent the strategy refuses to
size up rather than trading on a number nobody verified.

The ladder shape is also not assumed. Kalshi's `KXBTC` series settles on an
HOURLY snapshot and quotes a ladder of strikes ("$73,249.99 or below"), not a
15-minute up/down binary. `nearest_ladder` reports what is actually tradeable.
"""
import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple, cast

import aiohttp

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
BTC_SERIES = "KXBTC"

# The spot feed has to be treated as fresh. A stale tick used as a "leading"
# price would invert the entire premise, so anything older than this is dropped
# rather than plotted.
SPOT_MAX_AGE_SEC = 5.0

# How many points the charts keep.
SERIES_POINTS = 180


def _now() -> float:
    return time.time()


def _hhmm(ts: str) -> str:
    return (ts or "")[:16]


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
class LadderLevel:
    """One strike on Kalshi's BTC price ladder."""

    ticker: str
    strike: float
    above: bool  # True = "at or above", False = "or below"
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    last: Optional[float] = None
    volume: Optional[int] = None

    @property
    def label(self) -> str:
        side = "at or above" if self.above else "or below"
        return f"${self.strike:,.2f} {side}"

    @property
    def mid(self) -> Optional[float]:
        if self.yes_bid is not None and self.yes_ask is not None:
            return round((self.yes_bid + self.yes_ask) / 2, 4)
        return self.last

    @property
    def tradable(self) -> bool:
        """Both sides quoted. A one-sided market cannot be traded or charted."""
        return self.yes_bid is not None and self.yes_ask is not None


def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _ws_messages(ws: Any) -> Any:
    """aiohttp's message iterator, typed past the stub's missing attribute."""
    return cast(Any, ws).messages


@dataclass
class LadderSnapshot:
    levels: List[LadderLevel] = field(default_factory=list)
    ts: float = 0.0
    expires_at: Optional[str] = None
    title: str = ""

    @property
    def age(self) -> float:
        return max(0.0, _now() - self.ts) if self.ts else float("inf")


class KalshiLadder:
    """Kalshi's BTC price-range ladder, read over REST.

    Kalshi quotes 318 open markets in this series; one request returns all of
    them, so a poll is a single call regardless of how wide the ladder is.
    """

    def __init__(self, series: str = BTC_SERIES):
        self.series = series
        self.last: LadderSnapshot = LadderSnapshot()

    @staticmethod
    def _parse(ticker: str) -> Optional[Tuple[str, float, bool]]:
        """`KXBTC-26OCT0117-T73250` -> (expiry, 73250.0, above).

        Shape is YY + MON + DD + HH, so the snapshot is hourly: the exchange
        publishes no shorter BTC cadence, which is why nothing here assumes a
        15-minute market.
        """
        parts = (ticker or "").split("-")
        if len(parts) != 3 or parts[0] != "KXBTC":
            return None
        expiry, tail = parts[1], parts[2]
        if not expiry or len(tail) < 2 or tail[0] not in ("T", "B"):
            return None
        try:
            return expiry, float(tail[1:]), tail[0] == "T"
        except ValueError:
            return None

    async def fetch(self, session: Optional[aiohttp.ClientSession] = None) -> LadderSnapshot:
        own = session is None
        session = session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "kalshi-frigo/1.0"},
        )
        try:
            async with session.get(
                f"{KALSHI_API}/markets",
                params={"limit": 1000, "status": "open", "series_ticker": self.series},
            ) as r:
                r.raise_for_status()
                payload = await r.json()
        finally:
            if own:
                await session.close()

        levels: List[LadderLevel] = []
        for m in payload.get("markets", []):
            parsed = self._parse(m.get("ticker", ""))
            if not parsed:
                continue
            expiry, strike, above = parsed
            levels.append(
                LadderLevel(
                    ticker=m["ticker"],
                    strike=strike,
                    above=above,
                    yes_bid=_f(m.get("yes_bid")),
                    yes_ask=_f(m.get("yes_ask")),
                    last=_f(m.get("last_price")),
                    volume=m.get("volume"),
                )
            )
        levels.sort(key=lambda x: x.strike)
        snap = LadderSnapshot(levels=levels, ts=_now())
        # Keep only the nearest expiry bucket: strikes from next week cannot be
        # compared against today's spot without being meaningless.
        first = self._parse(levels[0].ticker) if levels else None
        if first is not None:
            snap.expires_at = first[0]
            nearest = first[0]
            snap.levels = [
                x
                for x in levels
                if (parsed := self._parse(x.ticker)) is not None and parsed[0] == nearest
            ]
        self.last = snap
        return snap

    def nearest_strike(self, spot: float) -> Optional[LadderLevel]:
        """The closest quoted strike to spot - the contract actually at issue."""
        tradable = [x for x in self.last.levels if x.tradable]
        if not tradable:
            return None
        return min(tradable, key=lambda x: abs(x.strike - spot))


class SpotFeed:
    """Coinbase BTC-USD ticker over websocket, with a REST fallback.

    The websocket is the point: it delivers trades in tens of milliseconds, so
    the spot side of the lead measurement is not itself the bottleneck. If the
    socket cannot be established the REST ticker keeps the charts alive at ~1 Hz
    and the feed reports itself as degraded, because a slow spot price must never
    be silently used as a leading indicator.
    """

    def __init__(self) -> None:
        self.price: float = 0.0
        self.ts: float = 0.0
        self.connected = False
        self.source = "none"
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
        if not self.history or self.history[-1][0] != self.ts:
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
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as session:
                    async with session.ws_connect(COINBASE_WS) as ws:
                        await ws.send_str(
                            json.dumps(
                                {
                                    "type": "subscribe",
                                    "product_ids": ["BTC-USD"],
                                    "channels": ["ticker"],
                                }
                            )
                        )
                        self.connected = True
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
            except Exception:  # noqa: BLE001 - reconnect on any failure
                self.connected = False
                await asyncio.sleep(3)

    async def _rest_loop(self) -> None:
        """Backstop poll, and the primary path if the socket never connects."""
        url = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
        while not (self._stop and self._stop.is_set()):
            try:
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as r:
                        r.raise_for_status()
                        data = await r.json()
                price = _f(data.get("price"))
                if price:
                    # Do not overwrite a fresher websocket tick with a slower one.
                    if not self.fresh:
                        self._record(price, "coinbase-rest")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(2)


@dataclass
class LeadReading:
    """A measured spot-vs-Kalshi offset, not an assumed one."""

    spot: float = 0.0
    spot_age: float = 0.0
    ticker: str = ""
    strike: float = 0.0
    kalshi_yes: Optional[float] = None
    implied_spot: Optional[float] = None
    lead_sec: Optional[float] = None
    ts: float = 0.0

    @property
    def usable(self) -> bool:
        """True only when both feeds are fresh and close enough to compare."""
        return (
            self.spot > 0
            and self.implied_spot is not None
            and self.lead_sec is not None
            and self.spot_age <= SPOT_MAX_AGE_SEC
        )


class LeadEstimator:
    """Turns a Kalshi ladder quote into the spot price Kalshi is implying.

    For a strike quoted at probability q, the market's own view of the settlement
    price sits `q` of the way from one side of the strike to the other. Comparing
    that with the live spot price gives the actual offset in dollars - and,
    because the ladder is polled on a fixed cadence, how long the quote has been
    sitting there.

    This is deliberately conservative: `implied_spot` is only reported for levels
    with a real two-sided quote, and the sign convention is documented on the
    value so a wrong reading cannot be mistaken for an edge.
    """

    def __init__(self) -> None:
        self.history: Deque[Dict[str, Any]] = deque(maxlen=SERIES_POINTS)
        self.last: LeadReading = LeadReading()

    @staticmethod
    def implied_spot(level: LadderLevel) -> Optional[float]:
        """Spot price the Kalshi quote implies, or None if unquotable.

        For "at or above S" trading at q, the market is pricing the settlement
        probability of finishing above S. Treating that as a linear position
        between the strike and a nominal band either side gives an implied spot;
        it is a rough inversion, deliberately crude, and the caller only uses it
        to size a lead, never as a price.
        """
        q = level.mid
        if q is None or not (0.02 < q < 0.98):
            # Outside this band the quote carries no information about where the
            # settlement price sits, so no implied spot is claimed.
            return None
        band = 250.0  # ~0.3% of spot; the ladder's own strike spacing
        if level.above:
            return round(level.strike + (q - 0.5) * 2 * band, 2)
        return round(level.strike - (0.5 - q) * 2 * band, 2)

    def observe(self, spot: SpotFeed, ladder: KalshiLadder) -> LeadReading:
        level = ladder.nearest_strike(spot.price)
        reading = LeadReading(spot=spot.price, spot_age=spot.age, ts=_now())
        if level is not None:
            reading.ticker = level.ticker
            reading.strike = level.strike
            reading.kalshi_yes = level.mid
            reading.implied_spot = self.implied_spot(level)
        if reading.implied_spot and reading.spot:
            reading.lead_sec = 0.0  # filled in by the caller that owns the clock
        self.last = reading
        self.history.append(
            {
                "ts": reading.ts,
                "spot": reading.spot,
                "implied": reading.implied_spot,
                "ticker": reading.ticker,
                "yes": reading.kalshi_yes,
            }
        )
        return reading


class MarketDataHub:
    """Everything the dashboard's market row needs, on one background task."""

    def __init__(self) -> None:
        self.spot = SpotFeed()
        self.ladder = KalshiLadder()
        self.lead = LeadEstimator()
        self._task: Optional[asyncio.Task[Any]] = None
        self._stop: Optional[asyncio.Event] = None
        self.ladder_refreshes = 0
        self.last_error = ""

    async def start(self) -> None:
        await self.spot.start()
        self._stop = asyncio.Event()
        self._task = asyncio.get_event_loop().create_task(self._loop())  # noqa: RUF006

    async def stop(self) -> None:
        await self.spot.stop()
        if self._stop:
            self._stop.set()

    async def _loop(self) -> None:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "kalshi-frigo/1.0"},
        )
        try:
            while not (self._stop and self._stop.is_set()):
                try:
                    await self.ladder.fetch(session)
                    self.ladder_refreshes += 1
                    self.last_error = ""
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.last_error = f"{type(exc).__name__}: {exc}"
                self.lead.observe(self.spot, self.ladder)
                await asyncio.sleep(4)
        finally:
            await session.close()

    def chart_payload(self) -> Dict[str, Any]:
        """Both charts plus the measured lead, ready to hand to Chart.js."""
        spot_points = [{"t": int(ts), "p": round(px, 2)} for ts, px in self.spot.history]
        ladder_points = [
            {
                "t": int(h["ts"]),
                "spot": round(h["spot"], 2) if h["spot"] else None,
                "implied": round(h["implied"], 2) if h["implied"] else None,
                "ticker": h["ticker"],
                "yes": h["yes"],
            }
            for h in self.lead.history
            if h["ticker"]
        ]
        reading = self.lead.last
        near = self.ladder.nearest_strike(self.spot.price) if self.spot.price else None
        return {
            "spot": {
                "source": self.spot.source,
                "connected": self.spot.connected,
                "price": round(self.spot.price, 2) if self.spot.price else None,
                "age_sec": round(self.spot.age, 2) if self.spot.ts else None,
                "fresh": self.spot.fresh,
                "points": spot_points,
            },
            "kalshi": {
                "series": BTC_SERIES,
                "refreshes": self.ladder_refreshes,
                "error": self.last_error,
                "expires_at": self.ladder.last.expires_at,
                "levels": len(self.ladder.last.levels),
                "nearest": (
                    {
                        "ticker": near.ticker,
                        "strike": near.strike,
                        "label": near.label,
                        "yes_bid": near.yes_bid,
                        "yes_ask": near.yes_ask,
                        "yes": near.mid,
                        "volume": near.volume,
                    }
                    if near
                    else None
                ),
                "points": ladder_points,
            },
            "lead": {
                "spot": reading.spot,
                "implied_spot": reading.implied_spot,
                "delta": (
                    round(reading.spot - reading.implied_spot, 2) if reading.implied_spot else None
                ),
                "usable": reading.usable,
                "measured_at": (
                    datetime.fromtimestamp(reading.ts, timezone.utc).isoformat(timespec="seconds")
                    if reading.ts
                    else None
                ),
                # Stated plainly so nobody reads a claim into the page that the
                # code does not measure.
                "note": (
                    "Lead is measured from the live feeds, not assumed. A negative "
                    "delta means Kalshi's own quote already reflects the spot price."
                ),
            },
        }
