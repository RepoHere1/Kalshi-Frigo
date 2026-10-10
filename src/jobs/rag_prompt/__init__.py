"""
Regime-Adaptive RAG Prompting - retrieve past markets via embedding and inject outcomes.
"""

import math
from typing import Any, Callable, Dict, List


def find_similar_markets(
    ticker: str,
    category: str,
    all_markets: List[Dict[str, Any]],
    embedding_fn: Callable[[str, str], List[float]],
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """
    Find top_k most similar historical markets.
    
    Args:
        ticker: current market ticker
        category: market category (e.g. "crypto", "macro")
        all_markets: historical markets with outcomes
        embedding_fn: function to get market embedding
        top_k: number to return
    
    Returns:
        List of similar markets with outcomes
    """
    if not all_markets:
        return []
    
    # Get embedding for current market
    current_embed = embedding_fn(ticker, category)
    
    # Compute similarity (cosine)
    scored = []
    for market in all_markets:
        other_embed = embedding_fn(market.get("ticker", ""), market.get("category", ""))
        # Cosine similarity
        dot = sum(a * b for a, b in zip(current_embed, other_embed))
        norm1 = math.sqrt(sum(a * a for a in current_embed))
        norm2 = math.sqrt(sum(b * b for b in other_embed))
        if norm1 > 0 and norm2 > 0:
            sim = dot / (norm1 * norm2)
        else:
            sim = 0.0
        scored.append((sim, market))
    
    # Sort and return top_k
    scored.sort(key=lambda x: x[0], reverse=True)
    return [market for _, market in scored[:top_k]]


def inject_rag_context(
    ticker: str,
    similar_markets: List[Dict[str, Any]],
    model_accuracy: Dict[str, float],
) -> str:
    """
    Generate RAG context to inject into LLM prompt.
    
    Returns:
        Context string
    """
    if not similar_markets:
        return "No similar historical markets found."
    
    lines = ["Historical analogs with outcomes:"]
    for i, market in enumerate(similar_markets, 1):
        outcome = market.get("outcome", "unknown")
        win_rate = market.get("model_win_rate", 0.0)
        actual_win = market.get("actual_win", False)
        lines.append(f"{i}. {market.get('ticker', '')}: model={win_rate:.0%}, outcome={outcome}")
    
    # Add model accuracy for this category
    acc = model_accuracy.get(ticker, model_accuracy.get("default", 0.0))
    lines.append(f"\nModel accuracy on {ticker}: {acc:.0%}")
    
    return "\n".join(lines)
