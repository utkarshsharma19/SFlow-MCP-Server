from app import mcp
from client import get_telemetry
from middleware.ratelimit import rate_limit


@mcp.tool()
@rate_limit(max_calls=30, window_seconds=60)
async def get_interface_utilization(
    device: str,
    interface: str,
    window_minutes: int = 15,
) -> dict:
    """Return utilization metrics for a specific network interface.

    Includes avg/max in and out utilization, trend direction, and any
    threshold breaches. Use this to answer: 'How loaded is this link?
    Is it trending worse?'

    Args:
        device: Device hostname (e.g. 'core-sw-01').
        interface: Interface name (e.g. 'Ethernet0/1').
        window_minutes: Lookback window (1-60 minutes).
    """
    if not 1 <= window_minutes <= 60:
        return {"error": "window_minutes must be between 1 and 60"}

    return await get_telemetry(
        "/interfaces/utilization",
        params={
            "device": device,
            "interface": interface,
            "window_minutes": window_minutes,
        },
    )
