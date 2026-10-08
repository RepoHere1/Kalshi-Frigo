"""The DRY book may only ever bill nothing: free models, or no call.

The operator's split is DRY = free OpenRouter config, LIVE = paid. A
":free" default is not enough on its own - the paid model roster was
still reachable through explicit model arguments and fallback chains,
so one DRY cycle could bill the paid balance. free_only makes the
boundary hard: a paid model request is refused, and a non-free default
means the AI simply has no opinion instead of spending money.
"""

import inspect
from pathlib import Path
from unittest.mock import MagicMock

from src.clients import xai_client as xc
from src.clients.openrouter_client import OpenRouterClient
from src.jobs import ai_models

FREE = "nvidia/nemotron-3-super-120b-a12b:free"


def _bare(default_model: str, free_only: bool) -> OpenRouterClient:
    client = OpenRouterClient.__new__(OpenRouterClient)
    client._logger = MagicMock()
    client.default_model = default_model
    client.free_only = free_only
    return client


def test_free_only_chain_names_only_the_free_default():
    client = _bare(FREE, True)
    assert client._build_fallback_chain("deepseek/deepseek-chat") == [FREE]


def test_free_only_refuses_a_non_free_default():
    client = _bare("deepseek/deepseek-chat", True)
    assert client._build_fallback_chain() == []


def test_paid_client_keeps_the_normal_chain():
    client = _bare("anthropic/claude-sonnet-4", False)
    chain = client._build_fallback_chain("openai/o3")
    assert chain[0] == "openai/o3"
    assert "anthropic/claude-sonnet-4.5" in chain


class _FreeOnlyFake:
    """Only what complete_for_job touches."""

    free_only = True
    default_model = FREE

    def __init__(self):
        self.calls = []
        self.total_cost = 0.0
        self.request_count = 0

    async def _check_daily_limits(self):
        return True

    async def _request_single_model(self, messages, model, temperature=None, max_tokens=None):
        self.calls.append(model)
        return '{"veto": false, "reason": ""}', 0.0, 5, 5

    def _track_model_cost(self, *args):
        pass

    def _update_daily_cost(self, cost):
        pass

    async def _log_query(self, **kwargs):
        pass


async def test_dry_veto_never_names_a_paid_model():
    client = _FreeOnlyFake()
    reply = await ai_models.complete_for_job(client, "veto", "prompt")
    assert reply is not None
    assert client.calls == [FREE]


def test_dry_clients_are_constructed_free_only():
    """The wiring itself: every DRY OpenRouter client is free_only."""
    assert "free_only=True" in inspect.getsource(xc)
    root = Path(__file__).resolve().parents[1]
    ladder = (root / "src" / "jobs" / "ladder_trader.py").read_text(encoding="utf-8")
    assert "free_only=True" in ladder


def test_veto_job_roster_is_bypassed_for_free_only():
    src = inspect.getsource(ai_models.complete_for_job)
    assert "free_only" in src
