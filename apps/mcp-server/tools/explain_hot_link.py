from client import get_telemetry
from middleware.ratelimit import rate_limit
from server import mcp


@mcp.tool()
@rate_limit(max_calls=10, window_seconds=60)
async def explain_hot_link(
    device: str,
    interface: str,
    window_minutes: int = 15,
) -> dict:
    """Explain why a specific network interface is heavily utilized.

    Returns peak utilization, top conversations on the link, and a short
    narrative. Use this to answer: 'Why is this link saturated? Who is
    causing it?'

    Args:
        device: Device hostname.
        interface: Interface name (e.g. 'Ethernet0/1').
        window_minutes: Lookback window (1-30 minutes).
    """
    if not 1 <= window_minutes <= 30:
        return {"error": "window_minutes must be between 1 and 30 for explain_hot_link"}
    return await get_telemetry(
        "/flows/explain-link",
        params={
            "device": device,
            "interface": interface,
            "window_minutes": window_minutes,
        },
    )
