"""
LLM prompting optimization for better trading decisions.

This module optimizes LLM prompts for improved decision-making,
caching responses, and adaptive prompting based on market conditions.
"""
import hashlib
import json
import time
from typing import Dict, Any, Optional
from src.utils.logging_setup import get_trading_logger
from src.config.settings import settings


class PromptOptimizer:
    """
    Optimizes LLM prompts for better trading decisions.
    
    Features:
    - Response caching for common prompts
    - Adaptive prompting based on confidence levels
    - Token limit optimization
    - Prompt validation and improvement
    """

    def __init__(self):
        self.logger = get_trading_logger("prompt_optimizer")
        self.prompt_cache = {}
        self.cache_ttl = settings.trading.prompt_cache_ttl

    def _generate_prompt_hash(self, market_data: Dict[str, Any], portfolio_data: Dict[str, Any]) -> str:
        """
        Generate a hash for prompt caching.
        
        Args:
            market_data: Market information
            portfolio_data: Portfolio information
            
        Returns:
            Hash string for the prompt
        """
        # Create a deterministic string representation
        prompt_data = {
            "ticker": market_data.get("ticker"),
            "yes_price": market_data.get("yes_price"),
            "no_price": market_data.get("no_price"),
            "volume": market_data.get("volume"),
            "available_balance": portfolio_data.get("available_balance", 0),
            "timestamp": int(time.time() / 300)  # Cache for 5 minutes
        }
        
        prompt_string = json.dumps(prompt_data, sort_keys=True)
        return hashlib.md5(prompt_string.encode()).hexdigest()

    def _is_cache_valid(self, cache_key: str) -> bool:
        """
        Check if cached prompt is still valid.
        
        Args:
            cache_key: Key for the cache entry
            
        Returns:
            True if cache is valid, False otherwise
        """
        if cache_key not in self.prompt_cache:
            return False
            
        cached_data = self.prompt_cache[cache_key]
        age = time.time() - cached_data.get("timestamp", 0)
        
        return age < self.cache_ttl

    def _optimize_prompt_for_confidence(self, base_prompt: str, confidence: float) -> str:
        """
        Optimize prompt based on confidence level.
        
        Args:
            base_prompt: Original prompt
            confidence: Confidence level (0-1)
            
        Returns:
            Optimized prompt
        """
        if not settings.trading.enable_adaptive_prompting:
            return base_prompt
            
        if confidence >= 0.8:
            # High confidence - more detailed analysis
            optimization = "\n\nNote: High confidence detected. Focus on identifying precise entry/exit points and risk management."
        elif confidence >= 0.6:
            # Medium confidence - balanced approach
            optimization = "\n\nNote: Medium confidence. Consider both conservative and aggressive positions based on edge."
        else:
            # Low confidence - conservative approach
            optimization = "\n\nNote: Low confidence. Emphasize risk management and consider smaller position sizes."
            
        return base_prompt + optimization

    def _optimize_prompt_for_tokens(self, prompt: str, max_tokens: int) -> str:
        """
        Optimize prompt to fit within token limits.
        
        Args:
            prompt: Original prompt
            max_tokens: Maximum allowed tokens
            
        Returns:
            Token-optimized prompt
        """
        if len(prompt.split()) <= max_tokens:
            return prompt
            
        # Truncate prompt while preserving key information
        words = prompt.split()
        truncated_words = words[:max_tokens - 10]  # Leave room for instructions
        truncated_prompt = " ".join(truncated_words)
        
        # Add a note about truncation
        truncated_prompt += f"\n\n[Note: Prompt truncated to {max_tokens} tokens due to limit]"
        
        self.logger.debug(
            f"Prompt truncated from {len(words)} to {len(truncated_words)} tokens",
            original_tokens=len(words),
            max_tokens=max_tokens
        )
        
        return truncated_prompt

    def get_optimized_prompt(
        self,
        market_data: Dict[str, Any],
        portfolio_data: Dict[str, Any],
        news_summary: Optional[str] = None
    ) -> str:
        """
        Generate optimized prompt for LLM decision making.
        
        Args:
            market_data: Market information
            portfolio_data: Portfolio information
            news_summary: News summary (optional)
            
        Returns:
            Optimized prompt string
        """
        # Generate cache key
        cache_key = self._generate_prompt_hash(market_data, portfolio_data)
        
        # Check cache
        if self._is_cache_valid(cache_key):
            self.logger.debug(f"Using cached prompt for {market_data.get('ticker')}")
            return self.prompt_cache[cache_key]["prompt"]
        
        # Build base prompt
        base_prompt = f"""
You are a professional Kalshi trading analyst. Analyze the following market and provide a trading decision.

Market Information:
- Ticker: {market_data.get('ticker')}
- Current YES price: ${market_data.get('yes_price', 0):.3f}
- Current NO price: ${market_data.get('no_price', 0):.3f}
- Volume: {market_data.get('volume', 0):,}
- Available balance: ${portfolio_data.get('available_balance', 0):.2f}

"""
        
        if news_summary:
            base_prompt += f"Recent News:\n{news_summary}\n\n"
            
        base_prompt += f"""
Trading Analysis Required:
1. Market sentiment and direction
2. Entry/exit strategy with specific prices
3. Position sizing recommendation
4. Risk management considerations
5. Confidence level (0.0-1.0)

Response Format:
Please respond with exactly this JSON format:
{{
    "action": "BUY" or "SELL",
    "side": "YES" or "NO", 
    "limit_price": 0.50,
    "confidence": 0.75,
    "reasoning": "Brief explanation of analysis"
}}
"""
        
        # Apply optimizations
        optimized_prompt = self._optimize_prompt_for_confidence(base_prompt, market_data.get('yes_price', 0))
        optimized_prompt = self._optimize_prompt_for_tokens(optimized_prompt, settings.trading.max_prompt_tokens)
        
        # Cache the optimized prompt
        self.prompt_cache[cache_key] = {
            "prompt": optimized_prompt,
            "timestamp": time.time(),
            "market": market_data.get('ticker')
        }
        
        self.logger.debug(
            f"Generated optimized prompt for {market_data.get('ticker')}",
            cache_key=cache_key[:8]
        )
        
        return optimized_prompt

    def clear_cache(self):
        """Clear the prompt cache."""
        self.prompt_cache.clear()
        self.logger.info("Prompt cache cleared")