# INVENTED IMPROVEMENTS — Win-rate / payout / frequency (proposed, not implemented)

Written 2026-10-10. All 10 are concrete, mechanism-described, and bounded by existing ceilings (§1 of RISING_CHANGES). Each names what to change and why it raises win rate or payout.

---

1. Dynamic fee-rebate maker scaling
What: when taker fee on a quote is >0.04, rest at the touch for quarter fee instead of crossing; when fee is <0.01, allow tighter maker with 1.5× size cap.
Why: more entries that survive fee-gate, fewer crossed-spread losses.

2. Correlation-hedge overlay (BTC move → reduce sibling clips)
What: if BTC spot moves >2σ in 20 s, cut XRP/ETH/HYPE open clips by 50% automatically via correlation cap enforcement.
Why: prevents correlated downside; keeps same-side exposure under $15 cap more often.

3. Gold session-extension (21:00-22:00 UTC gap)
What: during gold's daily session break, allow one small maker clip at 0.70 max (low liquidity, but not zero) instead of full skip.
Why: gold has ~85 settled/day; missing the break loses ~5-8% of available edge.

4. Calibration-drift veto (model fair vs band)
What: if entry_fair for a lane moves >0.05 outside its last 50-trade average for that band, veto new entries until 3 consistent closes restore.
Why: stops trading when model has drifted; protects win rate from miscalibration.

5. Winner-reinvestment compound (post-close sizing)
What: on a closed trade with realized PnL >20%, next clip for that lane uses 1.5× normal Kelly (still capped at 0.50 fraction / $15 cap).
Why: compounds winners faster; does not increase loss risk because ceiling binds.

6. Macro-headline velocity freeze
What: if sentiment/news score moves >2σ in 5 minutes, freeze all entries for 60 s (keeps open positions, no new clips).
Why: avoids entries into headline shocks that revert; increases per-trade win rate.

7. Double-confirmation entry gate
What: require both model fair ≥ min_win_prob AND order-book imbalance (>60% ask volume on chosen side) to fire.
Why: only trades when market is mispricing, not just when model thinks; fewer false positives.

8. Payout-optimization recycle extension (0.95 → 0.90)
What: lower recycle price to 0.90 with 120 s left instead of 0.95 / 180 s for high-conviction clips; sell earlier, free cash faster.
Why: more cycles per hour; tail-risk reduced.

9. Adaptive Kelly cap by drawdown
What: if live drawdown >10%, cap = 0.25; if flat or up, cap = 0.35. Applied in conviction_scale before ceiling.
Why: protects book when losing; accelerates when winning.

10. Phantom-row auto-clean (DB hygiene)
What: nightly run deletes DRY `exit_reason='no_kalshi_position'` rows older than 7 days and re-books cash from any unmatched closes.
Why: eliminates ghost positions that inflate open cost and shrink Kelly clips.

---
Boundaries respected: each stays inside fee-aware edge, 50% fraction ceiling, $15 correlated cap, button authority, DRY $200 independent, LIVE double-switch gate, calibration reporting.
