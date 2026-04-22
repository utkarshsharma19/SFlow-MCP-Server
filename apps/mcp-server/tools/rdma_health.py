"""MCP tool: get_rdma_health — RoCE/RDMA fabric assessment."""
from app import mcp
from client import get_telemetry
from middleware.audit import audit_tool
from middleware.ratelimit import rate_limit


@mcp.tool()
@audit_tool("get_rdma_health")
@rate_limit(max_calls=20, window_seconds=60)
async def get_rdma_health(
    device: str | None = None,
    window_minutes: int = 15,
) -> dict:
    """Assess RoCE / RDMA fabric health for an AI / GPU cluster.

    Combines flow-side RoCE share (UDP traffic estimate) with gNMI
    queue-side PFC pause, ECN marking, and drop counters. Returns a
    severity (low|medium|high|critical) and plain-language drivers
    suitable for an LLM to act on. Use this to answer:
        'Is my training fabric healthy? What's congested?'

    Args:
        device: Optional device hostname to scope queue analysis. If
            omitted, queue stats are aggregated across the whole tenant.
        window_minutes: Lookback window (1-60 minutes).
    """
    if not 1 <= window_minutes <= 60:
        return {"error": "window_minutes must be between 1 and 60"}

    params: dict = {"window_minutes": window_minutes}
    if device is not None:
        params["device"] = device

    return await get_telemetry("/rdma/health", params=params)
