"""Shared pytest configuration.

Safety contract: a plain ``pytest`` run must ALWAYS be safe to execute —
on contributor machines and in CI — with no credentials configured.

Tests marked ``live`` talk to the real Kalshi API (some place and cancel
real orders) and/or spend real LLM credits. They are skipped unless you
explicitly opt in with BOTH the env var and the marker filter:

    RUN_LIVE_TESTS=1 pytest -m live

Never run live tests against an account whose losses you are not
prepared to accept.
"""

import os

import pytest

RUN_LIVE_TESTS = os.getenv("RUN_LIVE_TESTS", "").lower() in {"1", "true", "yes"}


@pytest.fixture(autouse=True)
def _isolate_the_repo_db(tmp_path, monkeypatch):
    """Point every default DB resolution at this test's own temp file.

    Code that resolves the book from the environment (`DB_PATH`, falling
    back to "trading_system.db") was picking up the developer's LOCAL
    database: a stale dry cash figure made funded tests fail on one machine
    and pass in CI, and a test's writes could land in real local state.
    CI runs with no such file; with this fixture a contributor run behaves
    identically. Tests that set DB_PATH themselves still win - their
    monkeypatch applies after this one.
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "trading_system.db"))


def pytest_collection_modifyitems(config, items):
    if RUN_LIVE_TESTS:
        return
    skip_live = pytest.mark.skip(
        reason="live test (real Kalshi API / real money) — set RUN_LIVE_TESTS=1 and pass -m live to run"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
