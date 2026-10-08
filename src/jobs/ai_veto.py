"""AI veto sidecar: a cheap judge that can only vote NO.

The ladder finds mispricings with arithmetic; the veto kills the ones a
rulebook cannot see: scheduled macro events inside the window, volatility
explosions, streak-chasing after same-side losses. It never approves, never
sizes, never picks direction -- so the worst a wrong or dead model does is
nothing (fail-open, logged). Gated by ``UpDownConfig.ai_veto_enabled``
(default off): with the flag off this module is never imported on the
trading path.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.jobs.ai_models import complete_for_job, parse_json_object
from src.utils.skills import skills_block

VETO_PROMPT = """You are the risk veto for a 15-minute Bitcoin binary scalper on Kalshi. You can only vote NO. Most clips are coin flips with fees; your job is killing negative-expectancy entries, not finding winners.

Clip: {ticker} {side} @ ${ask:.3f}, model edge {edge:.3f} over required {required:.3f}, fair P({side}) {fair:.3f}, {seconds_left:.0f}s to close, same-side recent: {streak}, BTC 5-min move {vol_pct:+.2f}%, fresh headlines: {headlines}.

Veto ONLY for: a scheduled macro event inside the window (FOMC/CPI/Powell/ETF decision), a volatility explosion (5-min move over 0.6%), chasing (3 or more same-side losses in a row), or stale/contradictory context. Otherwise approve.
{skills}
Reply with exactly this JSON and nothing else: {{"veto": false, "reason": ""}}"""


def build_prompt(clip: Dict[str, Any], venue: Optional[Dict[str, Any]] = None) -> str:
    """Render the veto prompt from a clip facts dict (pure, tested)."""
    venue_txt = ""
    if venue:
        imb = venue.get("imb", 0.0)
        basis = venue.get("basis_pct")
        funding = venue.get("funding")
        parts = []
        if abs(imb) > 20:
            parts.append(f"book leans {'+buy' if imb > 0 else 'sell'} {abs(imb):.0f}%")
        if basis is not None and abs(basis) > 0.03:
            parts.append(f"perp basis {basis:+.4f}%")
        if funding is not None and abs(funding) > 0.0005:
            parts.append(f"funding {funding:+.4%}")
        if parts:
            venue_txt = f" | venue: {', '.join(parts)}"

    return VETO_PROMPT.format(
        ticker=clip.get("ticker", "?"),
        side=str(clip.get("side", "")).upper(),
        ask=float(clip.get("ask") or 0.0),
        edge=float(clip.get("edge") or 0.0),
        required=float(clip.get("required") or 0.0),
        fair=float(clip.get("fair") or 0.5),
        seconds_left=float(clip.get("seconds_left") or 0.0),
        streak=str(clip.get("streak", "unknown")),
        vol_pct=float(clip.get("vol_pct") or 0.0),
        headlines=str(clip.get("headlines") or "none")[:800] + venue_txt,
        skills=skills_block("crypto-15m-playbook", "kalshi-mechanics"),
    )


def interpret_reply(text: Optional[str]) -> str:
    """Model reply -> '' (approve) or the veto reason. Fail-open always."""
    data = parse_json_object(text)
    if not data or data.get("veto") is not True:
        return ""
    reason = str(data.get("reason") or "AI veto").strip()
    return reason[:200] if reason else "AI veto"


async def check_veto(client: Any, clip: Dict[str, Any], venue: Optional[Dict[str, Any]] = None) -> str:
    """Ask the veto judge. Returns '' or the veto reason (fail-open).
    
    venue: optional {"spot": float, "imb": float, "funding": float|None, "basis_pct": float|None}
    """
    try:
        reply = await complete_for_job(
            client,
            "veto",
            build_prompt(clip, venue),
            market_id=str(clip.get("ticker") or None),
        )
    except Exception:  # noqa: BLE001 - a judge error never blocks trading
        return ""
    return interpret_reply(reply)
