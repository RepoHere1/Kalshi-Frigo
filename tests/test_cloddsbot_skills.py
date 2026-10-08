"""Cloddsbot skills: operator rule sheets injected into the AI job prompts."""

import inspect

from src.jobs import sentinel
from src.utils import skills


def test_skills_load_and_cap():
    text = skills.load_skill("kalshi-mechanics")
    assert "0.07" in text
    assert len(text) <= skills.MAX_SKILL_CHARS
    assert skills.load_skill("does-not-exist") == ""


def test_block_header_and_missing_are_handled():
    block = skills.skills_block("kalshi-mechanics")
    assert "OPERATOR SKILLS" in block
    assert "### skill: kalshi-mechanics" in block
    assert skills.skills_block("no-such-skill") == ""


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("CLODDSBOT_SKILLS", "0")
    assert skills.skills_block("kalshi-mechanics") == ""
    assert skills.skills_enabled() is False


def test_veto_prompt_carries_the_skills():
    from src.jobs import ai_veto

    prompt = ai_veto.build_prompt(
        {
            "ticker": "KXBTC15M-TEST",
            "side": "YES",
            "ask": 0.45,
            "edge": 0.2,
            "required": 0.1,
            "fair": 0.6,
            "seconds_left": 500,
            "streak": "none",
            "vol_pct": 0.0,
            "headlines": "none",
        }
    )
    assert "OPERATOR SKILLS" in prompt
    assert "crypto-15m-playbook" in prompt
    assert "kalshi-mechanics" in prompt


def test_veto_prompt_skills_can_be_killed(monkeypatch):
    from src.jobs import ai_veto

    monkeypatch.setenv("CLODDSBOT_SKILLS", "0")
    prompt = ai_veto.build_prompt({"ticker": "T", "side": "YES", "ask": 0.45})
    assert "OPERATOR SKILLS" not in prompt


def test_sentinel_carries_its_skill():
    src = inspect.getsource(sentinel)
    assert 'skills_block("news-halt-rules")' in src
    assert "{skills}" in sentinel.CLASSIFY_PROMPT
