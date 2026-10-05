"""Hourly AI calibrator: proposes ladder parameters from evidence, applies nothing.

Once an hour this reads the closed-trade ledger (forever log: side, entry
band, UTC hour, streaks, fees) plus the live config, and asks Grok 4.7
(DeepSeek V4 Pro on failure) for one JSON proposal. The proposal is
appended to ``calibrator_proposals.jsonl`` for review. There is deliberately
no apply path: parameters move by human hand after a backtest, never by a
model at 3am. Gated by env ``AI_CALIBRATOR_ENABLED=1``; the module is never
imported on the trading path.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.jobs.ai_models import complete_for_job, parse_json_object

PROPOSAL_PATH = os.environ.get("CALIBRATOR_PATH", "calibrator_proposals.jsonl")

# Guardrails: the model proposes inside these, and anything outside is
# rejected before it is even written. The ladder's proven ranges, not the
# model's imagination.
NUMERIC_BOUNDS: Dict[str, Tuple[float, float]] = {
    "noise_usd": (5.0, 50.0),
    "min_edge": (0.03, 0.15),
    "up_override_margin": (0.0, 0.10),
    "out_of_band_extra_edge": (0.0, 0.06),
}
PREFER_SIDES = ("up", "down")

CALIBRATE_PROMPT = """You tune a 15-minute Bitcoin binary scalper on Kalshi. Current config: {config}. Ledger stats (settled closes, fee-aware): {stats}. Recent closes (side, entry, pnl, hour UTC): {recent}.

Propose parameters that maximize fee-net expectancy WITHOUT chasing noise: prefer fewer, better entries over more trades. Stay inside: noise_usd 5-50, min_edge 0.03-0.15, prefer_side up|down, up_override_margin 0-0.10, out_of_band_extra_edge 0-0.06. If the evidence does not support a change, propose the current values and say so.

Reply with exactly this JSON and nothing else: {{"noise_usd": 15.0, "min_edge": 0.06, "prefer_side": "down", "up_override_margin": 0.02, "out_of_band_extra_edge": 0.02, "rationale": "...", "confidence": "low|medium|high"}}"""


def summarize_ledger(db_path: str, limit: int = 500) -> Dict[str, Any]:
    """Aggregate settled btc_updown closes from trade_logs (pure read)."""
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute(
            "SELECT side, entry_price, quantity, pnl, fee_paid, exit_timestamp,"
            " market_id FROM trade_logs WHERE strategy='btc_updown'"
            " ORDER BY rowid DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        con.close()
    except Exception:  # noqa: BLE001 - no ledger, no proposal
        return {"closes": 0}
    if not rows:
        return {"closes": 0}
    by_side: Dict[str, Dict[str, float]] = {}
    by_hour: Dict[str, Dict[str, float]] = {}
    band_in = {"n": 0, "pnl": 0.0}
    band_out = {"n": 0, "pnl": 0.0}
    total_pnl = 0.0
    wins = 0
    recent: List[Dict[str, Any]] = []
    for i, (side, entry, qty, pnl, fee, exit_ts, ticker) in enumerate(rows):
        pnl = float(pnl or 0.0)
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        s = str(side or "?").upper()
        d = by_side.setdefault(s, {"n": 0, "pnl": 0.0})
        d["n"] += 1
        d["pnl"] += pnl
        try:
            hour = str(exit_ts)[:13]
        except Exception:  # noqa: BLE001
            hour = "?"
        h = by_hour.setdefault(hour[-2:] if len(hour) >= 2 else "?", {"n": 0, "pnl": 0.0})
        h["n"] += 1
        h["pnl"] += pnl
        try:
            e = float(entry or 0.0)
            b = band_in if 0.25 <= e <= 0.50 else band_out
            b["n"] += 1
            b["pnl"] += pnl
        except Exception:  # noqa: BLE001
            pass
        if i < 20:
            recent.append(
                {"side": s, "entry": entry, "pnl": round(pnl, 2), "at": str(exit_ts)[:16]}
            )
    return {
        "closes": len(rows),
        "win_rate": round(wins / len(rows), 3),
        "total_pnl": round(total_pnl, 2),
        "by_side": {k: {"n": v["n"], "pnl": round(v["pnl"], 2)} for k, v in by_side.items()},
        "sweet_band_in": {"n": band_in["n"], "pnl": round(band_in["pnl"], 2)},
        "sweet_band_out": {"n": band_out["n"], "pnl": round(band_out["pnl"], 2)},
        "recent": recent,
    }


def validate_proposal(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Bounds-check a model proposal. Returns the clean dict or None."""
    if not isinstance(data, dict):
        return None
    try:
        noise = float(data["noise_usd"])
        edge = float(data["min_edge"])
        side = str(data["prefer_side"]).lower()
        margin = float(data["up_override_margin"])
        extra = float(data.get("out_of_band_extra_edge", 0.02))
    except Exception:  # noqa: BLE001
        return None
    if not (NUMERIC_BOUNDS["noise_usd"][0] <= noise <= NUMERIC_BOUNDS["noise_usd"][1]):
        return None
    if not (NUMERIC_BOUNDS["min_edge"][0] <= edge <= NUMERIC_BOUNDS["min_edge"][1]):
        return None
    if side not in PREFER_SIDES:
        return None
    if not (
        NUMERIC_BOUNDS["up_override_margin"][0]
        <= margin
        <= NUMERIC_BOUNDS["up_override_margin"][1]
        and NUMERIC_BOUNDS["out_of_band_extra_edge"][0]
        <= extra
        <= NUMERIC_BOUNDS["out_of_band_extra_edge"][1]
    ):
        return None
    return {
        "noise_usd": noise,
        "min_edge": edge,
        "prefer_side": side,
        "up_override_margin": margin,
        "out_of_band_extra_edge": extra,
        "rationale": str(data.get("rationale", ""))[:1000],
        "confidence": str(data.get("confidence", "low"))[:10],
    }


async def run_once(
    client: Any,
    db_path: str,
    current_config: Dict[str, Any],
    path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """One calibration pass: summarize, ask, validate, append. No apply."""
    stats = summarize_ledger(db_path)
    if stats.get("closes", 0) < 20:
        return None  # not enough evidence to have opinions
    prompt = CALIBRATE_PROMPT.format(
        config=json.dumps(current_config, sort_keys=True),
        stats=json.dumps(stats, sort_keys=True)[:6000],
        recent=json.dumps(stats.get("recent", []))[:2000],
    )
    try:
        reply = await complete_for_job(client, "calibrator", prompt)
    except Exception:  # noqa: BLE001
        return None
    proposal = validate_proposal(parse_json_object(reply))
    if proposal is None:
        return None
    record = {
        "at": datetime.now(timezone.utc).isoformat(),
        "closes": stats["closes"],
        "proposal": proposal,
    }
    try:
        with open(path or PROPOSAL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return record


async def run_forever(interval_sec: float = 3600.0) -> None:
    """Resident loop. Enabled only with AI_CALIBRATOR_ENABLED=1."""
    if os.environ.get("AI_CALIBRATOR_ENABLED", "") != "1":
        print("calibrator disabled (AI_CALIBRATOR_ENABLED != 1)", flush=True)
        return
    from src.clients.openrouter_client import OpenRouterClient
    from src.jobs.ladder_trader import UpDownConfig

    cfg = UpDownConfig()
    current = {
        "noise_usd": cfg.noise_usd,
        "min_edge": cfg.min_edge,
        "prefer_side": cfg.prefer_side,
        "up_override_margin": cfg.up_override_margin,
        "out_of_band_extra_edge": cfg.out_of_band_extra_edge,
    }
    db_path = os.environ.get("DB_PATH", "trading_system.db")
    client: Any = None
    try:
        client = OpenRouterClient()
    except Exception:  # noqa: BLE001
        print("calibrator: no OpenRouter client, exiting", flush=True)
        return
    try:
        while True:
            try:
                rec = await run_once(client, db_path, current)
                print(
                    f"calibrator: {'proposal written' if rec else 'no proposal'}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 - the loop never dies
                print(f"calibrator pass failed: {exc}", flush=True)
            await asyncio.sleep(interval_sec)
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    asyncio.run(run_forever())
