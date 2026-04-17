from client import get_telemetry
from middleware.ratelimit import rate_limit
from server import mcp


@mcp.tool()
@rate_limit(max_calls=30, window_seconds=60)
async def summarize_protocol_mix(
    scope: str = "global",
    window_minutes: int = 15,
) -> dict:
    """Return the protocol share breakdown for a scope and window.

    Use this to answer: 'What protocols are running right now?' Response
    is ordered by byte share with a plain-language headline.

    Args:
        scope: 'global' or 'device:<hostname>'.
        window_minutes: Lookback window (1-60 minutes).
    """
    if not 1 <= window_minutes <= 60:
        return {"error": "window_minutes must be between 1 and 60"}
    return await get_telemetry(
        "/flows/protocol-mix",
        params={"scope": scope, "window_minutes": window_minutes},
    )
