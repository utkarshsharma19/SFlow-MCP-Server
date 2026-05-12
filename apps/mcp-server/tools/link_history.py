"""MCP tool: get_link_history — drill-down timeline for one interface."""
from app import mcp
from client import get_telemetry
from middleware.audit import audit_tool
from middleware.ratelimit import rate_limit


@mcp.tool()
@audit_tool("get_link_history")
@rate_limit(max_calls=30, window_seconds=60)
async def get_link_history(
    device: str,
    interface: str,
    window_minutes: int = 60,
    bucket_minutes: int = 5,
) -> dict:
    """Return a per-bucket timeline of utilization, errors, flaps, and anomalies.

    This is the drill-down primitive: call it *after* ``get_fabric_health``
    or ``get_recent_anomalies`` points at a specific link. The response
    has four sections an LLM can quote directly:

    - ``series``: time-bucketed avg/max util + error counts per bin.
    - ``threshold_breaches``: buckets where peak util exceeded the
      service-level threshold (80%).
    - ``flaps``: link transitions inside the window, derived from gNMI
      ``last_change`` rather than counter blips — fewer false positives.
    - ``open_anomalies``: scoped anomaly events tagged to this link or
      the parent device, so the model can cross-reference correlations.

    Args:
        device: Device hostname.
        interface: Interface name (e.g. Ethernet0/1).
        window_minutes: Lookback window (1-1440). Default 60.
        bucket_minutes: Downsampling granularity (1-60). Default 5.
    """
    if not device or not interface:
        return {"error": "device and interface are required"}
    if not 1 <= window_minutes <= 1440:
        return {"error": "window_minutes must be between 1 and 1440"}
    if not 1 <= bucket_minutes <= 60:
        return {"error": "bucket_minutes must be between 1 and 60"}

    return await get_telemetry(
        "/interfaces/history",
        params={
            "device": device,
            "interface": interface,
            "window_minutes": window_minutes,
            "bucket_minutes": bucket_minutes,
        },
    )
