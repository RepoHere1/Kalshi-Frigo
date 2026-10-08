"""News + volatility sentinel: one flag file the ladder reads every cycle.

Every 5 minutes this loop answers one question: is right now normal enough
to trade 15-minute binaries? Two inputs, no Gemini anywhere:

1. RSS headlines (CoinDesk + CoinTelegraph BTC feeds, stdlib XML, no new
   dependency) filtered to event-risk keywords, classified by Mistral Nemo
   into trade / caution / halt.
2. A local volatility guard: Coinbase spot sampled each run into a small
   ring in the state file. A 5-minute move over 0.6% is caution, over 1.0%
   is halt -- measured, not guessed, and free.

Output is ``sentinel_state.json``: {state, reason, at, headlines, move_5m}.
The ladder reads it with :func:`read_flag` (halt blocks, caution demands
extra edge). The AI can only say no; a dead feed or dead model degrades to
the volatility guard alone, never to a blind approval.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.jobs.ai_models import complete_for_job, parse_json_object

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss/tag/bitcoin",
]

# Words that put a 15-minute window under event risk. Macro decisions move
# BTC hundreds of dollars in minutes; exchange shocks do the same.
EVENT_KEYWORDS = [
    "fomc",
    "federal reserve",
    "powell",
    "cpi",
    "ppi",
    "payrolls",
    "nfp",
    "rate decision",
    "rate cut",
    "rate hike",
    "etf",
    "sec",
    "lawsuit",
    "hack",
    "exploit",
    "outage",
    "halts withdrawals",
    "doj",
    "treasury",
    "tariff",
    "war",
    "missile",
    "emergency",
    "flash crash",
]

CAUTION_MOVE_PCT = 0.6
HALT_MOVE_PCT = 1.0
HEADLINE_WINDOW_SEC = 30 * 60
PRICE_RING_MAX = 12  # 12 x 5min = one hour of context
FLAG_TTL_SEC = 10 * 60

CLASSIFY_PROMPT = """Classify these Bitcoin headlines (last 30 minutes) for a 15-minute binary options scalper. Reply with exactly this JSON and nothing else: {{"state": "trade", "reason": ""}}. state is halt when a macro decision or market shock lands inside the trading window, caution when uncertainty is elevated but unconfirmed, trade otherwise. Headlines: {headlines}{skills}"""


def fetch_rss(url: str, timeout: float = 15.0) -> List[Dict[str, Any]]:
    """Fetch one RSS feed -> [{title, link, published_ts}]. Never raises."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kalshi-frigo-sentinel/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            root = ET.fromstring(resp.read())
    except Exception:  # noqa: BLE001 - a dead feed is an empty feed
        return []
    items: List[Dict[str, Any]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        ts = 0.0
        if pub:
            try:
                from email.utils import parsedate_to_datetime

                dt = parsedate_to_datetime(pub)
                ts = dt.timestamp()
            except Exception:  # noqa: BLE001
                ts = 0.0
        if title:
            items.append({"title": title, "link": link, "published_ts": ts})
    return items


def fresh_event_items(items: List[Dict[str, Any]], now: float) -> List[Dict[str, Any]]:
    """Headlines from the window that carry event-risk keywords (pure)."""
    out = []
    for it in items:
        ts = float(it.get("published_ts") or 0.0)
        if ts and now - ts > HEADLINE_WINDOW_SEC:
            continue
        lowered = it.get("title", "").lower()
        if any(k in lowered for k in EVENT_KEYWORDS):
            out.append(it)
    return out


def fetch_spot(timeout: float = 15.0) -> float:
    """Coinbase BTC/USD spot, 0.0 on any failure."""
    try:
        with urllib.request.urlopen(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=timeout
        ) as resp:
            return float(json.load(resp)["data"]["amount"])
    except Exception:  # noqa: BLE001
        return 0.0


def ring_move_pct(ring: List[List[float]]) -> float:
    """Max abs % move between the newest print and any print ~5+ min old."""
    if len(ring) < 2:
        return 0.0
    now_px = ring[-1][1]
    now_ts = ring[-1][0]
    if now_px <= 0:
        return 0.0
    for ts, px in ring[:-1]:
        if now_ts - ts >= 240 and px > 0:
            return abs(now_px - px) / px * 100.0
    oldest_px = ring[0][1]
    return abs(now_px - oldest_px) / oldest_px * 100.0 if oldest_px > 0 else 0.0


def classify_state(move_pct: float, ai_state: Optional[str]) -> Tuple[str, str]:
    """Combine the measured move with the AI read into (state, reason)."""
    if move_pct >= HALT_MOVE_PCT:
        return "halt", f"BTC moved {move_pct:.2f}% in 5 minutes (measured)"
    if ai_state == "halt":
        return "halt", "headline classifier: event risk in window"
    if move_pct >= CAUTION_MOVE_PCT:
        return "caution", f"BTC moved {move_pct:.2f}% in 5 minutes (measured)"
    if ai_state == "caution":
        return "caution", "headline classifier: elevated uncertainty"
    return "trade", "headlines quiet, volatility normal"


async def run_once(
    client: Any = None,
    state_path: Optional[str] = None,
    spot_fn: Callable[[], float] = fetch_spot,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """One sentinel pass: sample, classify, write the flag file, return it."""
    now = now if now is not None else time.time()
    path = state_path or os.environ.get("SENTINEL_STATE_PATH", "sentinel_state.json")
    prev: Dict[str, Any] = {}
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:  # noqa: BLE001 - first run has no file
        prev = {}
    ring: List[List[float]] = [r for r in prev.get("price_ring", []) if len(r) == 2]
    px = spot_fn()
    if px > 0:
        ring.append([now, px])
        ring = ring[-PRICE_RING_MAX:]
    move_pct = ring_move_pct(ring)

    headlines: List[str] = []
    if client is not None:
        items: List[Dict[str, Any]] = []
        for url in FEEDS:
            items += fetch_rss(url)
        for it in fresh_event_items(items, now):
            headlines.append(it["title"])
    headlines = headlines[:12]

    ai_state: Optional[str] = None
    if client is not None and headlines:
        try:
            from src.utils.skills import skills_block

            reply = await complete_for_job(
                client,
                "sentinel",
                CLASSIFY_PROMPT.format(
                    headlines=" | ".join(headlines),
                    skills=skills_block("news-halt-rules"),
                ),
            )
            data = parse_json_object(reply)
            if data and str(data.get("state", "")).lower() in ("trade", "caution", "halt"):
                ai_state = str(data.get("state")).lower()
        except Exception:  # noqa: BLE001 - rules alone still stand
            ai_state = None

    state, reason = classify_state(move_pct, ai_state)
    flag = {
        "state": state,
        "reason": reason,
        "at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "at_ts": now,
        "move_5m_pct": round(move_pct, 3),
        "headlines": headlines,
        "price_ring": ring,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(flag, f)
    except Exception:  # noqa: BLE001 - a read-only disk still returns the flag
        pass
    return flag


def read_flag(state_path: Optional[str] = None, now: Optional[float] = None) -> Tuple[str, str]:
    """Ladder-side read: (state, reason). Missing/stale file means trade.

    A sentinel that cannot be read must never halt trading by itself: it is
    an advisor, and absence of advice is permission.
    """
    now = now if now is not None else time.time()
    path = state_path or os.environ.get("SENTINEL_STATE_PATH", "sentinel_state.json")
    try:
        with open(path, encoding="utf-8") as f:
            flag = json.load(f)
    except Exception:  # noqa: BLE001
        return "trade", "no sentinel flag yet"
    if now - float(flag.get("at_ts") or 0.0) > FLAG_TTL_SEC:
        return "trade", "sentinel flag stale, ignored"
    state = str(flag.get("state") or "trade").lower()
    if state not in ("trade", "caution", "halt"):
        return "trade", "sentinel flag unreadable"
    return state, str(flag.get("reason") or "")


async def run_forever(interval_sec: float = 300.0) -> None:
    """Resident loop: import OpenRouter lazily so DRY never pays for it."""
    from src.clients.openrouter_client import OpenRouterClient

    client: Any = None
    try:
        client = OpenRouterClient()
    except Exception:  # noqa: BLE001 - rules-only mode
        client = None
    try:
        while True:
            try:
                flag = await run_once(client)
                print(f"sentinel: {flag['state']} ({flag['reason']})", flush=True)
            except Exception as exc:  # noqa: BLE001 - the loop never dies
                print(f"sentinel pass failed: {exc}", flush=True)
            await asyncio.sleep(interval_sec)
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    asyncio.run(run_forever())
