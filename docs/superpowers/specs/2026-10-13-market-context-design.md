# Shared Market Context (UP/DOWN lanes)

**Status:** Implemented in phase 1 under the user's "take over" instruction. Independent code review found defects in the first build; this revision is the risk-only contract that remains after those fixes.
**Date:** 2026-10-13

## Principle

Context is risk-only. It can raise the required edge, shrink size, or block new pair entries. It never moves fair value, never raises size, and never creates an order. Headline direction and calendar text are reported in reasons, not priced in.

This is narrower than the originally approved design. That design allowed a small headline and breakout tilt on fair value. Review found the tilt could flip near-miss trades and feed Kelly sizing, so it was removed. A validated, one-sided directional version can be proposed later as a separate change.

## Scope

**In this delivery:**

- The six UP/DOWN lanes: `KXBTC15M`, `KXBTCD`, `KXXRP15M`, `KXXRPD`, `KXETHD`, `KXHYPE15M`, and daily Gold `KXGOLDD`. Their spot products are `BTC-USD`, `XRP-USD`, `ETH-USD`, `HYPE-USD`, and `PAXG-USD` (the tokenized-gold proxy).
- The certain-win pair path, for new pairs only.

**Deferred:**

- Context adapters for `ai_directional`, `beast_mode`, `safe_compounder`, and `quick_flip`, and quote-width modifiers for `market_making`. These run through separate entry paths and need their own adapters and bypass tests.
- Census release parsing. No Census events are used.
- BLS CPI, PPI, and Employment Situation dates. The BLS schedule returned HTTP 403 from this environment, so those releases are reported as unavailable, never as "no event".

**Found and not fixed in this delivery:** `market_making` reads quote fields from the top level of a Kalshi `get_market()` response that wraps them under `market`. Its quotes read as zero and candidates are discarded, so it has not been placing orders. Fixing it would also activate its uncapped LLM analysis and its order path, which needs a separate decision.

## Data sources

- **Four-hour range:** Coinbase public candles, five-minute granularity, explicit `start`/`end`, refreshed every two minutes. Only bars that closed at least 15 seconds ago count, so the forming bar is excluded.
- **Calendars (official):** BEA release ICS (timed UTC events) and the Federal Reserve FOMC calendar (date-only meetings), refreshed hourly. A source that parses to zero releases is a failure, not an empty calendar.
- **Headlines:** configured RSS (Reuters, NYT, BBC business) plus CoinDesk and Cointelegraph, refreshed every 10 minutes and fetched concurrently. A failed feed does not block the others.
- **Sentiment:** the configured OpenRouter sentiment model classifies only new, fresh, relevant headlines. Headline text is sanitised to one line of at most 200 characters and treated as data. Output must cite supplied URLs and fall in range, otherwise it is discarded.

## Four-hour range

Let `e` be the open time of the live five-minute bar.

- Current window: `[e - 4h, e)`, 48 completed bars. Valid with at least 39 (80%, rounded up).
- Baseline: the five contiguous windows before it. Four valid baseline windows are enough; the median of their widths is used.
- `compression_ratio = current_width / median_baseline_width`.
  - Compressed (ratio at or below 0.70): edge +0.02 and size x0.75, only when spot is in the middle 20-80% of the range.
  - Expanding (ratio at or above 1.50): edge +0.01 and size x0.75, at any position.
  - Otherwise: no modifier.
- Compression is never directional.

## Breakout (risk only)

- A candidate starts when fresh spot (at most 5 seconds old) exceeds the completed window's high or low by 0.05%. The boundary is frozen at the first crossing.
- It is confirmed after 30 seconds continuously beyond the frozen boundary and buffer. Re-entering the frozen range resets it.
- A confirmed breakout lasts 30 minutes. After expiry the tracker latches until spot re-enters, so it cannot re-arm while spot stays outside.
- Effect: edge +0.01 and size x0.75, in either direction. It does not tilt fair value.
- The tracker is fed on every trading pass, so the 30-second timer is measured in real time.

## Calendar risk

Event tiers (`event_map_v1`, matched on lower-cased official titles):

- High: BEA GDP advance estimate; BEA Personal Income and Outlays; the FOMC final (decision) day.
- Medium: BEA GDP second or third estimate.

Windows:

- Timed events: high from 15 minutes before to 10 minutes after; medium from 5 minutes before to 5 minutes after. Both ends inclusive.
- FOMC: date-only, covering the whole New York calendar day. The reason says `date-level`.

Effects: high adds edge +0.02 and size x0.50; medium adds edge +0.01 and size x0.75. Overlapping events apply only the highest tier.

## Calendar availability

Each official source is "fresh" (under 1 hour old), "stale" (1 to 6 hours), or "unavailable" (never loaded, or over 6 hours old).

- Both sources unavailable: calendar status "unavailable". Directional lanes get edge +0.01 (the bar rises; nothing is blocked). New certain-win pairs are blocked.
- One source unavailable: status "partial". Directional lanes get edge +0.01. Pairs are not blocked.
- Stale data still applies known future events, but never reads as "no event".
- Nothing is ever reported as "no event" while a source is unavailable.

## Headline scoring (risk only)

- Relevance: 1.0 for a named asset or protocol term; 0.8 for a USD macro term; otherwise 0. Matching uses word boundaries.
- Qualifying items: age from 0 to under 900 seconds (up to 60 seconds of future skew tolerated), relevance at least 0.80, sentiment confidence at least 0.75.
- Weight `= relevance x (1 - age/900)`. Score `= sum(weight x confidence x sentiment) / sum(weight)`.
- Conflicting (at least one item at or above +0.50, another at or below -0.50, and the absolute score below 0.15): edge +0.01.
- Otherwise: reason text only ("headline bias ... reason only; cited ..."). No pricing effect.

## Combined effects

- Edge additions from range, breakout, calendar, conflicting headlines, and unavailable calendar are summed and capped at +0.05. The sum applies to both sides equally, so it can only block trades; it never changes which side is chosen.
- Size multipliers (range, calendar, breakout) multiply and are clamped to [0, 1]. The multiplier never raises a clip.
- The multiplier applies to the clip when it is set, and to the configured notional when it is not. Sizing still uses base fair value.
- The $1 per-order cap and the one-contract exchange minimum still apply. A size reduction at the minimum still takes one contract, as the base policy does; context cannot round a clip up.

## Pair path (certain-win)

- Each leg is capped at the existing $1.00 per-order limit: `contracts_within_leg_cap` limits contracts by the dearer leg's price. This closes a gap where the budget could exceed $1 per leg.
- High-impact window: a new pair is blocked.
- Medium-impact window: the pair must clear net edge +0.01, and the pair budget is multiplied by 0.75.
- Calendar fully unavailable: a new pair is blocked.
- These gates apply only to new pairs. Exits and completing an open pair are never blocked.

## Failure behaviour

- Each source refreshes and fails independently, and refresh runs in the background so it never blocks a trading pass.
- Any fault inside context reads yields the neutral state and records `last_error`. The lane continues on base scoring, and the reason says so.
- Incomplete range history reports `4h range unavailable` and applies no range modifier.
- Each lane waits a random 0-60 seconds before its first fetch.

## Kill switch and rollback

- `MARKET_CONTEXT_ENABLED=0` disables all context: no fetches, neutral state, no modifiers. The default is enabled.
- Tests run with the switch off by default (`tests/conftest.py`), and tests that exercise context construct it with explicit fakes.
- Rollback: set `MARKET_CONTEXT_ENABLED=0` on the Railway service and redeploy, or revert the deployed commit.

## Acceptance coverage

- `tests/test_market_context.py` (35 tests): range regimes, the 39/38 coverage boundary, live-bar exclusion, settled-bar parsing, breakout confirm/reset/expiry/latch and stale-spot start, BEA and FOMC parsing, event windows and overlap, headline weighting, exclusion and conflicts, sentiment citation validation, RSS parsing, kill switch, unavailable calendar on zero parse, fault isolation, and the end-to-end refresh with fake sources including a failing classifier.
- `tests/test_context_wiring.py` (6 tests): pair leg cap; context never moves fair value; a context edge bar can block a base trade; context never increases size; the high-impact pair block (checked with `db_manager is None`, so it fails if the gate is removed); and the unavailable-calendar pair block.
- Full suite: 900 passed, 10 skipped.

## Verification after deploy

- Each UP/DOWN lane's reasons contain `context:`.
- Calendar status shows `fresh` for BEA and FOMC; an unavailable source is labelled as such, never as "no event".
- Workers remain in DRY; Railway deployment reaches `SUCCESS` for the pushed commit.
