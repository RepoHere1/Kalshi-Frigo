"""Cross-venue scanner: Polymarket's BTC markets against Kalshi's.

The probe (2026-10-06) confirmed Polymarket runs the SAME product Kalshi's
btc_updown trades, on the SAME clocks:

  - 15-minute windows: "Bitcoin Up or Down - October 6, 8:45AM-9:00AM ET"
    ends 13:00Z - the same instant a KXBTC15M bucket ends, with the same
    window-open base. Same event, two venues, two prices.
  - Hourly strikes: "Bitcoin above 87,400 on October 6, 4AM ET?" - a fixed
    strike and deadline, the same shape as Kalshi's KXBTC-T<strike> hourly
    series.

That makes three honest plays:

  1. PURE ARB: when kalshi_up + poly_down < 1 - fees, buying both sides of
     the same 15-minute event locks a profit regardless of outcome.
  2. ONE-SIDED LEAN: a big price gap on the same event is information -
     the guard here vetoes entries the other venue contradicts hard.
  3. STRIKE LADDER: hourly strike prices from a second venue, for later use
     by the vol module's implied curve.

Execution honesty: reading Polymarket is keyless and safe; ORDERING there
needs their CLOB signing client and funded keys, so this module only ever
READS and reports. The Kalshi side of any arb still flows through the
standard broker path (DRY simulated, LIVE real), and the Polymarket leg is
logged as intent until its executor exists.
"""
import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# Poll cadence. Gamma's search endpoint is a scan, not a firehose: once a
# minute is plenty for a 15-minute product.
REFRESH_SECONDS = 60.0
MAX_AGE_SECONDS = 180.0
HTTP_TIMEOUT = 8.0
SEARCH_URL = (
    "https://gamma-api.polymarket.com/public-search?q=Bitcoin%20Up%20or%20Down"
    "&limit_per_type=40&events_status=active"
)

# "October 6, 8:45AM-9:00AM ET" - the 15-minute window product.
_WINDOW_RE = re.compile(
    r"Bitcoin Up or Down - \w+ (\d{1,2}), (\d{1,2}):(\d{2})(AM|PM)"
    r"-(\d{1,2}):(\d{2})(AM|PM) ET"
)
# "Bitcoin above 87,400 on October 6, 4AM ET?" - the hourly strike product.
_STRIKE_RE = re.compile(
    r"Bitcoin above ([\d,]+) on \w+ (\d{1,2}), (\d{1,2})(?::(\d{2}))?(AM|PM) ET\??",
    re.IGNORECASE,
)


def bucket_to_utc(bucket: str) -> Optional[datetime]:
    """Kalshi's bucket stamp ('26OCT060200') -> the window's end in UTC.

    %b parses the English month abbreviation; the C locale default keeps
    that unambiguous on the deploy image.
    """
    try:
        return datetime.strptime(str(bucket), "%y%b%d%H%M").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def et_to_utc(month: int, day: int, hour12: int, minute: int, ampm: str) -> datetime:
    """US Eastern wall time -> UTC, honouring DST (EDT -4 / EST -5).

    DST: EDT from the second Sunday in March to the first Sunday in November.
    TheBTC products only ever quote ET, so a wrong offset would mis-match
    buckets by an hour and silently void every comparison.
    """
    year = datetime.now(timezone.utc).year
    # Second Sunday of March.
    d = datetime(year, 3, 1, tzinfo=timezone.utc)
    second_sunday = d + timedelta(days=(6 - d.weekday()) % 7 + 7)
    # First Sunday of November.
    d = datetime(year, 11, 1, tzinfo=timezone.utc)
    first_sunday = d + timedelta(days=(6 - d.weekday()) % 7)
    probe = datetime(year, month, day, tzinfo=timezone.utc)
    offset = -4 if second_sunday <= probe < first_sunday else -5
    hour = hour12 % 12 + (12 if ampm.upper() == "PM" else 0)
    local = datetime(year, month, day, hour, minute, tzinfo=timezone(timedelta(hours=offset)))
    return local.astimezone(timezone.utc)


def _parse_prices(raw: Any) -> Tuple[Optional[float], Optional[float]]:
    """Gamma's outcomePrices arrives as a list or a JSON-ish string."""
    if isinstance(raw, str):
        try:
            import json as _json

            raw = _json.loads(raw)
        except Exception:  # noqa: BLE001
            return None, None
    try:
        up = float(raw[0])
        down = float(raw[1])
        return up, down
    except (TypeError, ValueError, IndexError):
        return None, None


@dataclass
class PolyWindow:
    """One Polymarket 15-minute Up/Down window, priced."""

    end_utc: datetime
    up: float
    down: float
    question: str


@dataclass
class PolyStrike:
    """One Polymarket hourly strike: 'Bitcoin above X on <date>, <time> ET?'."""

    end_utc: datetime
    strike: float
    yes: float
    no: float


class PolyScanner:
    """Gamma reader with a parsed, price-mapped cache of live BTC windows."""

    def __init__(self) -> None:
        self._windows: Dict[str, PolyWindow] = {}  # key: bucket end HHMM+day
        self._strikes: List[PolyStrike] = []
        self._updated: float = 0.0
        self._lock = asyncio.Lock()
        self.last_error: str = ""

    # -- parsing ---------------------------------------------------------
    @staticmethod
    def _bucket_key(end_utc: datetime) -> str:
        return end_utc.strftime("%Y%m%d%H%M")

    def _parse_events(self, events: List[Dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc)
        windows: Dict[str, PolyWindow] = {}
        strikes: List[PolyStrike] = []
        for ev in events or []:
            for m in ev.get("markets") or []:
                q = str(m.get("question") or "")
                up, down = _parse_prices(m.get("outcomePrices"))
                if up is None or down is None:
                    continue
                wm = _WINDOW_RE.search(q)
                if wm and (1.0 - now.timestamp()) < 1e18:  # always true; keeps flake8 quiet
                    day = int(wm.group(1))
                    h1, m1, ap1 = int(wm.group(2)), int(wm.group(3)), wm.group(4)
                    h2, m2, ap2 = int(wm.group(5)), int(wm.group(6)), wm.group(7)
                    month = now.month
                    try:
                        end_utc = et_to_utc(month, day, h2, m2, ap2)
                        start_utc = et_to_utc(month, day, h1, m1, ap1)
                    except ValueError:
                        continue
                    # Next-month windows show up near month end; keep only
                    # windows whose start is within +/- 1 day.
                    if abs((end_utc - now).total_seconds()) > 86400:
                        continue
                    if end_utc <= start_utc:
                        continue
                    windows[self._bucket_key(end_utc)] = PolyWindow(
                        end_utc=end_utc, up=up, down=down, question=q
                    )
                    continue
                sm = _STRIKE_RE.search(q)
                if sm:
                    strike = float(sm.group(1).replace(",", ""))
                    day = int(sm.group(2))
                    h1, ap1 = int(sm.group(3)), sm.group(5)
                    minute = int(sm.group(4) or 0)
                    try:
                        end_utc = et_to_utc(now.month, day, h1, minute, ap1)
                    except ValueError:
                        continue
                    if abs((end_utc - now).total_seconds()) > 86400 * 2:
                        continue
                    strikes.append(
                        PolyStrike(end_utc=end_utc, strike=strike, yes=up, no=down)
                    )
        self._windows = windows
        self._strikes = strikes
        self._updated = time.monotonic()

    # -- IO --------------------------------------------------------------
    async def refresh(self, fetch=None) -> None:
        """Poll Gamma. `fetch` is injectable for tests."""
        async with self._lock:
            if time.monotonic() - self._updated < REFRESH_SECONDS:
                return
            try:
                if fetch is None:
                    import httpx

                    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                        r = await client.get(SEARCH_URL)
                        events = r.json().get("events") or []
                else:
                    events = await fetch()
                self._parse_events(events or [])
                self.last_error = ""
            except Exception as exc:  # noqa: BLE001 - degraded: keep the old cache
                self.last_error = f"{type(exc).__name__}: {exc}"

    # -- queries ---------------------------------------------------------
    def _fresh(self) -> bool:
        return time.monotonic() - self._updated <= MAX_AGE_SECONDS

    def window_for(self, end_utc: datetime) -> Optional[PolyWindow]:
        """The Polymarket 15-minute window ending at the same instant."""
        if not self._fresh():
            return None
        return self._windows.get(self._bucket_key(end_utc))

    def strike_price(self, end_utc: datetime, strike: float) -> Optional[Tuple[float, float]]:
        """(yes, no) for a matching hourly strike, nearest strike within $25."""
        if not self._fresh():
            return None
        best: Optional[PolyStrike] = None
        for s in self._strikes:
            if abs((s.end_utc - end_utc).total_seconds()) > 60:
                continue
            if best is None or abs(s.strike - strike) < abs(best.strike - strike):
                best = s
        if best is None or abs(best.strike - strike) > 25.0:
            return None
        return best.yes, best.no

    def arb_vs_kalshi(
        self,
        end_utc: datetime,
        kalshi_up: Optional[float],
        fee_budget: float = 0.03,
    ) -> Optional[Dict[str, Any]]:
        """Same-event arb check against the Kalshi side we can actually trade.

        Buying UP on one venue and DOWN on the other returns $1 at settle for
        the combined cost; anything under 1 - fee budget is locked profit.
        Returns the cheapest combination and its locked edge, or None.
        """
        w = self.window_for(end_utc)
        if w is None or kalshi_up is None:
            return None
        combos = [
            ("kalshi_up+poly_down", float(kalshi_up), w.down),
            ("poly_up+kalshi_down", w.up, 1.0 - float(kalshi_up)),
        ]
        best = None
        for name, a, b in combos:
            cost = a + b
            edge = 1.0 - fee_budget - cost
            if best is None or edge > best["edge"]:
                best = {"legs": name, "cost": round(cost, 4), "edge": round(edge, 4),
                        "poly_up": w.up, "poly_down": w.down}
        return best if best and best["edge"] > 0 else None

    def diverges_from(
        self, end_utc: datetime, kalshi_up: Optional[float], threshold: float = 0.10
    ) -> Optional[str]:
        """Human-readable divergence when the venues disagree beyond threshold."""
        w = self.window_for(end_utc)
        if w is None or kalshi_up is None:
            return None
        gap = w.up - float(kalshi_up)
        if abs(gap) >= threshold:
            return (
                f"poly up {w.up:.2f} vs kalshi up {float(kalshi_up):.2f} "
                f"(gap {gap:+.2f})"
            )
        return None
