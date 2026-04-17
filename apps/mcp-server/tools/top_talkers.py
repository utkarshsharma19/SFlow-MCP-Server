from app import mcp
from client import get_telemetry
from middleware.ratelimit import rate_limit


@mcp.tool()
@rate_limit(max_calls=30, window_seconds=60)
async def get_top_talkers(
    window_minutes: int = 15,
    scope: str = "global",
    limit: int = 10,
) -> dict:
    """Return the top traffic conversations in the network for a scope and window.

    Results include estimated traffic volume, protocol breakdown, and a
    sampling confidence note. Use this to answer: 'Who is generating the
    most traffic right now?'

    Args:
        window_minutes: Lookback window (1-60 minutes).
        scope: 'global' | 'device:<hostname>' | 'site:<name>'.
        limit: Max number of src/dst pairs to return (1-50).
    """
    if not 1 <= window_minutes <= 60:
        return {"error": "window_minutes must be between 1 and 60"}
    if not 1 <= limit <= 50:
        return {"error": "limit must be between 1 and 50"}

    return await get_telemetry(
        "/flows/top-talkers",
        params={"window_minutes": window_minutes, "scope": scope, "limit": limit},
    )
