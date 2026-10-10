"""
Macro Veto Agent - LLM-based macro risk analysis.
"""

import os
from typing import Any, Dict, Optional


async def macro_verdict(
    market_title: str,
    event_category: str,
    llm_client: Optional[Any] = None,
    cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get macro risk score for a market.
    
    Returns:
        {
            "score": -2..+2 (negative = risky, positive = safe),
            "reason": str,
            "vetoes": bool,
        }
    """
    # Use cache if available
    cache_key = market_title
    if cache and cache_key in cache:
        return cache[cache_key]
    
    # Default: fail-open (no veto without headlines)
    result = {
        "score": 0.0,
        "reason": "No macro headlines",
        "vetoes": False,
    }
    
    if llm_client is None:
        # Fail open
        if cache is not None:
            cache[cache_key] = result
        return result
    
    try:
        prompt = f"""Analyze the macro risk for: {market_title}
Category: {event_category}

Return a JSON object with:
- score: -2 (very risky) to +2 (very safe)
- reason: 1-2 sentences explaining
- vetoes: true only if score < -1.0
"""
        response = await llm_client.complete(prompt, max_tokens=100)
        
        # Parse response (simple extraction)
        score = 0.0
        reason = response or "No macro signal"
        vetoes = False
        
        # Try to extract score
        for line in response.split('\n') if response else []:
            if 'score' in line.lower():
                try:
                    score = float(line.split(':')[1].strip())
                except (ValueError, IndexError):
                    pass
        
        vetoes = score < -1.0
        result = {"score": score, "reason": reason, "vetoes": vetoes}
        
    except Exception:
        # Fail open on error
        pass
    
    if cache is not None:
        cache[cache_key] = result
    
    return result
