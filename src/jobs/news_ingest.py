"""
News Interpreter - Scans reliable media for prediction market signals.

Provides real-time news ingestion, sentiment analysis, and market signal matching
to enhance AI decision-making with current events intelligence.
"""

import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

import feedparser

# NLTK sentiment analyzer - safe import at module level
try:
    from nltk.sentiment import SentimentIntensityAnalyzer
    NLTK_AVAILABLE = True
except ImportError:
    NLTK_AVAILABLE = False

from src.utils.logging_setup import get_trading_logger
from src.clients.openrouter_client import OpenRouterClient


class NewsSignal:
    """Represents a news signal relevant to prediction markets."""

    def __init__(
        self,
        signal_id: str,
        source: str,
        headline: str,
        sentiment_score: float,
        relevance_score: float,
        market_tickers: List[str],
        timestamp: datetime,
    ):
        self.id = signal_id
        self.source = source
        self.headline = headline
        self.sentiment_score = sentiment_score  # -1 to 1
        self.relevance_score = relevance_score  # 0 to 1
        self.market_tickers = market_tickers
        self.timestamp = timestamp

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "source": self.source,
            "headline": self.headline,
            "sentiment_score": self.sentiment_score,
            "relevance_score": self.relevance_score,
            "market_tickers": self.market_tickers,
            "timestamp": self.timestamp.isoformat(),
        }


class NewsInterpreter:
    """
    Main news interpreter that polls RSS feeds, analyzes sentiment,
    matches signals to prediction markets, and stores in the database.
    """

    FEED_URLS = [
        "https://feeds.reuters.com/reuters/topNews",
        "https://feeds.reuters.com/reuters/businessNews",
        "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://www.cnbc.com/id/7901028/device/rss/rss.xml",
        "https://coindesk.com/feed/",
        "https://cointelegraph.com/rss",
        "https://techcrunch.com/category/crypto/feed/",
        "https://www.wsj.com/rss/mostpopular",
        "https://feeds.npr.org/1001/rss.xml",
    ]

    MARKET_RELEVANT_TERMS = [
        "election", "crypto", "bitcoin", "ethereum", "btc", "eth", "xrp",
        "hyperliquid", "hype", "trading", "market", "price", "stock",
        "economy", "inflation", "unemployment", "gdp", "interest rate",
        "fed", "federal reserve", "cpi", "consumer price", "employment",
        "jobs", "war", "conflict", "supply", "demand", "inflation",
        "recession", "policy", "regulation", "bill", "vote", "poll",
        "campaign", "president", "senate", "congress", "parliament"
    ]

    CRYPTO_TERMS = ["crypto", "blockchain", "defi", "nft", "token", "wallet"]

    def __init__(self, db_manager=None):
        self.logger = get_trading_logger("news_interpreter")
        self.db_manager = db_manager
        self.client = None
        self._sentiment_analyzer = None
        self._initialized = False

    async def initialize(self):
        """Initialize the news interpreter with necessary components."""
        if not self._initialized:
            # Initialize NLTK sentiment analyzer
            if NLTK_AVAILABLE:
                try:
                    self._sentiment_analyzer = SentimentIntensityAnalyzer()
                    self.logger.info("NLTK sentiment analyzer initialized")
                except Exception as e:
                    self.logger.warning(f"Failed to initialize NLTK: {e}")

            # Initialize OpenRouter client for sentiment analysis
            try:
                from src.config.settings import settings
                api_key = settings.api.openrouter_api_key or ""
                if api_key:
                    self.client = OpenRouterClient(
                        api_key=api_key,
                        default_model="deepseek/deepseek-r1",
                        free_only=True,
                    )
                    self.logger.info(
                        "OpenRouter client configured for news sentiment analysis"
                    )
            except Exception as e:
                self.logger.warning(f"Could not initialize OpenRouter client: {e}")

            self._initialized = True

    async def start_interpreting(
        self,
        max_signals: int = 50,
        sentiment_threshold: float = 0.3,
        relevance_threshold: float = 0.5,
    ):
        """Main news interpretation loop."""
        await self.initialize()

        processed_headlines: Set[str] = set()

        while True:
            try:
                # Poll RSS feeds
                signals = await self._poll_feeds()

                for signal in signals:
                    # Skip if we've already processed this headline
                    headline_hash = hashlib.md5(signal.headline.encode()).hexdigest()
                    if headline_hash in processed_headlines:
                        continue

                    # Calculate sentiment
                    if self._sentiment_analyzer:
                        sentiment = self._calculate_sentiment(signal.headline)
                    else:
                        sentiment = self._estimate_sentiment(signal.headline)

                    # Estimate relevance to prediction markets
                    relevance = self._estimate_relevance(signal.headline)

                    # Mark to process
                    processed_headlines.add(headline_hash)

                    # Keep memory manageable
                    if len(processed_headlines) > max_signals * 2:
                        # Keep only the most recent entries
                        sorted_hashes = sorted(
                            processed_headlines,
                            key=lambda h: h,
                        )
                        processed_headlines = set(sorted_hashes[-max_signals :])

                    # Only process if scores meet thresholds
                    if sentiment >= sentiment_threshold and relevance >= relevance_threshold:
                        # Update signal scores after checking thresholds
                        signal.sentiment_score = sentiment
                        signal.relevance_score = relevance

                        # Store the signal in database
                        await self._store_news_signal(signal)

                        # Log the discovery
                        self.logger.info(
                            "News signal discovered",
                            headline=signal.headline[:100],
                            sentiment=sentiment,
                            relevance=relevance,
                            tickers=signal.market_tickers,
                        )

                # Wait before next poll (5 minutes)
                await asyncio.sleep(300)

            except Exception as e:
                self.logger.error(f"Error in news interpretation loop: {e}")
                await asyncio.sleep(60)  # Wait 1 minute on error

    async def _poll_feeds(self) -> List[NewsSignal]:
        """Poll RSS feeds and extract headlines."""
        signals = []

        for feed_url in self.FEED_URLS:
            try:
                # Parse feed
                feed_data = feedparser.parse(feed_url)

                for entry in feed_data.entries:
                    if not entry.get("title") or not entry.get("summary"):
                        continue

                    # Combine title and summary
                    headline = f"{entry.title} - {entry.summary}"

                    # Limit length
                    if len(headline) > 2000:
                        headline = headline[:2000]

                    # Extract market tickers from headline
                    tickers = self._extract_tickers(headline)

                    signal_id = self._generate_signal_id(
                        entry.link if entry.link else entry.id,
                        entry.title,
                    )

                    signal = NewsSignal(
                        signal_id=signal_id,
                        source=feed_data.feed.title if feed_data.feed else "unknown",
                        headline=headline,
                        sentiment_score=0.0,
                        relevance_score=0.0,
                        market_tickers=tickers,
                        timestamp=datetime.now(),
                    )

                    signals.append(signal)

            except Exception as e:
                self.logger.warning(f"Failed to poll feed {feed_url}: {e}")

        return signals

    def _calculate_sentiment(self, text: str) -> float:
        """Calculate sentiment score using NLTK SentimentIntensityAnalyzer."""
        try:
            scores = self._sentiment_analyzer.polarity_scores(text)
            sentiment = scores["compound"]

            # Adjust for trading purposes: moderate positive, moderate negative
            if sentiment < 0:
                sentiment = sentiment * 0.7
            elif sentiment > 0.3:
                sentiment = min(0.95, sentiment)

            return sentiment

        except Exception:
            return self._estimate_sentiment(text)

    def _estimate_sentiment(self, text: str) -> float:
        """Simple sentiment estimation based on keyword matching."""
        text_lower = text.lower()

        bullish_keywords = [
            "up", "rise", "increase", "growth", "bull", "rally", "gain",
            "soar", "boost", "surge", "climb", "jump", "positive", "profit",
            "win", "higher", "future", "potential", "opportunity", "breakthrough",
        ]

        bearish_keywords = [
            "down", "fall", "drop", "decline", "bear", "crash", "loss",
            "risk", "negative", "problem", "crisis", "downward", "warn",
            "concern", "failed", "trouble", "decline", "hardship", "issue",
        ]

        bullish_score = sum(1 for word in bullish_keywords if word in text_lower)
        bearish_score = sum(1 for word in bearish_keywords if word in text_lower)

        if bullish_score > bearish_score:
            return min(0.9, bullish_score * 0.1)
        elif bearish_score > bullish_score:
            return max(-0.9, -bearish_score * 0.1)
        else:
            return 0.0

    def _estimate_relevance(self, text: str) -> float:
        """Estimate relevance to prediction markets."""
        text_lower = text.lower()

        # Count market-relevant terms
        relevant_count = sum(1 for term in self.MARKET_RELEVANT_TERMS if term in text_lower)

        # Check for crypto terms (bonus)
        crypto_bonus = sum(1 for term in self.CRYPTO_TERMS if term in text_lower) * 0.1

        # Calculate relevance score (0-1)
        relevance = min(1.0, (relevant_count * 0.05) + crypto_bonus)

        return relevance

    @staticmethod
    def _extract_tickers(text: str) -> List[str]:
        """Extract prediction market tickers from text."""
        known_tickers = [
            "BTC", "ETH", "XRP", "HYPE",
            "BTC-15M", "ETH-15M", "XRP-15M",
            "BTC-1H", "ETH-1H", "XRP-1H",
            "HYPE-15M", "HYPE-1H",
        ]

        tickers: List[str] = []
        text_upper = text.upper()

        for ticker in known_tickers:
            if ticker in text_upper:
                tickers.append(ticker)

        return tickers

    @staticmethod
    def _generate_signal_id(url: str, title: str) -> str:
        """Generate a unique ID for a news signal."""
        content = f"{url}:{title}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    async def _store_news_signal(self, signal: NewsSignal):
        """Store news signal in the database."""
        try:
            if self.db_manager:
                async with self.db_manager.connect() as db:
                    # Check if signal already exists
                    try:
                        cursor = await db.execute(
                            "SELECT id FROM news_signals WHERE id = ?",
                            (signal.id,),
                        )
                        if await cursor.fetchone():
                            return  # Already stored
                    except Exception:
                        pass  # Table might not exist yet

                    # Ensure table exists - create if needed
                    await db.execute(
                        """
                        CREATE TABLE IF NOT EXISTS news_signals (
                            id TEXT PRIMARY KEY,
                            source TEXT NOT NULL,
                            headline TEXT NOT NULL,
                            sentiment_score REAL NOT NULL DEFAULT 0.0,
                            relevance_score REAL NOT NULL DEFAULT 0.0,
                            market_tickers TEXT NOT NULL DEFAULT '[]',
                            timestamp TEXT NOT NULL
                        )
                        """
                    )
                    await db.commit()

                    # Insert new signal
                    tickers_json = json.dumps(signal.market_tickers)
                    await db.execute(
                        """
                        INSERT INTO news_signals
                        (id, source, headline, sentiment_score, relevance_score,
                         market_tickers, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            signal.id,
                            signal.source,
                            signal.headline,
                            signal.sentiment_score,
                            signal.relevance_score,
                            tickers_json,
                            signal.timestamp.isoformat(),
                        ),
                    )
                    await db.commit()

            else:
                # In-memory fallback for testing (no-op if no db manager)
                pass

        except Exception as e:
            self.logger.error(f"Failed to store news signal: {e}")

    async def get_recent_signals(
        self,
        market_tickers: Optional[List[str]] = None,
        hours: int = 24,
    ) -> List[Dict]:
        """Get recent news signals, optionally filtered by market tickers."""
        try:
            if self.db_manager:
                async with self.db_manager.connect() as db:
                    # Build base query
                    base_query = """
                        SELECT id, source, headline, sentiment_score,
                               relevance_score, market_tickers, timestamp
                        FROM news_signals
                        WHERE timestamp > datetime(?, 'unixepoch')
                    """
                    params: List[Any] = [
                        (datetime.now() - timedelta(hours=hours)).isoformat()
                    ]

                    # Add ticker filter if provided
                    if market_tickers:
                        ticker_conditions = []
                        for ticker in market_tickers:
                            ticker_conditions.append(
                                f"market_tickers LIKE ?"
                            )
                            params.append(f"%{ticker}%")

                        base_query += (
                            f" AND ({' OR '.join(ticker_conditions)})"
                        )

                    base_query += " ORDER BY timestamp DESC"

                    cursor = await db.execute(
                        base_query, params
                    )
                    rows = await cursor.fetchall()

                    signals: List[Dict] = []
                    for row in rows:
                        signal_dict = dict(row)
                        # Parse market_tickers JSON
                        try:
                            parsed_tickers = json.loads(
                                signal_dict.get("market_tickers", "[]")
                            )
                            signal_dict["market_tickers"] = (
                                parsed_tickers if parsed_tickers else []
                            )
                        except Exception:
                            signal_dict["market_tickers"] = []

                        signals.append(signal_dict)

                    return signals

            else:
                # In-memory fallback - return empty if no DB
                return []

        except Exception as e:
            self.logger.error(f"Failed to get recent signals: {e}")
            return []

    async def analyze_news_for_market(
        self, market_ticker: str, hours_back: int = 24
    ) -> Dict:
        """Analyze news for a specific market ticker."""
        try:
            signals = await self.get_recent_signals(
                market_tickers=[market_ticker] if market_ticker else None,
                hours=hours_back,
            )

            if not signals:
                return {
                    "market": market_ticker,
                    "news_signals": [],
                    "sentiment": 0.0,
                    "relevance": 0.0,
                    "signal_count": 0,
                    "analysis": "No recent news signals found.",
                }

            # Analyze sentiment distribution
            sentiments = [s["sentiment_score"] for s in signals]
            relevances = [s["relevance_score"] for s in signals]

            avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0
            avg_relevance = sum(relevances) / len(relevances) if relevances else 0.0

            # Categorize signals
            bullish_signals = [s for s in signals if s["sentiment_score"] > 0.1]
            bearish_signals = [s for s in signals if s["sentiment_score"] < -0.1]
            neutral_signals = [
                s for s in signals if abs(s["sentiment_score"]) <= 0.1
            ]

            return {
                "market": market_ticker,
                "news_signals": signals,
                "sentiment": avg_sentiment,
                "relevance": avg_relevance,
                "bullish_signals": len(bullish_signals),
                "bearish_signals": len(bearish_signals),
                "neutral_signals": len(neutral_signals),
                "signal_count": len(signals),
                "analysis": self._generate_news_analysis(signals),
            }

        except Exception as e:
            self.logger.error(
                f"Failed to analyze news for {market_ticker}: {e}"
            )
            return {
                "market": market_ticker,
                "news_signals": [],
                "sentiment": 0.0,
                "relevance": 0.0,
                "signal_count": 0,
                "analysis": f"Error analyzing news: {str(e)}",
            }

    @staticmethod
    def _generate_news_analysis(signals: List[Dict]) -> str:
        """Generate human-readable analysis of news signals."""
        if not signals:
            return "No news signals available."

        bullish = sum(1 for s in signals if s["sentiment_score"] > 0.1)
        bearish = sum(1 for s in signals if s["sentiment_score"] < -0.1)

        if bullish > bearish:
            relevance = sum(s["relevance_score"] for s in signals) / len(signals)
            return f"Mostly bullish news ({bullish} bullish vs {bearish} bearish signals). News relevance: {relevance:.1%}."
        elif bearish > bullish:
            relevance = sum(s["relevance_score"] for s in signals) / len(signals)
            return f"Mostly bearish news ({bullish} bullish vs {bearish} bearish signals). News relevance: {relevance:.1%}."
        else:
            relevance = sum(s["relevance_score"] for s in signals) / len(signals)
            return f"Mixed sentiment news ({bullish} bullish vs {bearish} bearish). News relevance: {relevance:.1%}."


# Convenience functions for easy integration

async def start_news_interpreter(
    db_manager=None,
):
    """Start the news interpreter as a background task."""
    interpreter = NewsInterpreter(db_manager)
    await interpreter.initialize()
    # Start in background
    return await asyncio.create_task(interpreter.start_interpreting())


async def get_market_news_summary(
    market_ticker: str,
    hours_back: int = 24,
    db_manager=None,
) -> Dict:
    """Get news summary for a specific market ticker."""
    interpreter = NewsInterpreter(db_manager)
    await interpreter.initialize()
    return await interpreter.analyze_news_for_market(market_ticker, hours_back=hours_back)


# Test function
async def test_news_interpreter():
    """Test the news interpreter functionality."""
    interpreter = NewsInterpreter()

    print("Testing News Interpreter...")

    # Analyze news for a test market
    result = await interpreter.analyze_news_for_market("BTC", hours_back=1)

    print(f"Market: {result['market']}")
    print(f"Signals found: {result['signal_count']}")
    print(f"Average sentiment: {result['sentiment']:.3f}")
    print(f"Average relevance: {result['relevance']:.3f}")
    print(f"Analysis: {result['analysis']}")

    if result["news_signals"]:
        print("\nRecent signals:")
        for i, signal in enumerate(result["news_signals"][:3]):
            print(
                f"{i+1}. {signal['source']}: {signal['headline'][:100]}..."
            )

    return result


if __name__ == "__main__":
    asyncio.run(test_news_interpreter())