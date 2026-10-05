"""Replay the veto judge over settled closes: does it kill losers, not winners?

Usage (needs OPENROUTER_API_KEY for live judging):
    python scripts/replay_veto.py --db trading_system.db --limit 200
    python scripts/replay_veto.py --db trading_system.db --canned approve
    python scripts/replay_veto.py --db trading_system.db --canned veto-all

The verdict is two win rates: kept vs vetoed. A veto worth deploying shows
vetoed-win-rate clearly BELOW kept-win-rate over 100+ closes. --canned runs
the stats machinery with no network (used by tests and dry checks).
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from typing import Any, Dict, List, Optional


def load_closes(db_path: str, limit: int) -> List[Dict[str, Any]]:
    import os
    import urllib.request
    import json
    
    # Try local first; fall back to Railway API.
    if os.path.exists(db_path):
        con = sqlite3.connect(db_path)
    else:
        # Fetch from Railway API — grab the trade_logs as JSON.
        try:
            url = "https://kalshi-frigo-production.up.railway.app/api/trades"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.load(resp)
                trades = data.get("trades", [])
                closes = []
                for t in trades[:limit]:
                    if t.get("exit_timestamp"):
                        closes.append(
                            {
                                "side": str(t.get("side", "?")).upper(),
                                "entry": float(t.get("entry_price") or 0.0),
                                "qty": int(t.get("quantity") or 0),
                                "pnl": float(t.get("pnl") or 0.0),
                                "at": str(t.get("exit_timestamp", ""))[:16],
                                "ticker": str(t.get("market_id", "")),
                            }
                        )
                if closes:
                    return closes
        except Exception as e:  # noqa: BLE001
            print(f"Warning: Railway API fetch failed ({e}), trying local DB...")
        # Fall back to local.
        if not os.path.exists(db_path):
            print(f"Error: {db_path} not found and Railway API unavailable")
            return []
        con = sqlite3.connect(db_path)
    
    cur = con.cursor()
    try:
        cur.execute(
            "SELECT side, entry_price, quantity, pnl, exit_timestamp, market_id"
            " FROM trade_logs WHERE exit_timestamp IS NOT NULL"
            " ORDER BY exit_timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        print(f"cannot read ledger: {exc}")
        rows = []
    finally:
        con.close()
    closes = []
    for side, entry, qty, pnl, exit_ts, ticker in rows:
        closes.append(
            {
                "side": str(side or "?").upper(),
                "entry": float(entry or 0.0),
                "qty": int(qty or 0),
                "pnl": float(pnl or 0.0),
                "at": str(exit_ts or "")[:16],
                "ticker": str(ticker or ""),
            }
        )
    return closes


def clip_for_close(close: Dict[str, Any], streak: str) -> Dict[str, Any]:
    """Approximate the veto context the ladder would have seen at entry."""
    entry = close["entry"]
    band = "sweet" if 0.25 <= entry <= 0.50 else ("blocked" if entry >= 0.90 else "outer")
    return {
        "ticker": close["ticker"],
        "side": close["side"].lower(),
        "ask": entry,
        "edge": 0.10,
        "required": 0.09,
        "fair": 0.75 if close["side"] == "NO" else 0.75,
        "seconds_left": 600.0,
        "streak": streak,
        "vol_pct": 0.0,
        "headlines": "replay has no headline archive",
        "band": band,
    }


def score_replay(closes: List[Dict[str, Any]], vetoes: List[bool]) -> Dict[str, Any]:
    """Pure stats: kept vs vetoed win rates and dollar deltas."""
    kept_n = kept_w = 0
    kept_pnl = 0.0
    vet_n = vet_w = 0
    vet_pnl = 0.0
    for close, vetoed in zip(closes, vetoes):
        won = close["pnl"] > 0
        if vetoed:
            vet_n += 1
            vet_w += 1 if won else 0
            vet_pnl += close["pnl"]
        else:
            kept_n += 1
            kept_w += 1 if won else 0
            kept_pnl += close["pnl"]
    return {
        "closes": len(closes),
        "kept": {
            "n": kept_n,
            "win_rate": round(kept_w / kept_n, 3) if kept_n else None,
            "pnl": round(kept_pnl, 2),
        },
        "vetoed": {
            "n": vet_n,
            "win_rate": round(vet_w / vet_n, 3) if vet_n else None,
            "pnl": round(vet_pnl, 2),
        },
    }


async def judge_live(closes: List[Dict[str, Any]]) -> List[bool]:
    from src.clients.openrouter_client import OpenRouterClient
    from src.jobs import ai_veto

    client = OpenRouterClient()
    vetoes: List[bool] = []
    losses = 0
    try:
        for c in closes:
            streak = f"{losses} same-side losses in a row" if losses else "no streak"
            reason = await ai_veto.check_veto(client, clip_for_close(c, streak))
            vetoes.append(bool(reason))
            if c["pnl"] <= 0:
                losses += 1
            else:
                losses = 0
            print(
                f"{c['at']} {c['ticker'][-3:]} {c['side']} {c['pnl']:+.2f} -> {'VETO ' + reason if reason else 'keep'}"
            )
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
    return vetoes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="trading_system.db")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--canned", choices=["approve", "veto-all"], default=None)
    ap.add_argument("--test", action="store_true", help="Use synthetic test trades (no DB/API needed)")
    args = ap.parse_args()

    if args.test:
        # Synthetic test closes for validation.
        closes = [
            {"side": "UP", "entry": 0.40, "qty": 1, "pnl": 0.50, "at": "2026-10-05 12:00", "ticker": "KXBTC15M-26OCT011715-15"},
            {"side": "UP", "entry": 0.45, "qty": 1, "pnl": -0.30, "at": "2026-10-05 12:15", "ticker": "KXBTC15M-26OCT011730-15"},
            {"side": "DOWN", "entry": 0.35, "qty": 1, "pnl": 0.40, "at": "2026-10-05 12:30", "ticker": "KXBTC15M-26OCT011745-15"},
            {"side": "DOWN", "entry": 0.50, "qty": 1, "pnl": -0.20, "at": "2026-10-05 12:45", "ticker": "KXBTC15M-26OCT012000-15"},
            {"side": "UP", "entry": 0.38, "qty": 1, "pnl": -0.15, "at": "2026-10-05 13:00", "ticker": "KXBTC15M-26OCT012015-15"},
            {"side": "UP", "entry": 0.42, "qty": 1, "pnl": 0.35, "at": "2026-10-05 13:15", "ticker": "KXBTC15M-26OCT012030-15"},
        ]
    else:
        closes = load_closes(args.db, args.limit)
        if not closes:
            print(f"No closed trades found in {args.db} (or via Railway API)")
            return 1
    if args.canned == "approve":
        vetoes = [False] * len(closes)
    elif args.canned == "veto-all":
        vetoes = [True] * len(closes)
    else:
        vetoes = asyncio.run(judge_live(closes))
    report = score_replay(closes, vetoes)
    print(f"closes: {report['closes']}")
    print(f"kept:   {report['kept']}")
    print(f"vetoed: {report['vetoed']}")
    kv = report["kept"]["win_rate"]
    vv = report["vetoed"]["win_rate"]
    if kv is not None and vv is not None and report["vetoed"]["n"] >= 10:
        print(f"verdict: veto cuts {vv:.1%} winners-vs-losers mix to a {kv:.1%} kept book")
    else:
        print("verdict: too few vetoed closes to judge - need 10+")
    return 0


if __name__ == "__main__":
    sys.exit(main())
