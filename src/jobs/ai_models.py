"""Model roster for the AI advisory stack: veto, sentinel, calibrator.

All calls route through OpenRouter (never direct, never a second provider).
Each job names a primary and a fallback; a dead model degrades to the
fallback, and a dead stack degrades to pure math -- callers treat ``None``
as "no opinion" and keep trading the arithmetic.

Prices below are OpenRouter standard listings, late September 2026, USD per
1M tokens (in/out). They feed cost tracking only; the bill is bounded by
cadence (veto ~40/day, sentinel ~288/day, calibrator 24/day => ~$0.05-0.30
against the $10/day budget).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from json_repair import repair_json

# job -> primary / fallback / sampling. Temperature 0 for judges (veto,
# sentinel): a veto that changes its mind with temperature is not a rule.
JOB_MODELS: Dict[str, Dict[str, Any]] = {
    "veto": {
        "primary": "deepseek/deepseek-v4.1-flash",
        "fallback": "openai/gpt-5-nano",
        "max_tokens": 300,
        "temperature": 0.0,
    },
    "sentinel": {
        "primary": "mistralai/mistral-nemo",
        "fallback": "qwen/qwen3.7-flash",
        "max_tokens": 200,
        "temperature": 0.0,
    },
    "calibrator": {
        "primary": "x-ai/grok-4.7",
        "fallback": "deepseek/deepseek-v4-pro",
        "max_tokens": 1500,
        "temperature": 0.2,
    },
}


async def complete_for_job(
    client: Any,
    job: str,
    prompt: str,
    market_id: Optional[str] = None,
) -> Optional[str]:
    """One completion for *job* via primary, then fallback, else None.

    Deliberately bypasses ``get_completion``: its built-in chain falls back
    to frontier flagships ($3-15/1K), so one dead cheap model would burn a
    flagship call before this helper ever saw the failure. Cost tracking and
    the daily budget use the same client bookkeeping.
    """
    cfg = JOB_MODELS[job]
    if not await client._check_daily_limits():
        return None
    for model in (cfg["primary"], cfg["fallback"]):
        try:
            content, cost, in_tok, out_tok = await client._request_single_model(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=cfg["temperature"],
                max_tokens=cfg["max_tokens"],
            )
            client._track_model_cost(model, in_tok, out_tok, cost)
            client.total_cost += cost
            client.request_count += 1
            client._update_daily_cost(cost)
            await client._log_query(
                strategy=f"ai_{job}",
                query_type=f"ai_{job}",
                prompt=prompt,
                response=content,
                market_id=market_id,
                tokens_used=in_tok + out_tok,
                cost_usd=cost,
            )
            return content
        except Exception:  # noqa: BLE001 - next model, then no opinion
            continue
    return None


def parse_json_object(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Best-effort parse of a model reply into a dict, else None."""
    if not text:
        return None
    import json as _json

    for candidate in (text, _safe_repair(text)):
        if not candidate:
            continue
        try:
            data = _json.loads(candidate)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict):
            return data
    return None


def _safe_repair(text: str) -> Optional[str]:
    try:
        return repair_json(text)
    except Exception:  # noqa: BLE001
        return None
