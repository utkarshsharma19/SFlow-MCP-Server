"""MCP tool: get_top_offenders — what to look at first this morning."""
from app import mcp
from client import get_telemetry
from middleware.audit import audit_tool
from middleware.ratelimit import rate_limit


@mcp.tool()
@audit_tool("get_top_offenders")
@rate_limit(max_calls=30, window_seconds=60)
async def get_top_offenders(
    scope: str = "device",
    window_minutes: int = 60,
    limit: int = 10,
) -> dict:
    """Rank the noisiest devices/interfaces over a window.

    Composite score blends severity-weighted open anomalies, flap count
    from gNMI last_change deltas, summed interface errors, and the
    number of minute buckets above 80% peak utilization. Each
    component is normalized to its own max so a flood of low-severity
    anomalies doesn't drown a single critical fabric event.

    Use this as a standup tool: "what should I look at first?". Then
    drill in with ``get_link_history`` for one of the returned entries.

    Args:
        scope: "device" (default) groups by hostname; "interface"
            groups by (device, interface).
        window_minutes: Lookback (1-1440). Default 60.
        limit: Max offenders to return (1-50). Default 10.
    """
    if scope not in {"device", "interface"}:
        return {"error": "scope must be 'device' or 'interface'"}
    if not 1 <= window_minutes <= 1440:
        return {"error": "window_minutes must be between 1 and 1440"}
    if not 1 <= limit <= 50:
        return {"error": "limit must be between 1 and 50"}
    return await get_telemetry(
        "/topology/top-offenders",
        params={
            "scope": scope,
            "window_minutes": window_minutes,
            "limit": limit,
        },
    )
