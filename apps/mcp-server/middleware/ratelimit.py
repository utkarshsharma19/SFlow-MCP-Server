"""Per-tool sliding-window rate limiter.

In-memory only — good enough for a single MCP process. For multi-process
deployments, swap in a Redis-backed implementation without changing the
decorator surface.
"""
import time
from collections import defaultdict
from functools import wraps

_call_history: dict[str, list[float]] = defaultdict(list)


def rate_limit(max_calls: int = 30, window_seconds: int = 60):
    """Decorator: return a {'error': ...} dict when the caller exceeds the budget.

    Tools should return structured errors rather than raise — the MCP
    client treats exceptions as hard failures, and a soft error is more
    useful to the model.
    """

    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            key = fn.__name__
            now = time.monotonic()
            history = _call_history[key]
            history[:] = [t for t in history if now - t < window_seconds]
            if len(history) >= max_calls:
                return {
                    "error": (
                        f"rate_limit_exceeded: max {max_calls} calls per "
                        f"{window_seconds}s for {key}"
                    ),
                    "retry_after_seconds": int(window_seconds - (now - history[0])),
                }
            history.append(now)
            return await fn(*args, **kwargs)

        return wrapper

    return decorator
