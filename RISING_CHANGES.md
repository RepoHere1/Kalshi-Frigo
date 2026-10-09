# 🚀 RISING_CHANGES — The Kalshi-Frigo Upgrade Ledger

> **What this file is:** the single authoritative record of every tuning, fix, and capability this
> trading script has been given, in the order a reader needs them. Written for **two audiences at
> once**: an AI can treat it as the canonical spec of current behaviour (every knob has its exact
> value and code location), and a human can read it top-to-bottom as the story of what this machine
> now does and why.
>
> **Maintenance rule:** when a tuning lands, add a numbered entry *and* update the parameter table.
> Never rewrite history in this file — append, mark superseded numbers, move on.
>
> **As of:** 2026-10-09, deploy spine `124d18e`. Full test suite: **737 passing / 12 known
> pre-existing failures** (listed in §13).

---

## Part 0 — The machine in one paragraph

Five crypto **15-minute up/down lanes** (BTC, XRP, GOLD, ETH, HYPE) trade Kalshi binary contracts
against live spot feeds. Each lane compares its **model fair probability** (live-stream realised
volatility + spot-vs-target) with the market quote, and only fires when a real edge survives fees.
Winners **ride to the $1.00 settlement** (or get **recycled early** when nearly locked); losers are
**cut by stop**. A separate **certain-win detector** buys both sides whenever YES+NO cost < $1.00
after fees — arithmetic, not prediction. Buttons are **law**: nothing starts or stops a lane except
an operator press. Every decision, fill, and close is recorded, and the dashboard shows the model
its own **calibration report card**. LIVE money is governed by a double-switch gate; DRY is a fixed
**$200 simulated book** that rehearses the exact same code paths.

---

## 1️⃣ Money laws (the ceilings nothing may escape)

1. **Fee-aware everything.** Kalshi taker fee `0.07 × price × (1 − price)` per contract (rounded
   up to the cent); maker ≈ quarter of that (`0.0175`). The required-edge bar always adds the fee
   for the book being traded, so DRY and LIVE demand the same net edge. (`ladder_trader._required`)
2. **One bucket, one coin flip.** A lane may hold **one clip per ticker** at a time; pyramiding was
   the source of every dollar-large loss in the record. (Entry guard in `ladder_trader.evaluate`)
3. **Hard price blocks.** Entries at **≥ 90¢** are refused (the 4%-win-rate graveyard); taker caps
   at 60¢, maker at 75¢; quotes outside 1–99¢ are invalid data.
4. **Correlated-crypto cap.** BTC/ETH/XRP/HYPE move together — four same-side lanes are one bet.
   Total same-direction up/down exposure across lanes is capped at **$15**.
   (`correlated_cap_usd`)
5. **Fraction ceiling.** One clip can never exceed **50 % of the book**, even with all multipliers
   stacked. (`conviction_max_fraction`)
6. **LIVE budget.** In LIVE, a clip is additionally capped at **20 % of the real balance**
   (`live_cash_fraction`), floored to one whole contract when the account is tiny.
7. **Survival mode.** Below **$20** the LIVE book sizes down to **10 % per clip** and demands
   near-certain wins — a small refill cannot be ended by one mistake.
   (`survival_floor_usd`, `survival_clip_fraction`)
8. **Fail-closed money.** If the balance cannot be read before an order, the order is skipped; if
   LIVE balance is $0, the refusal is named in the log.

## 2️⃣ The lanes and what they trade (series truth)

9. **Each lane names its own market.** `btc_updown → KXBTC15M/BTC-USD`, `xrp_updown →
   KXXRP15M/XRP-USD`, `xau_updown → KXXAU15M/PAXG-USD`, `btc_1h_updown (ETH) →
   KXETH15M/ETH-USD`, `hyperliquid_updown → KXHYPE15M/HYPE-USD`. The Hyperliquid card sits right
   of ETH on the board.
10. **Series-normalisation fixed.** `cli.py`'s old `replace("KX","KXX")` mangled real tickers
    (`KXETH15M→KXXETH15M`, `KXBTC15M→KXXBTC15M`) so those lanes fetched nothing. Replaced with one
    rule: prefix `KX` only if absent. (`_normalize_updown_series`)
11. **BTC lane got its own flags.** It previously inherited XRP argparse defaults and silently
    traded XRP markets. Now explicit `--series KXBTC15M --spot-product BTC-USD`.
12. **Gold prices against PAXG-USD.** Coinbase has no `XAU-USD` (404); the gold lane uses PAXG
    (1 oz tokenised gold, verified live) and grades with the XAU tuning. (`apply_asset`)
13. **DOGE is dead forever.** No card, no command, blocked by `HEAVY_API_ABUSERS`; the supervisor
    will never restart it. Hyperliquid was re-enabled by operator order and toggles like any lane.
14. **Duplicate gold command removed** from the dashboard command table (schema hygiene).

## 3️⃣ The brain: model, edges, and confidence

15. **Sigma clamp made relative to spot.** The absolute `$5..$5000` clamp (BTC-sized) blinded every
    sub-$100 asset — XRP's fair was pinned at 0.4995 forever (198 dead skips while its market quoted
    a 44-point edge). Now sigma ∈ **[1e-4 .. 8e-2] × spot** for the horizon: identical for BTC
    (~$8–$6.5k) and unit-correct for XRP/ETH/HYPE. (`realized_vol.MIN/MAX_SIGMA_FRACTION`)
16. **Venue guard scoped to BTC.** The Kraken/Bitstamp dislocation guard is hardcoded BTC pairs; on
    any other lane it compared BTC's $81,707 against, say, XRP's $1.38 and vetoed 100 % of entries
    ("gap +81,707"). Now active **only on KXBTC15M**. (`UpDownTrader.__init__`)
17. **WebSocket header shim.** `websockets` renamed `extra_headers → additional_headers`; the wrong
    spelling left the BRTI truth feed dead with "unexpected keyword argument". The connect now tries
    both spellings. (`kalshi_ws.py`)
18. **QUICK_WIN experiment removed.** A 15 % minimum-edge override that no real 15-minute market
    clears — deleted from code and deploy env. Nothing can bring it back.
19. **`min_win_prob` 0.60 → 0.55.** 0.60 + the fee-aware edge gate stacked into never trading
    near-the-money dislocations the market genuinely mispriced. 0.55 keeps the majority-odds law.
    (`UpDownConfig.min_win_prob`)
20. **Tiered edge bar.** The bar is priced by the model's own confidence: views **≥ 0.72** clear
    **0.6×** the normal bar (more trades where the model actually believes); everything else
    unchanged. The marginal 1.35× cushion exists but ships at **1.0×** — nothing is refused that
    the fee alone wouldn't refuse until its own evidence pass. (`tiered_edge_bar`)
21. **Conviction sizing.** At/above 0.72, the clip is multiplied **2×** on top of fractional Kelly;
    at/above **0.85 (certified certainty)** it is **3×** — bound by every ceiling in §1.
    (`conviction_scale`, `certified_prob`, `certified_mult`)
22. **Fractional Kelly is the sizer.** `clip = balance × min(kelly_cap, kelly_scale × (fair−price)/(1−price))`,
    `kelly_scale = 0.25` (quarter-Kelly), `kelly_cap = 0.35`. Clips compound with the book; the
    same rule runs in both books.
23. **Fresh-burst latency capture.** A **≥ 2.5σ spot move in the last 20 s** that the contract
    quote has not repriced switches the entry from maker-rest to **taker-take** (edge that decays in
    seconds is worth paying the fee for). Needs ≥ 180 s runway; veto rules still apply.
    (`burst_*`, powered by `RealizedVol.recent_move`)
24. **Maker-first pricing otherwise.** Patient exits/entries rest at the touch for the quarter fee;
    `short_window` taker logic handles the urgent cases.

## 4️⃣ Payoff management (why the win rate finally makes money)

25. **Winners ride to settlement — all up/down lanes.** Scalping +8–11¢ made a 70 %-win book LOSE
    money while −78¢ resolution losses did the damage. Payoff symmetry only exists at $1.00.
    (`execute._rides_to_settlement`)
26. **Losers are cut — all lanes.** The down-tail is stopped at **−15 %** on every lane (BTC
    included); the full-size resolution losses are capped. (stop-loss helper; profit threshold 20 %)
27. **Cash recycling (velocity).** A winner already at **≥ 95¢ with ≥ 180 s left** is SOLD instead
    of ridden: per trade it's EV-neutral (the last cents price the tail), but the clip is freed
    minutes early for the next edge and the tail risk vanishes. Env: `RECYCLE_PRICE=0.95`,
    `RECYCLE_MIN_SECONDS=180`, kill `RECYCLE_WINNERS=0`. (`execute._updown_recycle_ready`)
28. **Certain-win pair trades.** When YES ask + NO ask + both taker fees leave a net ≥ **$0.015**
    per pair, the lane buys BOTH sides — the pair pays $1.00 whatever happens. Sized bigger (50 % of
    cash, $50 cap — the ONLY place size may exceed an edge clip, because it cannot lose), 120 s
    cooldown, max 3 rounds per market. Runs identically in DRY and LIVE. (`certain_win.py`)
29. **Exit volume filter fixed.** Unknown/empty book depth no longer blocks a stop or take-profit —
    a resting sell order beats a stranded position; only a genuinely thin read holds back.
    (`track.should_exit_position`)
30. **`should_exit_position` got its client.** It referenced a `kalshi_client` that was never a
    parameter — a NameError crashed every stop/take-profit pass and stranded positions.
31. **DRY money printer killed.** The profit/stop helpers sold + credited **without claiming or
    closing**, so every process re-sold the same open row every ~4 s (cash tripled with zero
    closes). Now: **claim → sell → close**, releasing the claim only when no sell booked — a booked
    fill can never be re-sold. (`execute.place_profit/stop_loss_orders`)
32. **Naive/aware datetime crash fixed.** DB timestamps come back timezone-aware; subtraction
    against naive `now()` raised and blocked real closes. One helper matches awareness.
    (`track._hours_since`)
33. **DRY settlement payouts booked.** LIVE's account is credited by Kalshi at settlement; the
    simulated book paid winners NOTHING (cash fell $80 while realized said +$12, and starved cash
    shrank Kelly clips). Resolution closes now book the fee-free payout, DRY only.
34. **LiveReaper** liquidates exchange holdings with no local row (LIVE only).

## 5️⃣ Governance: buttons, latches, and resets

35. **The button is the only authority.** No keep-alive, no auto-start, no seeder exists; a spawn
    refuses any lane the button didn't arm in that exact book. (`_spawn_strategy` latch)
36. **Start arms before it spawns** — a failed spawn keeps the intent; the supervisor retries. A
    Start stays a Start, forever. (`api_strategy_toggle`)
37. **Stop is permanent and book-scoped.** One book's buttons never touch the other book's state.
38. **Restart-proof.** After any deploy/restart the supervisor resumes exactly the `desired=1` rows,
    in the active book only. (`_strategy_supervisor_loop`)
39. **Operator tools (token-protected):**
    - `POST /api/dry/reset` — reseed the simulated book at its **$200** default.
    - `POST /api/strategies/reset-state` — the surgical kill for stale volume rows: stops every
      recorded child and walls every lane OFF in both books.
    - `POST /api/maintenance/lean` — drop the dead legacy table, fold the WAL
      (reclaimed 5.4 MB→2.4 MB live), retry VACUUM around the busy DB.
40. **Popups removed as operator order:** CLI `CLOSE ALL` auto-confirms; the live bulk-start
    confirm is bypassed; quick_flip stays cost-locked with a visible explanation (not a silent
    override).

## 6️⃣ The two books

41. **DRY = a fixed $200 simulated book** (`DEFAULT_DRY_STARTING_BALANCE`), independent of LIVE
    balance, reset-on-demand. Fee model matches LIVE so rehearsal predicts reality.
42. **DRY is free-only.** Its OpenRouter client carries `free_only=True`: only `:free` models can
    ever be requested; a paid model raises instead of billing, a non-free default yields no call.
    (`openrouter_client.free_only`; enforced in chain-building `ai_models.complete_for_job`)
43. **LIVE money gate (double switch).** `should_trade_live()`: the dashboard's persisted mode
    always wins; a spawned child must match its book stamp; env `LIVE_TRADING_ENABLED` only serves
    manual CLI runs. Children have that env scrubbed on purpose.
44. **No strategy reads the env gate.** market_making, unified_trading_system, safe_compounder, and
    (fixed last) `immediate.py` all call `should_trade_live()`. A regression test fails the build if
    any strategy code reads the env var again.
45. **LIVE styling + visibility.** Pressing LIVE turns the button word **blood red** and the LIVE
    MODE flag red with a red border (class-driven, survives polls). The dashboard never shows a
    zero for unknown money — unknowns stay unknown.

## 7️⃣ The AI brain ("Cloddsbot") and its skills

46. **No API of its own.** Cloddsbot = Claude (`anthropic/claude-sonnet-4.5`) reached exclusively
    through **OpenRouter** — the single gateway for every LLM call; DRY pinned to a free model,
    LIVE on the paid roster. The lanes themselves trade on **math**; Claude's roles are the
    **veto** and **sentinel** classifiers (fail-open guards) plus job prompts.
47. **Operator skills are injected into the prompts.** `prompts/skills/*.md` (Kalshi mechanics, the
    15-minute playbook, headline/halt rules) are appended to the veto and sentinel prompts as an
    "OPERATOR SKILLS" block. `CLODDSBOT_SKILLS=0` kills every injection. Each sheet is capped
    (2 000 chars) and a missing sheet degrades to nothing. (`src/utils/skills.py`)

## 8️⃣ Telling the truth on the dashboard (tallies & calibration)

48. **Tally labels carry their time frames.** "**DRY book P&L · since reset**" = equity − starting
    (whole-book, includes open positions at cost and the pre-fix historical era; subtitle shows
    realized + closed + fills). "**Bot realized P&L · closed trades**" = the trade-log sum since
    that log began. Value and colour always agree now (the old tile was a label/value/colour
    three-way hybrid).
49. **Calibration report card.** Every position stamps `entry_fair` at entry; every close path
    carries it into `trade_logs` (migrations included). The dashboard renders
    "**Calibration — outcomes by entry fair**": trades / win % / net $ per fair band
    (0.55–0.65 … 0.85+). This is how "near certainty" stops being an opinion.
    (`_SQL_FAIR_BANDS`, `/api/snapshot → trades.by_fair_band`)
50. **Fee-netted live logs.** LIVE trade rows store the estimated round-trip fee and net it out of
    PnL; DRY rows keep gross and show the simulated fee separately.

## 9️⃣ Housekeeping that keeps it all honest

51. **Test isolation for good.** An autouse fixture pins `DB_PATH` to a per-test temp file, and the
    book resolver trusts the `DatabaseManager`'s own path over the global env. The suite is
    machine-independent (previously a developer's local `trading_system.db` could poison runs).
52. **Every fix ships with its proof.** Regression tests exist for: the venue-guard scope, the
    series normaliser, the latch, the printer (claim/close), settlement payouts, the recycle
    predicates, conviction/certified sizing with ceilings, the tiered bar, calibration stamping,
    the no-env-gate invariant, and the blood-red LIVE styling.

## 🔟 Current parameter table (canonical values)

| Knob | Value | Where |
|---|---|---|
| DRY starting balance | **$200** | `mode.DEFAULT_DRY_STARTING_BALANCE` |
| Entry band / caps | 0.40–0.55 (taker ≤0.60, maker ≤0.75, hard block ≥0.90) | `UpDownConfig` |
| `min_edge` / `min_win_prob` | 0.06 / **0.55** | `UpDownConfig` |
| Tiered bar | strong ≥0.72 → **0.6×**; marginal cushion **1.0×** | `tiered_edge_bar` |
| Conviction / certified | ≥0.72 → **2×**; ≥0.85 → **3×**; fraction ceiling **0.50** | `conviction_scale` |
| Kelly | quarter (`0.25`), cap `0.35` | `UpDownConfig` |
| Correlated cap | **$15** same-direction | `correlated_cap_usd` |
| Burst | 20 s lookback, **2.5σ**, ≥180 s left | `burst_*` |
| Recycle | **0.95** / **180 s** (kill `RECYCLE_WINNERS=0`) | `execute._recycle_config` |
| Certain-win pair | net ≥ **$0.015**, ≤50 % cash, ≤$50, 120 s cooldown, ≤3 rounds | `certain_win.py` |
| Stop / take-profit | **−15 %** / 20 % | `execute.py` |
| LIVE budget / survival | **20 %** of balance; below **$20** → **10 %** clips | `UpDownConfig` |
| Fees | taker `0.07·p·(1−p)`; maker ≈ `0.0175·p·(1−p)` | `live_fees.py` |

## 1️⃣1️⃣ Operations quick list

- **Start/stop lanes:** dashboard buttons only (book-scoped, permanent). LIVE button = blood red.
- **Health:** `/health` · **status:** `/api/status` · **snapshot:** `/api/snapshot` (includes
  `trades.by_fair_band`).
- **Repairs:** `/api/dry/reset`, `/api/strategies/reset-state`, `/api/maintenance/lean` (token =
  `DASHBOARD_TOKEN` from the master env; never printed).
- **Deploy:** commit + `git push` → Railway auto-build; verify `/health` uptime resets.
- **LIVE checklist:** fund ≥ $20 (exits survival), press LIVE, press Start; watch the Calibration
  panel fill in. Kill switches: `RECYCLE_WINNERS=0`, `CLODDSBOT_SKILLS=0`, `VENUE_GUARD=0`,
  `CERTAIN_WIN=0`.

## 1️⃣2️⃣ Deploy spine (recent, verified in production)

`a6753e9` lane fixes → `05ccd9d`/`cafe83e` button law + DRY force-off → `3515985` keepalive
removed, free-only DRY, syntax crash fixed → `f186501` money printer + datetime fixes →
`d5b9c7c` test isolation → `1c0cfea` blood-red LIVE → `fc1b917` relative sigma clamp (model
unblinded) → `ec3ecac` burst + correlated cap → `f80ef26` tiered bar → `c8696a5`/`0508d96`
conviction + ceiling → `b51db94`/`93d00b0` cash recycling (+tuned) → `246f36e` tally truth +
certified sizing + calibration → `124d18e` LIVE-readiness sweep.

## 1️⃣3️⃣ Known pre-existing failures (leave until given their own pass)

Not caused by any change above; verified identical at HEAD: `test_edge_filter` (8, fee-math
expectations), `test_cross_venue_vol` (2, parsing), `test_dry_maker_reprice` (1),
`test_tiny_cash::test_dry_ledger_books_the_ceiled_fee` (1). **737 pass / 12 fail** is the clean
baseline.

---

## 1️⃣4️⃣ Addenda (append-only)

53. **Gold series corrected (2026-10-09 night).** The gold lane was aimed at `KXXAU15M` — a
    **dead ticker** (0 open *and* 0 settled markets in its entire history), which is why it never
    traded: 369+ consecutive `skipped_unquoted` skips while its PAXG spot feed worked fine. The
    live gold 15-minute series is **`KXGOLD15M`** (verified: 200 buckets settled in the last three
    days, ~85/day, with a daily session break around 21:00–22:00 UTC — futures-style maintenance).
    Fixed in the lane command, the docs text, `execute._UPDOWN_SERIES` (KXXAU15M kept for legacy
    positions), the CLI help, and the regression test. The lane now trades gold during session
    hours on PAXG-USD spot.

---

*Written 2026-10-09. If you are an AI reading this: the invariants in §1–§5 are the constitution —
do not weaken them without an explicit operator order recorded in this file. If you are a human:
you now hold the machine's autobiography. Keep it rising.* 🚀
