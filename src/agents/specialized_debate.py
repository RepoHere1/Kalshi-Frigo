"""
3-Model Specialized Debate System with Local Ollama.

Three hyperspecialized models for BTC trading decisions:
1. BTC Momentum Analyst: Directional signal + confidence
2. Risk Manager: Risk score + position sizing
3. Microstructure Expert: Liquidity score + execution timing

All models query local Ollama (free inference) with fallback to OpenRouter.
Consensus rule: Trade only if 2+ models agree on direction AND risk score <= 50.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import aiohttp

from src.config.settings import settings
from src.utils.logging_setup import get_trading_logger

logger = get_trading_logger(__name__)

# Ollama service configuration
OLLAMA_BASE_URL = "http://localhost:11434"  # Railway internal network
OLLAMA_MODEL = "mistral:7b-instruct-q4_K_M"
OLLAMA_TIMEOUT = 30.0  # seconds
OLLAMA_CONNECT_TIMEOUT = 10.0  # seconds

# OpenRouter fallback configuration
OPENROUTER_FALLBACK_ENABLED = True


@dataclass
class ModelDecision:
    """One specialist model's output."""

    model_name: str  # "momentum_analyst", "risk_manager", "microstructure_expert"
    direction: Optional[str]  # "up", "down", or None
    confidence: float  # 0-100 for analyst, 0-50 for risk score
    reasoning: str
    success: bool  # Did the model produce valid output?
    inference_time_ms: float  # Time to produce decision


class SpecializedDebateEngine:
    """Orchestrates 3-model debate for trading decisions."""

    def __init__(self, ollama_url: str = OLLAMA_BASE_URL):
        self.ollama_url = ollama_url
        self.model = OLLAMA_MODEL
        self._session: Optional[aiohttp.ClientSession] = None

    async def initialize(self) -> None:
        """Create HTTP session and verify Ollama connectivity."""
        self._session = aiohttp.ClientSession()
        await self._check_ollama_health()

    async def cleanup(self) -> None:
        """Clean up HTTP session."""
        if self._session:
            await self._session.close()

    async def _check_ollama_health(self) -> bool:
        """Verify Ollama is reachable and model is loaded."""
        if not self._session:
            return False
        try:
            timeout = aiohttp.ClientTimeout(total=OLLAMA_CONNECT_TIMEOUT)
            async with self._session.get(
                f"{self.ollama_url}/api/tags", timeout=timeout
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    models = data.get("models", [])
                    model_names = [m.get("name", "") for m in models]
                    if any(self.model in name for name in model_names):
                        logger.info(f"Ollama healthy, model {self.model} available")
                        return True
                    else:
                        logger.warning(f"Ollama healthy but {self.model} not found")
                        return False
        except Exception as exc:
            logger.warning(f"Ollama health check failed: {exc}")
            return False

    async def _query_ollama(self, prompt: str) -> Tuple[Optional[str], float]:
        """Query Ollama directly. Returns (response_text, inference_time_ms)."""
        if not self._session:
            return None, 0.0

        start_time = time.time()
        try:
            timeout = aiohttp.ClientTimeout(total=OLLAMA_TIMEOUT)
            payload = {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "temperature": 0.2,  # Consistent, deterministic output
            }
            async with self._session.post(
                f"{self.ollama_url}/api/generate",
                json=payload,
                timeout=timeout,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    response_text = data.get("response", "").strip()
                    elapsed_ms = (time.time() - start_time) * 1000.0
                    return response_text, elapsed_ms
        except asyncio.TimeoutError:
            logger.warning("Ollama query timeout")
        except Exception as exc:
            logger.warning(f"Ollama query failed: {exc}")
        
        return None, (time.time() - start_time) * 1000.0

    async def _query_openrouter_fallback(self, prompt: str) -> Tuple[Optional[str], float]:
        """Query OpenRouter as fallback. Returns (response_text, inference_time_ms)."""
        if not OPENROUTER_FALLBACK_ENABLED or not settings.api_config.openrouter_api_key:
            return None, 0.0

        start_time = time.time()
        try:
            if not self._session:
                self._session = aiohttp.ClientSession()
            
            timeout = aiohttp.ClientTimeout(total=30.0)
            headers = {
                "Authorization": f"Bearer {settings.api_config.openrouter_api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": "mistral/mistral-7b-instruct",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 500,
            }
            async with self._session.post(
                f"{settings.api_config.openrouter_base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=timeout,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    response_text = data["choices"][0]["message"]["content"].strip()
                    elapsed_ms = (time.time() - start_time) * 1000.0
                    logger.info("Used OpenRouter fallback for specialist model")
                    return response_text, elapsed_ms
        except Exception as exc:
            logger.warning(f"OpenRouter fallback failed: {exc}")
        
        return None, (time.time() - start_time) * 1000.0

    async def _momentum_analyst(self, market_context: Dict[str, Any]) -> ModelDecision:
        """BTC Momentum Analyst: Reads market context, outputs directional signal + confidence."""
        spot = market_context.get("spot_price", 0.0)
        target = market_context.get("target_price", 0.0)
        vol_30d = market_context.get("vol_30d", 0.0)
        market_price_up = market_context.get("market_price_up", 0.5)
        delta_pct = (spot - target) / target * 100.0 if target > 0 else 0.0

        prompt = f"""You are a BTC momentum analyst specializing in 15-minute microstructure.

Market Context:
- Current spot price: ${spot:.2f}
- Target price (settlement ref): ${target:.2f}
- Deviation: {delta_pct:+.1f}%
- 30-day realized volatility: {vol_30d:.2f}%
- Market price (UP side): ${market_price_up:.4f}

Decision: Based on momentum and microstructure, should we be LONG (UP) or SHORT (DOWN) for the next 15 minutes?

Respond in JSON format ONLY:
{{"direction": "up" OR "down", "confidence": 0-100, "reasoning": "brief reason"}}
"""
        response_text, elapsed_ms = await self._query_ollama(prompt)
        if not response_text:
            response_text, elapsed_ms = await self._query_openrouter_fallback(prompt)

        direction = None
        confidence = 0.0
        reasoning = "Failed to get response"
        success = False

        if response_text:
            try:
                parsed = json.loads(response_text)
                direction = parsed.get("direction", "").lower()
                if direction not in ["up", "down"]:
                    direction = None
                confidence = float(parsed.get("confidence", 0.0))
                confidence = max(0.0, min(100.0, confidence))
                reasoning = parsed.get("reasoning", "No reasoning provided")
                success = True
            except (json.JSONDecodeError, ValueError):
                logger.warning(f"Failed to parse momentum analyst response: {response_text}")

        return ModelDecision(
            model_name="momentum_analyst",
            direction=direction,
            confidence=confidence,
            reasoning=reasoning,
            success=success,
            inference_time_ms=elapsed_ms,
        )

    async def _risk_manager(self, market_context: Dict[str, Any], direction: Optional[str]) -> ModelDecision:
        """Risk Manager: Reads proposed trade, outputs risk score + max position size."""
        if not direction:
            return ModelDecision(
                model_name="risk_manager",
                direction=None,
                confidence=100.0,  # Risk score of 100 = DO NOT TRADE
                reasoning="No direction to risk-check",
                success=True,
                inference_time_ms=0.0,
            )

        balance = market_context.get("balance", 100.0)
        proposed_size_pct = market_context.get("proposed_size_pct", 5.0)
        daily_loss_pct = market_context.get("daily_loss_pct", 0.0)
        open_positions = market_context.get("open_positions", 0)
        win_rate_7d = market_context.get("win_rate_7d", 0.5)

        prompt = f"""You are a risk manager for algorithmic crypto trading.

Trade Context:
- Account balance: ${balance:.2f}
- Direction: {direction.upper()}
- Proposed position size: {proposed_size_pct:.1f}% of balance
- Today's loss: {daily_loss_pct:+.1f}%
- Open positions: {open_positions}
- 7-day win rate: {win_rate_7d:.1%}

Provide a RISK SCORE (0-100) where:
- 0-30: Very safe, approve the trade
- 31-50: Manageable risk, can proceed
- 51-70: Elevated risk, reconsider
- 71-100: High risk or conditions unfavorable, VETO

Respond in JSON format ONLY:
{{"risk_score": 0-100, "max_position_size_pct": 0-10, "reasoning": "brief assessment"}}
"""
        response_text, elapsed_ms = await self._query_ollama(prompt)
        if not response_text:
            response_text, elapsed_ms = await self._query_openrouter_fallback(prompt)

        risk_score = 50.0
        reasoning = "Failed to get response"
        success = False

        if response_text:
            try:
                parsed = json.loads(response_text)
                risk_score = float(parsed.get("risk_score", 50.0))
                risk_score = max(0.0, min(100.0, risk_score))
                reasoning = parsed.get("reasoning", "No reasoning provided")
                success = True
            except (json.JSONDecodeError, ValueError):
                logger.warning(f"Failed to parse risk manager response: {response_text}")

        return ModelDecision(
            model_name="risk_manager",
            direction=direction,
            confidence=risk_score,  # Risk score as confidence metric
            reasoning=reasoning,
            success=success,
            inference_time_ms=elapsed_ms,
        )

    async def _microstructure_expert(self, market_context: Dict[str, Any]) -> ModelDecision:
        """Microstructure Expert: Reads book depth + volatility, outputs liquidity score + timing."""
        bid_depth = market_context.get("bid_depth_total", 100.0)
        ask_depth = market_context.get("ask_depth_total", 100.0)
        bid_ask_spread_cents = market_context.get("bid_ask_spread_cents", 1.0)
        recent_volume = market_context.get("recent_volume", 50.0)

        prompt = f"""You are a microstructure specialist for high-frequency binary options trading.

Order Book Context:
- Total bid depth: ${bid_depth:.2f}
- Total ask depth: ${ask_depth:.2f}
- Bid-ask spread: {bid_ask_spread_cents:.1f}¢
- Recent 1-min volume: ${recent_volume:.2f}

Provide a LIQUIDITY SCORE (0-100) where:
- 0-30: Poor liquidity, avoid entry
- 31-60: Acceptable liquidity, small position
- 61-80: Good liquidity, normal sizing
- 81-100: Excellent liquidity, can size up

Also recommend EXECUTION TIMING:
- "maker": Post limit order at bid/ask
- "taker": Cross the spread immediately
- "wait": Liquidity event incoming, hold

Respond in JSON format ONLY:
{{"liquidity_score": 0-100, "execution_timing": "maker"|"taker"|"wait", "reasoning": "brief assessment"}}
"""
        response_text, elapsed_ms = await self._query_ollama(prompt)
        if not response_text:
            response_text, elapsed_ms = await self._query_openrouter_fallback(prompt)

        liquidity_score = 50.0
        timing = "maker"
        reasoning = "Failed to get response"
        success = False

        if response_text:
            try:
                parsed = json.loads(response_text)
                liquidity_score = float(parsed.get("liquidity_score", 50.0))
                liquidity_score = max(0.0, min(100.0, liquidity_score))
                timing = parsed.get("execution_timing", "maker").lower()
                if timing not in ["maker", "taker", "wait"]:
                    timing = "maker"
                reasoning = parsed.get("reasoning", "No reasoning provided")
                success = True
            except (json.JSONDecodeError, ValueError):
                logger.warning(f"Failed to parse microstructure expert response: {response_text}")

        return ModelDecision(
            model_name="microstructure_expert",
            direction=timing,  # Use direction field for timing recommendation
            confidence=liquidity_score,
            reasoning=reasoning,
            success=success,
            inference_time_ms=elapsed_ms,
        )

    async def debate(self, market_context: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """Run 3-model debate and return trading decision.

        Returns:
            (should_trade, decision_report)
        """
        start_time = time.time()

        # Run all three models in parallel
        tasks = [
            self._momentum_analyst(market_context),
            self._risk_manager(market_context, None),  # Will re-query after analyst output
            self._microstructure_expert(market_context),
        ]

        try:
            analyst, risk_pre, expert = await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as exc:
            logger.error(f"Debate error: {exc}")
            return False, {"error": str(exc), "should_trade": False}

        # Check if analyst produced a direction
        direction = None
        if isinstance(analyst, ModelDecision) and analyst.success and analyst.direction:
            direction = analyst.direction

        # Re-query risk manager with actual direction
        if direction:
            risk_mgr = await self._risk_manager(market_context, direction)
        else:
            risk_mgr = risk_pre if isinstance(risk_pre, ModelDecision) else None

        # Consensus rule: Trade only if:
        # 1. At least 2 models succeeded
        # 2. Momentum analyst agrees with a clear direction
        # 3. Risk score <= 50 (acceptable risk)
        # 4. Liquidity score >= 60 (adequate liquidity)

        models_succeeded = sum(
            1
            for m in [analyst, risk_mgr, expert]
            if isinstance(m, ModelDecision) and m.success
        )

        should_trade = (
            models_succeeded >= 2
            and direction is not None
            and isinstance(risk_mgr, ModelDecision)
            and risk_mgr.confidence <= 50.0
            and isinstance(expert, ModelDecision)
            and expert.confidence >= 60.0
        )

        report = {
            "should_trade": should_trade,
            "direction": direction,
            "consensus_met": should_trade,
            "models_succeeded": models_succeeded,
            "analyst": {
                "direction": analyst.direction if isinstance(analyst, ModelDecision) else None,
                "confidence": analyst.confidence if isinstance(analyst, ModelDecision) else 0,
                "reasoning": analyst.reasoning if isinstance(analyst, ModelDecision) else "",
                "success": analyst.success if isinstance(analyst, ModelDecision) else False,
                "inference_ms": analyst.inference_time_ms if isinstance(analyst, ModelDecision) else 0,
            },
            "risk_manager": {
                "risk_score": risk_mgr.confidence if isinstance(risk_mgr, ModelDecision) else 50,
                "reasoning": risk_mgr.reasoning if isinstance(risk_mgr, ModelDecision) else "",
                "success": risk_mgr.success if isinstance(risk_mgr, ModelDecision) else False,
                "inference_ms": risk_mgr.inference_time_ms if isinstance(risk_mgr, ModelDecision) else 0,
            },
            "expert": {
                "liquidity_score": expert.confidence if isinstance(expert, ModelDecision) else 50,
                "execution_timing": expert.direction if isinstance(expert, ModelDecision) else "maker",
                "reasoning": expert.reasoning if isinstance(expert, ModelDecision) else "",
                "success": expert.success if isinstance(expert, ModelDecision) else False,
                "inference_ms": expert.inference_time_ms if isinstance(expert, ModelDecision) else 0,
            },
            "total_inference_ms": (time.time() - start_time) * 1000.0,
        }

        return should_trade, report
