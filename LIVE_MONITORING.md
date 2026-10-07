# LIVE Crypto Lane Monitoring — findings

Started monitoring: 2026-10-07 ~20:26 UTC (book=LIVE, all 4 lanes running).

## GOOD
- All four lanes are **up and heartbeating** (heartbeat ~1s, not stuck): btc_updown, doge_updown, hyperliquid_updown, btc_1h_updown (ETH15M).
- btc_updown is actually trading: **165 trades, 74W, 44.8% win, realized $1.84**. It is live (`dry: False`).
- No crashes, no `pass failed`, no last_error.
- Survival mode is live and correctly gating: `live_budget=15.61`, `required_edge=0.285` when `dry:False` (LIVE), vs `0.095` (DRY).

## BAD / NEEDS FIXING

### 1. CRITICAL — duplicate/stale processes: doge/hype/eth still running a DRY loop
`/api/bots` reports all four lanes `mode=live`, but the strategy log summaries show `"dry": True`
for doge/hype/eth (and eth shows BOTH `dry:True` and `dry:False` entries back-to-back). That means
**two processes per lane**: a stale DRY child (spawned before the book switch) and the new LIVE child.
The stale DRY loop is still scoring and, worse, could place DRY fills while the page says LIVE.
Root cause: on the DRY→LIVE switch, old child processes were not killed before the LIVE ones spawned.
The stale children hold `STRATEGY_BOOK_MODE=paper`, so `should_trade_live()` returns False for them.

### 2. DOGE and ETH near-universally "inside the noise band, no trade"
- DOGE: `skipped_no_edge: 251`, steel: spot 0.0888 vs target 0.088744, delta 0.000056, noise $0.000444. It is almost always within 0.5% of target — the 15-min DOGE move is just tiny vs the band. Nearly zero entries all session.
- ETH: same — spot 2575.00 vs target 2574.26, delta 0.74, noise $2.57. ETH barely moves > $2.57 inside a 15-min bucket.
- Net: doge/eth are effectively parked; the noise band is too wide for their realized per-bucket move.

### 3. HYPE finds real edge but gets killed by min_win_prob
- HYPE: `actionable: 1`, `edge +0.430` on DOWN (fair 0.46 vs kalshi 0.04) — a **huge genuine edge** — but `blocked: "win probability 0.46 below 0.60 on DOWN"`.
- The model's strongest signal is being refused by the 0.60 win-prob floor. min_win_prob=0.60 blocks the highest-edge trade in the whole system.

### 4. BRTI feed is dead (`No module named 'websockets'`)
All lanes: `brti_state: "stale-brti unavailable ... ModuleNotFoundError ... 'websockets'"`.
Kalshi's own settlement index is unavailable; the lanes fall back to Coinbase spot. That's the designed fallback, but it means the "certainty" of settlement is coming from one venue, not the composite the contract actually settles on.

### 5. Washington geo-block still holds the 2 NBA stuck positions
KXDJIA short closed earlier; KXNEXTTEAMNBA x2 remain (403 geo + 0.0 bid). Not related to crypto lanes, still unresolved.

## WHAT I'LL DO WHEN YOU'RE BACK
- Fix the `dry:True` book-stamp on doge/hype/eth (so they place real LIVE orders, not simulated).
- Retune DOGE/ETH noise_pct down so they actually trade.
- Reconsider min_win_prob=0.60 vs the +0.43 HYPE edge (that block looks wrong).
- Add `websockets` to the deployed environment so BRTI works.