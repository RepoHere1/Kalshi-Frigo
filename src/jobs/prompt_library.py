"""
Prompt Library - Templates for LLM interactions.
"""

from typing import Optional


VETO_PROMPT_TEMPLATE = """You are a risk analyst for a Kalshi trading bot.

Market: {market_title}
Entry Fair: {entry_fair:.2f}
Current Price: {current_price:.2f}
Side: {side}

Operator Skills:
- Always respect the entry guard (one clip per ticker, price bands)
- Winners ride to settlement unless recycled near $1.00
- Losers cut at -15% stop

Analyze the risk and return:
- score: -2 (veto) to +2 (strong go)
- reason: brief explanation
"""

SCENARIO_PROMPT_TEMPLATE = """Generate 5 hypothetical scenarios for {market_title}:
1. Bull case outcome
2. Base case outcome  
3. Bear case outcome
4. Black swan event
5. Early exit condition

Return as JSON with scenario names and probabilities.
"""
