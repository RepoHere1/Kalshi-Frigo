"""
On-Chain + Sentiment Fusion - inject crypto indicators into edge scorer.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class OnChainData:
    funding_rate: float
    open_interest: float
    whale_flow_1h: float
    whale_flow_24h: float


@dataclass
class SentimentData:
    social_score: float
    news_sentiment: float


def fetch_onchain_data(
    ticker: str,
    client: Any,
) -> Optional[OnChainData]:
    """
    Fetch on-chain metrics for crypto lanes.
    """
    try:
        # Placeholder - would call exchange API
        return OnChainData(
            funding_rate=0.0001,
            open_interest=1000000,
            whale_flow_1h=50000,
            whale_flow_24h=500000,
        )
    except Exception:
        return None


def fetch_sentiment(
    ticker: str,
    client: Any,
) -> Optional[SentimentData]:
    """
    Fetch sentiment data for crypto lanes.
    """
    try:
        # Placeholder - would call sentiment API
        return SentimentData(
            social_score=0.5,
            news_sentiment=0.5,
        )
    except Exception:
        return None


def sentiment_fusion(
    base_edge: float,
    onchain: Optional[OnChainData],
    sentiment: Optional[SentimentData],
) -> float:
    """
    Adjust base edge with on-chain and sentiment features.
    
    Returns:
        adjusted edge
    """
    adjustment = 0.0
    
    # On-chain adjustments
    if onchain:
        if onchain.funding_rate > 0.0002:  # High positive funding
            adjustment -= 0.02  # Bearish signal
        if onchain.whale_flow_1h > 100000:  # Large inflow
            adjustment += 0.01
    
    # Sentiment adjustments
    if sentiment:
        if sentiment.social_score > 0.7:  # Bullish social
            adjustment += 0.02
        if sentiment.news_sentiment < 0.3:  # Bearish news
            adjustment -= 0.02
    
    return base_edge + adjustment
