"""BRTI truth feed: Kalshi's own CF Benchmarks index over its own WebSocket.

Why this exists: KXBTC15M settles on the 60-second average of the CF
Benchmarks Bitcoin Real-Time Index. A retail spot ticker (Coinbase, or a
once-a-minute newhedge REST snapshot) is a proxy for that average; the
cfbenchmarks_value channel IS the index family, ticked ~1/sec with source
timestamps, a trailing 60-second average, and -- in the final minute before
a quarter-hour close -- the accumulating settlement window average itself.

Priority in the trader: final-minute windowed average > trailing avg60 >
live BRTI value > Coinbase spot. Every level degrades loudly (last_error,
stale age) so a dead feed reads as dead, never as an edge.

newhedge was evaluated and rejected as primary: REST-only, 60s updates,
paid token. It is slower than the Coinbase socket we already have.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

BRTI_INDEX_ID = "BRTI"
# A BRTI tick older than this is not used for decisions. Ticks arrive ~1/sec;
# anything older was produced by a dead socket masquerading as truth.
BRTI_MAX_AGE_SEC = 5.0
BRTI_POINTS = 180


def _now() -> float:
    return time.time()


def _num(value: Any) -> Optional[float]:
    """First float found in a scalar-or-dict shape, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
        return out if out > 0 else None
    if isinstance(value, str):
        try:
            out = float(value)
        except ValueError:
            return None
        return out if out > 0 else None
    if isinstance(value, dict):
        for key in ("average", "value", "price", "index_value", "last"):
            found = _num(value.get(key))
            if found is not None:
                return found
    return None


def parse_brti_message(msg: object) -> Dict[str, Optional[float]]:
    """Extract value/averages/timestamps from a cfbenchmarks_value frame.

    The exact frame shape is walked defensively: scalars or nested dicts,
    several key spellings. Returns {"value", "avg60", "win_avg",
    "source_ts"} with None for anything absent. Never raises.
    """
    out: Dict[str, Optional[float]] = {
        "value": None,
        "avg60": None,
        "win_avg": None,
        "source_ts": None,
    }
    if not isinstance(msg, dict):
        return out
    scopes: List[Any] = [msg]
    # Real Kalshi WS frame (verified live 2026-10-05):
    #   {"type":"cfbenchmarks_value","sid":1,"seq":1,
    #    "msg":{"index_id":"BRTI","received_at":<ms>,
    #           "data":"{\"type\":\"value\",\"time\":<ms>,\"id\":\"BRTI\",
    #                    \"value\":\"85850.67\"}",   <-- JSON-encoded STRING
    #           "avg_60s_data":{"value":"85850.67",...}},
    #    "sending_ts_ms":<ms>}
    # The envelope is `msg`, and the live value rides inside a JSON string.
    envelope = msg.get("msg")
    if isinstance(envelope, dict):
        scopes.append(envelope)
        data_str = envelope.get("data")
        if isinstance(data_str, str):
            try:
                import json as _json

                inner = _json.loads(data_str)
                if isinstance(inner, dict):
                    scopes.append(inner)
            except (ValueError, TypeError):
                pass
    for key in ("data", "value", "index", "payload"):
        sub = msg.get(key)
        if isinstance(sub, (dict, int, float, str)):
            scopes.append(sub)
    for scope in scopes:
        if not isinstance(scope, dict):
            candidate = _num(scope)
            if candidate is not None and out["value"] is None:
                out["value"] = candidate
            continue
        if out["value"] is None:
            for key in ("value", "price", "index_value", "last", "brti"):
                out["value"] = _num(scope.get(key))
                if out["value"] is not None:
                    break
        if out["avg60"] is None:
            for key in (
                "avg_60s_data",
                "avg_60s",
                "avg60",
                "trailing_average",
                "avg_60s_average",
            ):
                out["avg60"] = _num(scope.get(key))
                if out["avg60"] is not None:
                    break
        if out["win_avg"] is None:
            for key in (
                "last_60s_windowed_average_15min",
                "windowed_average",
                "final_minute_average",
                "settlement_average",
            ):
                out["win_avg"] = _num(scope.get(key))
                if out["win_avg"] is not None:
                    break
        if out["source_ts"] is None:
            for key in (
                "time",
                "source_ts_ms",
                "timestamp_ms",
                "ts_ms",
                "source_ts",
                "sending_ts_ms",
                "received_at",
            ):
                raw = scope.get(key)
                if raw is None or isinstance(raw, bool):
                    continue
                try:
                    ts = float(raw)
                except (TypeError, ValueError):
                    continue
                if ts > 1e12:  # millis -> seconds
                    ts /= 1000.0
                if ts > 1e9:
                    out["source_ts"] = ts
                    break
    return out


class BrtiFeed:
    """Kalshi WS BRTI index feed with graceful degradation.

    Starts degraded (no price, clear error) when credentials are absent, so
    importing or constructing this on a box without keys never crashes the
    trader -- the Coinbase path simply stays primary.
    """

    def __init__(self, index_id: str = BRTI_INDEX_ID) -> None:
        self.index_id = index_id
        self.value: float = 0.0
        self.avg60: float = 0.0
        self.win_avg: float = 0.0
        self.ts: float = 0.0
        self.source_ts: float = 0.0
        self.connected = False
        self.degraded_reason = ""
        self.last_error = ""
        self.frames_seen = 0
        self.last_msg_type = ""
        self.history: Deque[Tuple[float, float]] = deque(maxlen=BRTI_POINTS)
        self._ws: Any = None
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None

    @property
    def age(self) -> float:
        return max(0.0, _now() - self.ts) if self.ts else float("inf")

    @property
    def fresh(self) -> bool:
        return self.value > 0 and self.age <= BRTI_MAX_AGE_SEC

    def estimate(self) -> float:
        """Best settlement predictor: windowed avg, else avg60, else value."""
        return self.win_avg or self.avg60 or self.value

    def estimate_kind(self) -> str:
        if self.win_avg > 0:
            return "windowed-settlement-avg"
        if self.avg60 > 0:
            return "trailing-avg60"
        if self.value > 0:
            return "brti-value"
        return "none"

    async def _on_msg(self, msg: Dict[str, Any]) -> None:
        self.frames_seen += 1
        if isinstance(msg, dict):
            self.last_msg_type = str(msg.get("type", ""))[:40]
        # One-time shape discovery: if frames arrive but nothing parses, print
        # one truncated frame to the strategy log so the real key names can be
        # wired in. Price data only, no credentials.
        if self.frames_seen == 5 and self.value <= 0 and self.avg60 <= 0:
            try:
                import json as _json

                print(
                    f"BRTI unparsed frame shape: {_json.dumps(msg)[:600]}",
                    flush=True,
                )
            except Exception:  # noqa: BLE001
                pass
        parsed = parse_brti_message(msg)
        if parsed["value"] is None and parsed["avg60"] is None:
            return
        if parsed["value"]:
            self.value = parsed["value"]
        if parsed["avg60"]:
            self.avg60 = parsed["avg60"]
        # The windowed average only exists in the final minute; outside it
        # the field is omitted, so a missing value must NOT clear the last
        # one mid-window -- but a stale one from a previous bucket would be
        # worse. Fresh ticks carry it every second in-window, so expire it
        # with the tick age instead of clearing here.
        if parsed["win_avg"]:
            self.win_avg = parsed["win_avg"]
        if parsed["source_ts"]:
            self.source_ts = parsed["source_ts"]
        self.ts = _now()
        self.history.append((self.ts, self.estimate()))

    async def start(self) -> None:
        self._stop = asyncio.Event()
        try:
            from src.clients.kalshi_client import KalshiClient as _KC  # noqa: F401
            from src.clients.kalshi_ws import KalshiWebSocket
        except Exception as exc:  # noqa: BLE001
            self.degraded_reason = f"brti unavailable: {type(exc).__name__}"
            self.last_error = str(exc)[:120]
            return
        try:
            self._ws = KalshiWebSocket(publish_to_event_bus=False)
            self._ws.on_cf_benchmarks(self._on_msg)
            await self._ws.connect()
            await self._ws.subscribe_indices([self.index_id])
            self.connected = True
            self.degraded_reason = ""
            self.last_error = ""
        except Exception as exc:  # noqa: BLE001 - degraded, Coinbase stays primary
            self._ws = None
            self.connected = False
            self.degraded_reason = f"brti degraded: {type(exc).__name__}"
            self.last_error = str(exc)[:160]
            return
        self._task = asyncio.get_event_loop().create_task(self._ws.run())

    async def stop(self) -> None:
        if self._stop:
            self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        self.connected = False
