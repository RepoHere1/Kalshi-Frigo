# Shared Market Context (UP/DOWN lanes)

**Status:** Implemented in phase 1 under the user's "take over" instruction; reviewed by independent code review before deploy.
**Date:** 2026-10-13

## Goal

Give every UP/DOWN lane (BTC, ETH, XRP, HYPE, and daily Gold) risk awareness from:

- the last four hours of price structure (regime and confirmed breakouts);
- official scheduled releases that move Gold and USD-linked crypto;
- fresh, source-cited headlines.

Context may raise the required edge, shrink size, or make a bounded fair-value tilt. Context alone never creates an order, and missing or stale data is reported as such, never as bullish or bearish.

## Scope

**In this delivery:**

- UP/DOWN lanes: `KXBTC15M`, `KXBTCD`, `KXXRP15M`, `KXXRPD`, `KXETHD`, `KXHYPE15M`, `KXGOLDD`, mapped to `BTC-USD`, `XRP-USD`, `ETH-USD`, `HYPE-USD`, and `PAXG-USD`. Each lane owns a `MarketContext`.
- The certain-win pair path (new pairs only; see Pair path).
- The market-making Kalshi payload fix (a bug fix on the path that reads quotes, not context-driven).

**Deferred (not in this delivery):**

- Context adapters for `ai_directional`, `beast_mode`, `safe_compounder`, and `quick_flip`, and quote-width modifiers for `market_making`. These run through separate entry paths and need their own adapter and bypass tests.
- Census release parsing. The Census schedule is not yet parsed; no Census events are used.
- BLS CPI, PPI, and Employment Situation dates. The BLS schedule returned HTTP 403 from this environment, so those releases are reported as unsupported rather than as "no event".

## Data sources

- **Four-hour range:** Coinbase public candles, five-minute granularity, explicit `start` and `end` (the unbounded request returned 350 rows). The range refreshes every two minutes; the live partial bar is excluded from history and used only for spot.
- **Calendars (official only):** BEA release ICS (`bea.gov/news/schedule/ics/online-calendar-subscription.ics`, timed UTC events) and the Federal Reserve FOMC calendar (`federalreserve.gov/monetarypolicy/fomccalendars.htm`, date-only meetings). Refreshed hourly. Each source keeps its last good data.
- **Headlines:** configured RSS (Reuters, NYT, BBC business) plus CoinDesk and Cointelegraph, refreshed every 10 minutes. Feeds that fail do not block the others. Reuters' endpoint does not resolve from this environment and is reported as an error.
- **Sentiment:** the configured OpenRouter sentiment model (`settings.sentiment.sentiment_model`) classifies only new, fresh, relevant headlines. Output must cite supplied URLs and fall inside valid ranges, otherwise it is discarded.

## Range regime

Let `e` be the open time of the live five-minute bar.

- Current window: `[e - 4h, e)`, 48 completed bars. It is valid with at least 39 bars (80%, rounded up).
- Baseline: the five contiguous windows before it. Four valid windows are enough; the median of valid widths is used.
- `compression_ratio = current_width / median_baseline_width`.
  - Compressed: ratio at or below 0.70. Edge +0.02 and size x0.75, only when spot is in the middle 20-80% of the range.
  - Expanding: ratio at or above 1.50. Edge +0.01 and size x0.75, at any position.
  - Otherwise normal, with no modifier.
- Compression is never directional. Near an edge, nothing changes.

## Breakout

- A breakout candidate starts when fresh spot (at most 5 s old) exceeds the completed window's high or low by 0.05%. The boundary is frozen at the first crossing.
- It is confirmed after 30 seconds continuously beyond the frozen boundary and buffer. Returning inside the frozen range resets everything.
- A confirmed breakout lasts 30 minutes. After expiry, the tracker latches until spot re-enters the frozen range, so it cannot re-arm while spot stays outside.
- A confirmed breakout tilts fair value by 0.01 toward its direction, only when the base estimate already agrees with that direction.

## Calendar risk

Event tiers (`event_map_v1`, matched on lower-cased official titles):

- High: BEA GDP advance estimate; BEA Personal Income and Outlays; the FOMC final (decision) day.
- Medium: BEA GDP second or third estimate.

Windows:

- Timed events: high from 15 minutes before to 10 minutes after; medium from 5 minutes before to 5 minutes after. Both ends are inclusive.
- FOMC: date-only, covering the whole New York calendar day. Its reason says `date-level`.

Effects: high adds +0.02 edge and multiplies size by 0.50; medium adds +0.01 and multiplies size by 0.75. Overlapping events apply only the highest tier once.

## Headline scoring

- Relevance: 1.0 for a named asset or protocol term; 0.8 for a USD macro term; otherwise 0. Matching uses word boundaries.
- Qualifying items: age from 0 to under 900 seconds (up to 60 seconds of future skew tolerated), relevance at least 0.80, and sentiment confidence at least 0.75.
- Weight `= relevance x (1 - age/900)`. The score is `sum(weight x confidence x sentiment) / sum(weight)`.
- `|score| < 0.15`: neutral. If opposing items at +/-0.50 are also present, the status is conflicting. Neither changes fair value.
- Otherwise the fair-value tilt is `0.02 x score`.

## Combined effects

- Fair-value tilt (news plus breakout): clamped to +/-0.02.
- Edge addition (range plus calendar): capped at +0.05.
- Size multiplier: the product of the modifiers, clamped to [0, 1]. Context never raises size or any order cap.
- If the adjusted clip cannot fund one contract, the trade is skipped rather than rounded up.

## Pair path (certain-win)

- Each leg is capped at the existing $1.00 per-order limit: contracts are limited by the dearer leg's price (`contracts_within_leg_cap`). This closes a gap where the budget could exceed $1 per leg.
- A high-impact window blocks starting a new pair.
- A medium-impact window requires net edge +0.01 and multiplies the pair budget by 0.75.
- The gate is applied only to new pairs. Exits and completing an open pair are never blocked.

## Failure behaviour

- Each source refreshes and fails independently. A failure never raises into trading; the lane runs on its base scoring and the reason says what is unavailable.
- Calendar: last good data is used for 6 hours. From 1 to 6 hours the source is "stale" and only risk-increasing modifiers apply. After 6 hours it is "unavailable". Neither state yields a positive adjustment or "no event".
- Headlines: a failed feed's last good articles count only while under 15 minutes old. A failed or invalid classifier batch yields no tilt; URLs that repeatedly fail are dropped after two tries.
- Range: incomplete history reports `4h range unavailable`; no range-based adjustment applies.
- Startup: each lane waits a random 0-60 seconds before its first fetch.

## Kill switch and rollback

- `MARKET_CONTEXT_ENABLED=0` disables all context: no fetches, neutral state, and no modifiers. The default is enabled.
- Tests run with the switch off by default (`tests/conftest.py`).
- Rollback: set `MARKET_CONTEXT_ENABLED=0` on the Railway service and redeploy, or revert the deployed commit.

## Acceptance coverage

- `tests/test_market_context.py` (30 tests): range regimes and gaps, live-bar exclusion, candle parsing, breakout confirm/reset/expiry/latch, BEA/FOMC parsing, event windows and overlap, headline weighting and exclusion, sentiment citation validation, RSS parsing, kill switch, and the end-to-end refresh with fake sources, including a failing classifier.
- `tests/test_context_wiring.py` (5 tests): pair leg cap, market-making payload unwrap, fair tilt reaching the signal, edge and size applied by the trader, and the high-impact pair block.
- Full suite passes: 894 passed, 10 skipped.

## Verification

- Live read-only check (no orders): both PAXG and BTC lanes produced a valid four-hour read (regime `expanding` at the time of check) and calendar state `fresh`; CoinDesk and Cointelegraph produced dated, relevant articles.
- After deploy, verify each UP/DOWN lane's reasons contain `context:`, that calendar status is `fresh`, and that workers remain in DRY.
