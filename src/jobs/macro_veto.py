"""
Macro Veto Agent - LLM-based macro risk analysis.
"""

from typing import Any, Dict, Optional


def macro_verdict(
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
        # Placeholder - real LLM call would be async
        score = 0.0
        reason = "No macro signal"
        vetoes = False
        
        # Try to extract score from response
        # In production, call: response = await llm_client.complete(prompt)
        # For now, fail open
        result = {"score": score, "reason": reason, "vetoes": vetoes}
        
    except Exception:
        # Fail open on error
        pass
    
    if cache is not None:
        cache[cache_key] = result
    
    return result
