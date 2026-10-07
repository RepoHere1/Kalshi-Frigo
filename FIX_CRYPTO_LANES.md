# Why the Crypto Lanes Are Not Trading — Root Causes + Fix Order

**Status:** Diagnosis complete. BTC 15-min trades; DOGE, HYPE, and BTC-1H do not.

Verified against the live Kalshi API and by driving `UpDownTrader.evaluate()` directly
(2026-10-07). Every claim below is reproducible, with the live numbers shown.

---

## The four lanes and their real state

| Lane | Series ticker | Spot feed | Actually trades? | Root cause |
|------|---------------|-----------|------------------|------------|
| `btc_updown` | KXBTC15M | BTC-USD | **YES** | (healthy) |
| `doge_updown` | KXDOGE15M | DOGE-USD | **NO** | `round(...,2)` destroys sub-dollar deltas → always "inside noise band" |
| `hyperliquid_updown` | KXHYPE15M | HYPE-USD | **NO (mostly)** | favored side quotes 0.78–0.88, above the 0.85 maker cap / taker 0.60 cap |
| `btc_1h_updown` | KXBTC1H | BTC-USD | **NO** | `KXBTC1H` does not exist on Kalshi — 0 markets ever returned |

---

## Root cause 1 — `delta = round(spot - target, 2)` kills DOGE (and any sub-$1 asset)

**File:** `src/jobs/ladder_trader.py:464`

```python
delta = round(spot - target, 2)
```

For DOGE the spot and target differ in the **4th decimal place**:

```
spot   = 0.09033
target = 0.09044
spot - target = -0.00011
round(..., 2) = -0.0      # the actual move is rounded away to exactly zero
```

`delta` is then compared against the noise band at `ladder_trader.py:537`:

```python
noise_usd = target * self.config.noise_pct   # 0.09044 * 0.0002 = ~$0.000018
if abs(delta) <= noise_usd:                  # abs(-0.0) <= 0.000018  -> ALWAYS True
    self.book.skipped_no_edge += 1
    ...  "inside the noise band, no trade"
```

Result: **DOGE increments `skipped_no_edge` on every pass and never forms an edge**,
because its real movement (fractions of a cent) is smaller than the 2-decimal
rounding resolution. DOGE is a ~$0.09 asset; BTC is ~$84,000, which is why the
same code works for BTC and silently dies for DOGE.

### Fix
Remove the fixed 2-decimal rounding. `delta` must keep full float precision (or
round to a precision that is *relative* to the asset price, e.g.
`round(spot - target, max(2, len of target decimals))`). Minimal, correct fix:

```python
delta = spot - target          # no rounding; keep all digits
```

Keep the `round(..., 2)` only for *display* strings, not the decision value.
`spot_vs_target` already carries `delta` into the signal; it must stay precise.

---

## Root cause 2 — `noise_pct` is a flat fraction of price and too tight for DOGE/HYPE

**File:** `src/jobs/ladder_trader.py:226` (`noise_pct = 0.0002`)

Noise was tuned for BTC: `$84,000 * 0.0002 = ~$17`. The docstring at line 224
even claims DOGE gets "$0.00004" noise — but that is **roughly four orders of
magnitude too small for how much DOGE moves in 15 minutes**. DOGE regularly
swings 1–3% per 15-min window; a $0.00002 deadband treats every ordinary
sub-cent wiggle as "settlement-relevant noise" while actually making the
deadband meaningless.

Once Root cause 1 is fixed, DOGE will start producing `delta` values and will
immediately over-trade on noise, because the S-curve
(`fair_up_probability`) saturates to 0 or 1 for any move >> $0.000018.

### Fix
Make noise per-asset (or per-series) rather than a single flat `0.02%`:

- Add a per-lane noise override keyed on series/spot-product (env or config
  map), e.g. DOGE `noise_pct` ≈ 0.002–0.005 (0.2–0.5%), HYPE ≈ 0.001–0.002.
- OR scale noise by realized volatility already measured in `self.rv`
  (`RealizedVol`) instead of a static fraction of price. The code already has
  `sigma_dollars` plumbing in `fair_up_probability`; the deadband should use
  the same measured vol rather than a magic fraction.

This is the second-order fix after Root cause 1; do them together or DOGE goes
from "0 trades" straight to "trades noise".

---

## Root cause 3 — HYPE's only actionable side sits above the entry caps

**Files:** `src/jobs/ladder_trader.py:71,108` (`max_entry_price=0.60`,
`max_entry_price_maker=0.85`) and the `$0.90+` hard block at line 669.

Live HYPE quotes (2026-10-07):
```
KXHYPE15M-26OCT070515-15  up 0.21/0.22   down 0.78/0.80   target 90.58
```
The model correctly prefers **DOWN** (spot 90.4 below target 90.58), but the
DOWN fill price is 0.78–0.88, which:

- exceeds the **taker cap 0.60** → blocked, and
- bounces against the **maker cap 0.85** whenever the ask is 0.88 (`_side_ok`
  at line 677: `if fill_price > _cap: return False`), and
- is one step from the absolute 0.90 hard block.

The sweet band / `max_entry_price` values were **derived from BTC's
microstructure** (the forever log: BTC "sweet band" 0.20–0.50 wins 100%). DOGE
and HYPE market-makers quote the "near-certain" side at 0.64–0.88, which BTC
never does, so the BTC-derived caps systematically refuse the very trades these
lanes exist to take.

### Fix
Do **not** blindly raise the caps globally (that would re-open BTC to the 0.90+
"4% win" band the comment explicitly forbids). Instead:

- Give each lane its own entry bands (per-series config), tuned from each
  asset's own history — HYPE needs `max_entry_price`/`max_entry_price_maker`
  that admits 0.78–0.88 with real edge, DOGE needs bands around 0.24–0.64.
- Key the bands off the lane identity (the `series` arg already flows into
  `UpDownTrader`; it just does not reach `UpDownConfig` today — plumb it).

---

## Root cause 4 — `btc_1h_updown` points at a series that does not exist

**Files:** `web_dashboard.py:147`, `cli.py:898`.

`KXBTC1H` returns **0 markets** from `/v2/markets` (status open, and with no
status filter). There is no BTC **1-hour up/down** series on Kalshi. The only
BTC series are `KXBTC15M` (15-min up/down), `KXBTC` (range), and various
one-touch/ATH markets. There is no hourly analogue.

### Fix
Pick one:

1. **Remove** the `btc_1h_updown` lane (and its `STRATEGY_DOCS`/`STRATEGY_COMMANDS`/
   `BTC_ALWAYS_ON` entries) because the market it trades does not exist.
2. **Retarget** it to a real series if the operator actually wants an hourly
   lane — but note Kalshi has no hourly up/down product; the closest real
   alternatives are other 15-minute crypto series (`KXETH15M`, `KXSOL15M`,
   `KXXRR15M`, `KXBCH15M`, `KXBNB15M`, etc.) or `KXBTC` (range, different
   settlement semantics — the ladder math assumes an up/down floor_strike).

Confirmed live series that *do* resolve quotes right now: `KXBTC15M`,
`KXDOGE15M`, `KXHYPE15M`, plus `KXETH15M`, `KXSOL15M`, `KXXRP15M`, `KXADA15M`,
`KXBNB15M`, `KXTON15M`, `KXNEAR15M`, `KXZEC15M`, `KXBCH15M`.

---

## Root cause 5 (diagnostic, not trading-blocking) — DOGE logs render prices as "0"

**File:** `src/jobs/ladder_trader.py:554,715,727`.

Every reason string formats spot/target/delta with `:,.0f`:

```python
f"{truth_kind} {spot:,.0f} is {delta:+,.0f} from target {target:,.0f}"
```

For DOGE this prints `coinbase-ws 0 is -0 from target 0`, which is why DOGE's
logs have looked broken/empty to every observer even though the process is
alive and scoring. The lane is not dead; its numbers are being truncated away
in the one place a human reads.

### Fix
Use an asset-appropriate number of decimals in the log strings, e.g. a
`_fmt(x)` helper that picks decimals by magnitude (`{:.4f}` under $1,
`{:.2f}` over $100, `{:,.0f}` only over $10,000). This makes the lane
observable and lets the operator actually see DOGE moving.

---

## Fix order (do it in this sequence)

1. **`delta = spot - target`** (line 464) — no 2-decimal rounding. Unblocks DOGE
   scoring entirely.
2. **Per-lane `noise_pct`** (or vol-scaled noise) — prevent DOGE from
   over-trading once unblocked. DOGE ~0.2–0.5%, HYPE ~0.1–0.2%, BTC keep 0.02%.
3. **Per-lane entry bands** — admit HYPE 0.78–0.88 and DOGE 0.24–0.64 with real
   edge; do not raise BTC's caps.
4. **Fix or remove `btc_1h_updown`** — the series does not exist. Retarget to a
   real 15-min crypto series (or `KXBTC` with reworked math) or delete the lane.
5. **Fix the `:,.0f` log formatting** — make sub-$1 assets readable.

After each change, re-run the direct evaluator harness (it needs no Kalshi
creds, only public market reads):

```bash
.venv/Scripts/python -c "
import asyncio
from src.jobs.market_data import Btc15mFeed, SpotFeed
from src.jobs.ladder_trader import UpDownTrader, UpDownConfig
async def go(s, p):
    f = Btc15mFeed(series=s); await f.fetch()
    sp = SpotFeed(product=p); await sp.start(); await asyncio.sleep(2)
    t = UpDownTrader(sp, f, UpDownConfig()); t._dry_cash_cache = 300.0
    sig = t.evaluate(f.nearest(), live=False)
    print(s, '->', (sig.side, sig.edge, sig.contracts, sig.reason) if sig else None)
    await sp.stop()
for s,p in [('KXDOGE15M','DOGE-USD'),('KXHYPE15M','HYPE-USD'),('KXETH15M','ETH-USD')]:
    asyncio.run(go(s,p))
"
```

Success definition: each lane emits a non-empty `side` with `edge` clearing its
own fee bar at least intermittently, and `spot_vs_target`/`delta` is a precise
non-zero float.