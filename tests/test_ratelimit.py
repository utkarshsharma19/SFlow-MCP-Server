"""Rate limiter tests — in-memory + Redis fake (PR 30)."""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER_DIR = PROJECT_ROOT / "apps" / "mcp-server"


def _clear_mcp_modules() -> None:
    for name in list(sys.modules):
        if (
            name in {"app", "client", "server", "middleware", "resources", "tools"}
            or name.startswith("middleware.")
            or name.startswith("resources.")
            or name.startswith("tools.")
        ):
            sys.modules.pop(name)


def _import_ratelimit(monkeypatch):
    _clear_mcp_modules()
    monkeypatch.syspath_prepend(str(MCP_SERVER_DIR))
    monkeypatch.delenv("REDIS_URL", raising=False)
    return importlib.import_module("middleware.ratelimit")


def _run(coro):
    return asyncio.run(coro)


def test_memory_check_allows_under_budget(monkeypatch):
    rl = _import_ratelimit(monkeypatch)
    rl._reset_for_tests()
    allowed, retry = rl._check_memory("k", max_calls=3, window_seconds=60)
    assert allowed is True
    assert retry == 0


def test_memory_check_blocks_over_budget(monkeypatch):
    rl = _import_ratelimit(monkeypatch)
    rl._reset_for_tests()
    for _ in range(3):
        rl._check_memory("k", max_calls=3, window_seconds=60)
    allowed, retry = rl._check_memory("k", max_calls=3, window_seconds=60)
    assert allowed is False
    assert retry >= 1


def test_decorator_returns_error_dict_when_over_budget(monkeypatch):
    rl = _import_ratelimit(monkeypatch)
    rl._reset_for_tests()

    @rl.rate_limit(max_calls=2, window_seconds=60)
    async def tool():
        return {"ok": True}

    assert _run(tool()) == {"ok": True}
    assert _run(tool()) == {"ok": True}
    third = _run(tool())
    assert "error" in third
    assert "rate_limit_exceeded" in third["error"]
    assert third["retry_after_seconds"] >= 1


def test_redis_check_falls_back_on_failure(monkeypatch):
    rl = _import_ratelimit(monkeypatch)
    rl._reset_for_tests()

    class _BrokenPipeline:
        def zremrangebyscore(self, *a, **k):
            return self

        def zcard(self, *a, **k):
            return self

        async def execute(self):
            raise RuntimeError("boom")

    class _BrokenRedis:
        def pipeline(self, transaction=False):
            return _BrokenPipeline()

    out = _run(
        rl._check_redis(_BrokenRedis(), "k", max_calls=5, window_seconds=60, now_ms=0)
    )
    # On any Redis exception we fall back to in-memory rather than failing closed.
    assert out[0] is True


def test_redis_check_blocks_when_at_limit(monkeypatch):
    rl = _import_ratelimit(monkeypatch)
    rl._reset_for_tests()
    now_ms = 1_700_000_000_000

    class _Pipeline:
        def __init__(self, ops):
            self._ops = ops
            self._calls = []

        def zremrangebyscore(self, *a, **k):
            self._calls.append("zrem")
            return self

        def zcard(self, *a, **k):
            self._calls.append("zcard")
            return self

        def zadd(self, *a, **k):
            self._calls.append("zadd")
            return self

        def expire(self, *a, **k):
            self._calls.append("expire")
            return self

        async def execute(self):
            return self._ops

    class _Redis:
        def pipeline(self, transaction=False):
            # First pipeline returns (rem_count, current_count=5)
            return _Pipeline([0, 5])

        async def zrange(self, *a, **k):
            # Oldest score is 10s old; window is 60s → retry_after ≈ 50s
            return [(b"member", now_ms - 10_000)]

    allowed, retry = _run(
        rl._check_redis(_Redis(), "k", max_calls=5, window_seconds=60, now_ms=now_ms)
    )
    assert allowed is False
    assert 40 <= retry <= 60
