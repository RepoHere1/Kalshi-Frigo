"""AI advisory stack: veto, sentinel, calibrator. No network in any test.

Every OpenRouter call is replaced by a fake client with canned replies, so
this suite proves the decision logic (fail-open, halt/caution/trade,
bounds-checking, replay stats) without spending a cent.
"""

import asyncio
import json
import time

from scripts.replay_veto import score_replay
from src.jobs import ai_models, ai_veto, sentinel
from src.jobs.ai_calibrator import validate_proposal


class _FakeClient:
    """Stands in for OpenRouterClient: canned reply or boom, with budget."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []
        self.total_cost = 0.0
        self.request_count = 0

    async def _check_daily_limits(self):
        return True

    async def _request_single_model(self, messages, model, temperature=None, max_tokens=None):
        self.calls.append(model)
        reply = self._replies.pop(0) if self._replies else None
        if reply is None or isinstance(reply, Exception):
            raise reply or RuntimeError("model down")
        return reply, 0.0001, 100, 20

    def _track_model_cost(self, *a):
        pass

    def _update_daily_cost(self, *a):
        pass

    async def _log_query(self, **kw):
        pass


def _clip(**kw):
    base = {
        "ticker": "KXBTC15M-26OCT011715-15",
        "side": "down",
        "ask": 0.44,
        "edge": 0.12,
        "required": 0.09,
        "fair": 0.25,
        "seconds_left": 600.0,
        "streak": "no streak",
        "vol_pct": 0.1,
        "headlines": "none",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Model roster + plumbing
# ---------------------------------------------------------------------------
def test_job_models_use_no_gemini():
    """The stack was ordered Gemini-free; keep it that way."""
    blob = json.dumps(ai_models.JOB_MODELS).lower()
    assert "gemini" not in blob
    assert ai_models.JOB_MODELS["veto"]["primary"] == "deepseek/deepseek-v4.1-flash"
    assert ai_models.JOB_MODELS["veto"]["fallback"] == "openai/gpt-5-nano"
    assert ai_models.JOB_MODELS["sentinel"]["primary"] == "mistralai/mistral-nemo"
    assert ai_models.JOB_MODELS["calibrator"]["primary"] == "x-ai/grok-4.7"
    assert ai_models.JOB_MODELS["calibrator"]["fallback"] == "deepseek/deepseek-v4-pro"


def test_complete_falls_back_then_gives_up():
    async def _run():
        c = _FakeClient([RuntimeError("down"), '{"veto": false}'])
        out = await ai_models.complete_for_job(c, "veto", "prompt")
        assert out == '{"veto": false}'
        assert c.calls == [
            "deepseek/deepseek-v4.1-flash",
            "openai/gpt-5-nano",
        ]
        c2 = _FakeClient([RuntimeError("x"), RuntimeError("y")])
        assert await ai_models.complete_for_job(c2, "veto", "prompt") is None

    asyncio.run(_run())


def test_parse_json_object_tolerates_prose():
    assert ai_models.parse_json_object('{"veto": true}') == {"veto": True}
    assert ai_models.parse_json_object('sure: {"veto": false, "reason": ""} trailing')
    assert ai_models.parse_json_object("no json here") is None
    assert ai_models.parse_json_object(None) is None


# ---------------------------------------------------------------------------
# Veto
# ---------------------------------------------------------------------------
def test_veto_blocks_with_reason():
    async def _run():
        c = _FakeClient(['{"veto": true, "reason": "FOMC in window"}'])
        assert await ai_veto.check_veto(c, _clip()) == "FOMC in window"

    asyncio.run(_run())


def test_veto_approves_and_fails_open():
    async def _run():
        c = _FakeClient(['{"veto": false, "reason": ""}'])
        assert await ai_veto.check_veto(c, _clip()) == ""
        c2 = _FakeClient([None])  # dead stack
        assert await ai_veto.check_veto(c2, _clip()) == ""
        c3 = _FakeClient(["garbage"])
        assert await ai_veto.check_veto(c3, _clip()) == ""

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Sentinel: RSS filter, vol guard, flag file
# ---------------------------------------------------------------------------
_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Bitcoin steady as FOMC decision looms</title><link>x</link><pubDate>{now}</pubDate></item>
<item><title>Bitcoin steady, analysts quiet</title><link>y</link><pubDate>{now}</pubDate></item>
<item><title>Ancient coin moves, CPI due tomorrow</title><link>z</link><pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate></item>
</channel></rss>"""


def test_event_filter_keeps_fresh_keyword_hits_only():
    import email.utils

    now = time.time()
    feed = _RSS.format(now=email.utils.formatdate(now, usegmt=True))
    import urllib.request

    real = urllib.request.urlopen

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return feed.encode()

    urllib.request.urlopen = lambda *a, **k: _Resp()
    try:
        items = sentinel.fetch_rss("http://x")
    finally:
        urllib.request.urlopen = real
    hits = sentinel.fresh_event_items(items, now)
    assert [h["title"] for h in hits] == ["Bitcoin steady as FOMC decision looms"]


def test_vol_guard_halts_and_cautions():
    now = time.time()
    ring = [[now - 300, 85000.0], [now, 85000.0]]
    assert sentinel.ring_move_pct(ring) == 0.0
    ring_hot = [[now - 300, 85000.0], [now, 85900.0]]  # +1.05%
    assert sentinel.classify_state(sentinel.ring_move_pct(ring_hot), None)[0] == "halt"
    ring_warm = [[now - 300, 85000.0], [now, 85600.0]]  # +0.7%
    assert sentinel.classify_state(sentinel.ring_move_pct(ring_warm), None)[0] == "caution"
    assert sentinel.classify_state(0.0, "halt")[0] == "halt"
    assert sentinel.classify_state(0.0, None) == ("trade", "headlines quiet, volatility normal")


def test_flag_roundtrip_and_stale_means_trade(tmp_path):
    path = str(tmp_path / "flag.json")

    async def _run():
        c = _FakeClient(['{"state": "caution", "reason": "r"}'])
        flag = await sentinel.run_once(c, state_path=path, spot_fn=lambda: 85000.0, now=time.time())
        return flag

    flag = asyncio.run(_run())
    assert flag["state"] in ("trade", "caution")
    state, _ = sentinel.read_flag(path)
    assert state == flag["state"]
    # A stale or missing flag is permission, never a halt.
    old_state, _ = sentinel.read_flag(path, now=time.time() + 3600)
    assert old_state == "trade"
    assert sentinel.read_flag(str(tmp_path / "nope.json"))[0] == "trade"


# ---------------------------------------------------------------------------
# Calibrator bounds + replay stats
# ---------------------------------------------------------------------------
def test_calibrator_rejects_out_of_bounds():
    good = {
        "noise_usd": 18.0,
        "min_edge": 0.07,
        "prefer_side": "down",
        "up_override_margin": 0.02,
        "out_of_band_extra_edge": 0.02,
        "rationale": "r",
        "confidence": "medium",
    }
    assert validate_proposal(dict(good))["min_edge"] == 0.07
    bad = dict(good, min_edge=0.50)
    assert validate_proposal(bad) is None
    bad2 = dict(good, prefer_side="sideways")
    assert validate_proposal(bad2) is None
    assert validate_proposal(None) is None
    assert validate_proposal({"noise_usd": 1}) is None


def test_replay_stats_split_kept_and_vetoed():
    closes = [
        {"pnl": 1.0},
        {"pnl": -1.0},
        {"pnl": 1.0},
        {"pnl": -1.0},
    ]
    rep = score_replay(closes, [False, True, False, True])
    assert rep["kept"] == {"n": 2, "win_rate": 1.0, "pnl": 2.0}
    assert rep["vetoed"] == {"n": 2, "win_rate": 0.0, "pnl": -2.0}
