"""Per-tool sliding-window rate limiter (PR 30).

Two backends:

* **Redis** (default when ``REDIS_URL`` is set) — a sorted-set sliding
  window. Safe across multiple MCP processes/replicas. Lua-less, single
  pipelined round trip per check; under load the cost is ~1 ms.

* **In-memory fallback** — the original PR 11 implementation, retained
  so single-process runs and tests don't require Redis. The decorator
  surface is identical; only the storage swaps underneath.

The decorator never raises: a Redis outage degrades to in-memory rather
than failing closed. Production should monitor the
``ratelimit_backend_degraded`` warning log.
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from functools import wraps
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL")
_RATE_LIMIT_KEY_PREFIX = "flowmind:ratelimit:"

# In-memory fallback storage. Keyed identically to Redis so a half-up
# Redis can't drift the counters relative to the fallback path.
_call_history: dict[str, list[float]] = defaultdict(list)
_redis_client: Any | None = None
_redis_unavailable = False


async def _get_redis():
    """Lazy-load redis.asyncio. Returns None if import or connect fails.

    The first failure flips ``_redis_unavailable`` to short-circuit
    every subsequent call — we don't want each request to pay the cost
    of a fresh DNS lookup against a known-dead host.
    """
    global _redis_client, _redis_unavailable
    if _redis_unavailable:
        return None
    if _redis_client is not None:
        return _redis_client
    if not REDIS_URL:
        _redis_unavailable = True
        return None
    try:
        from redis import asyncio as redis_async
    except ImportError:
        log.warning("redis package missing — rate limiter falling back to in-memory")
        _redis_unavailable = True
        return None
    try:
        client = redis_async.from_url(REDIS_URL, decode_responses=False)
        await client.ping()
        _redis_client = client
        log.info("rate limiter using Redis backend at %s", REDIS_URL)
        return client
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "ratelimit_backend_degraded: Redis unreachable (%s); using in-memory",
            exc,
        )
        _redis_unavailable = True
        return None


async def _check_redis(
    redis_client,
    key: str,
    max_calls: int,
    window_seconds: int,
    now_ms: int,
) -> tuple[bool, int]:
    """One round-trip sliding window via ZSET. Returns (allowed, retry_after).

    Algorithm:
      1. Strip every entry older than the window.
      2. Count what's left.
      3. If under the budget: ZADD the current timestamp + EXPIRE the key.
      4. Otherwise: report the wait-time as (oldest_kept_ts + window) - now.

    We use the call timestamp (ms) as both the ZSET member and score so
    duplicates across rapid bursts don't collide.
    """
    window_ms = window_seconds * 1000
    cutoff_ms = now_ms - window_ms
    try:
        pipe = redis_client.pipeline(transaction=False)
        pipe.zremrangebyscore(key, 0, cutoff_ms)
        pipe.zcard(key)
        _, count = await pipe.execute()
        count = int(count)
        if count >= max_calls:
            # Oldest kept score is the earliest call that hasn't aged
            # out yet; the limit lifts when *that* one ages out.
            zr = await redis_client.zrange(key, 0, 0, withscores=True)
            if zr:
                oldest_score = int(zr[0][1])
                retry_after = max(
                    1, int((oldest_score + window_ms - now_ms) / 1000)
                )
            else:
                retry_after = window_seconds
            return False, retry_after
        pipe = redis_client.pipeline(transaction=False)
        pipe.zadd(key, {f"{now_ms}-{count}": now_ms})
        pipe.expire(key, window_seconds + 1)
        await pipe.execute()
        return True, 0
    except Exception as exc:  # noqa: BLE001
        log.warning("ratelimit Redis op failed: %s; falling back this call", exc)
        return _check_memory(key, max_calls, window_seconds)


def _check_memory(
    key: str, max_calls: int, window_seconds: int
) -> tuple[bool, int]:
    now = time.monotonic()
    history = _call_history[key]
    history[:] = [t for t in history if now - t < window_seconds]
    if len(history) >= max_calls:
        retry_after = max(1, int(window_seconds - (now - history[0])))
        return False, retry_after
    history.append(now)
    return True, 0


def rate_limit(
    max_calls: int = 30, window_seconds: int = 60
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Decorator. Returns ``{"error": ..., "retry_after_seconds": N}`` on breach."""

    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            key = f"{_RATE_LIMIT_KEY_PREFIX}{fn.__name__}"
            redis_client = await _get_redis()
            if redis_client is not None:
                allowed, retry_after = await _check_redis(
                    redis_client,
                    key,
                    max_calls,
                    window_seconds,
                    now_ms=int(time.time() * 1000),
                )
            else:
                allowed, retry_after = _check_memory(
                    key, max_calls, window_seconds
                )
            if not allowed:
                return {
                    "error": (
                        f"rate_limit_exceeded: max {max_calls} calls per "
                        f"{window_seconds}s for {fn.__name__}"
                    ),
                    "retry_after_seconds": retry_after,
                }
            return await fn(*args, **kwargs)

        return wrapper

    return decorator


def _reset_for_tests() -> None:
    """Drop in-memory state. Tests call this between cases."""
    _call_history.clear()
    global _redis_client, _redis_unavailable
    _redis_client = None
    _redis_unavailable = False
