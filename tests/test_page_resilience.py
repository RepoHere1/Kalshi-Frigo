"""The page must never blank or hang, in either book.

Measured on the live service while the dashboard was showing zeros:

- `/api/snapshot` returned **500 with an empty body**. The client had nothing to
  render, so every figure on the page fell back to zero. That reads as "the book
  blanked to zero"; the book was untouched.
- `/api/strategies` took **27 seconds** - the 30s busy timeout nearly expiring
  on a blocked read. Buttons look dead and clicks look ignored while that is in
  flight, so strategies appear not to turn on.

Neither is a trading fault. Both are the page failing to stay up while the books
are fine, which is indistinguishable from the books being broken.
"""
import pytest

import web_dashboard as wd
from src.utils.database import BUSY_TIMEOUT_SECONDS, READ_BUSY_TIMEOUT_SECONDS


def test_a_read_never_waits_as_long_as_a_write():
    """27-second page loads are the symptom; this is the cause."""
    assert READ_BUSY_TIMEOUT_SECONDS < BUSY_TIMEOUT_SECONDS
    assert READ_BUSY_TIMEOUT_SECONDS <= 5


def test_writes_keep_the_long_timeout():
    """A write that gives up early loses work; a read that does not."""
    assert BUSY_TIMEOUT_SECONDS >= 15


def test_the_snapshot_caches_for_a_short_window():
    """A burst of polls should cost one query, not six."""
    assert 0 < wd._SNAPSHOT_TTL <= 5


def test_the_snapshot_is_memoised(monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(wd, "_SNAPSHOT_CACHE", {"at": 0.0, "payload": None})
    wd.app.config["TESTING"] = False
    calls = []

    real = wd.build_snapshot

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(wd, "build_snapshot", counting)
    try:
        a = wd.build_snapshot_cached()
        b = wd.build_snapshot_cached()
        assert len(calls) == 1, "the second read rebuilt instead of reusing"
        assert a is b
    finally:
        wd.app.config["TESTING"] = True


def test_a_failed_rebuild_serves_the_last_good_payload(monkeypatch, tmp_path):
    """A blank page is worse than a slightly stale one."""
    monkeypatch.setattr(wd, "DB_PATH", str(tmp_path / "d.db"))
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path / "logs")
    good = {"mode": {"mode": "dry"}, "positions": [{"market_id": "KX"}]}
    monkeypatch.setattr(wd, "_SNAPSHOT_CACHE", {"at": 0.0, "payload": good})
    monkeypatch.setattr(wd.app.config, "get", lambda *a, **k: False, raising=False)
    wd.app.config["TESTING"] = False

    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(wd, "build_snapshot", boom)
    try:
        assert wd.build_snapshot_cached() is good
    finally:
        wd.app.config["TESTING"] = True


def test_a_failed_rebuild_with_no_cache_still_returns_a_shape():
    """Even cold, the client must get something renderable."""
    monkeypatch_calls = []

    def boom():
        monkeypatch_calls.append(1)
        raise RuntimeError("database is locked")

    original = wd.build_snapshot
    wd.build_snapshot = boom
    wd._SNAPSHOT_CACHE["payload"] = None
    wd.app.config["TESTING"] = False
    try:
        payload = wd.build_snapshot_cached()
    finally:
        wd.build_snapshot = original
        wd.app.config["TESTING"] = True

    for key in ("mode", "positions", "strategy_cards", "trades", "open"):
        assert key in payload, key
