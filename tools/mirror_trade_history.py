"""Mirror the Railway trade history into the local trading_system.db.

The local DB is a separate file from the Railway volume, so a fresh checkout has
an empty trade_logs and every chart on the local page renders "no closed trades
yet". This pulls the history back down through the dashboard's own read API so
the local DRY and LIVE charts show the same curves the operator sees in
production.

Sources (read-only, no production state is changed beyond the book switch):
  /api/recent-trades  - paged, carries exit_price, full history, no phantoms
  /api/trades         - 500 most recent per book, carries exit_reason,
                        rationale, entry_fair, and the phantom rows

The book is switched back and forth to page each side; it is restored to the
book it started on when the mirror finishes.
"""

import json
import sqlite3
import sys
import urllib.error
import urllib.request

BASE = "https://kalshi-frigo-production.up.railway.app"
TOKEN = "Is5GsycXi2rMP6rlPaIBGXFnYwaGG6G9gcAwx5lgqWo"
DB = "trading_system.db"


def call(path, method="GET", body=None, timeout=90):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Auth-Token", TOKEN)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode()[:200]
            print(f"   HTTP {e.code} on {path}: {body_txt}")
            if e.code >= 500:
                import time

                time.sleep(3 * (attempt + 1))
                continue
            raise
        except Exception:
            import time

            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"gave up on {path}")


def switch(mode):
    d = call("/api/mode", "POST", {"mode": mode, "confirm": True})
    print(f"  switched to {mode.upper()} -> ok={d.get('ok')}")
    return d.get("mode")


def pull_recent(book):
    """Every closed trade in the book, paged."""
    rows = []
    page = 1
    while True:
        d = call(f"/api/recent-trades?page={page}&per_page=100")
        batch = d.get("trades") or []
        total = d.get("total", 0)
        rows.extend(batch)
        print(f"   page {page}: +{len(batch)} (total {len(rows)}/{total})")
        if not batch or len(rows) >= total:
            break
        page += 1
    return rows


def pull_detail(book):
    """The 500 most recent per book: rationale, exit_reason, entry_fair."""
    d = call(f"/api/trades?mode={book}")
    return d.get("trades") or []


def key(row, mode):
    return (
        row.get("market_id"),
        row.get("exit_timestamp"),
        row.get("side"),
        mode,
    )


def main():
    start = call("/api/mode").get("mode", "dry")
    print("production book at start:", start)

    merged = {}
    for book in ("dry", "live"):
        switch(book)
        print(f" pulling {book} history...")
        detail = pull_detail(book)
        for r in detail:
            merged[key(r, book)] = {
                "market_id": r.get("market_id"),
                "side": r.get("side"),
                "entry_price": r.get("entry_price"),
                "exit_price": None,
                "quantity": r.get("quantity"),
                "pnl": r.get("pnl"),
                "entry_timestamp": None,
                "exit_timestamp": r.get("exit_timestamp"),
                "rationale": r.get("rationale") or "",
                "strategy": r.get("strategy") or "",
                "exit_reason": r.get("exit_reason") or "",
                "mode": book,
                "fee_paid": 0.0,
                "entry_fair": r.get("entry_fair"),
            }
        for r in pull_recent(book):
            k = key(r, book)
            row = merged.get(k)
            if row is None:
                row = {
                    "market_id": r.get("market_id"),
                    "side": r.get("side"),
                    "entry_price": r.get("entry_price"),
                    "exit_price": None,
                    "quantity": r.get("quantity"),
                    "pnl": r.get("pnl"),
                    "entry_timestamp": None,
                    "exit_timestamp": r.get("exit_timestamp"),
                    "rationale": "",
                    "strategy": r.get("strategy") or "",
                    "exit_reason": "",
                    "mode": book,
                    "fee_paid": 0.0,
                    "entry_fair": None,
                }
                merged[k] = row
            if row.get("exit_price") is None:
                row["exit_price"] = r.get("exit_price")
            if not row.get("rationale") and r.get("rationale"):
                row["rationale"] = r.get("rationale")
            if not row.get("exit_reason") and r.get("exit_reason"):
                row["exit_reason"] = r.get("exit_reason")
            if row.get("entry_fair") is None and r.get("entry_fair") is not None:
                row["entry_fair"] = r.get("entry_fair")

    switch(start)
    print(f"production book restored to {start}")

    rows = list(merged.values())
    print(f"\nmerged rows: {len(rows)}")
    from collections import Counter

    print("by mode:", Counter(r["mode"] for r in rows))

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("DELETE FROM trade_logs")
    cur.executemany(
        "INSERT INTO trade_logs (market_id, side, entry_price, exit_price, quantity,"
        " pnl, entry_timestamp, exit_timestamp, rationale, strategy, exit_reason,"
        " mode, fee_paid, entry_fair) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                r["market_id"],
                r["side"],
                r["entry_price"],
                r["exit_price"],
                r["quantity"],
                r["pnl"],
                r["entry_timestamp"],
                r["exit_timestamp"],
                r["rationale"],
                r["strategy"],
                r["exit_reason"],
                r["mode"],
                r["fee_paid"],
                r["entry_fair"],
            )
            for r in rows
        ],
    )
    conn.commit()
    cur.execute("SELECT mode, COUNT(*) FROM trade_logs GROUP BY mode")
    print("local trade_logs now:", cur.fetchall())
    conn.close()


if __name__ == "__main__":
    main()
