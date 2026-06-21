"""MCP tool: get_fabric_health — single rolled-up fabric health score."""
from app import mcp
from client import get_telemetry
from middleware.audit import audit_tool
from middleware.ratelimit import rate_limit


@mcp.tool()
@audit_tool("get_fabric_health")
@rate_limit(max_calls=30, window_seconds=60)
async def get_fabric_health(window_minutes: int = 15) -> dict:
    """Return a rolled-up fabric health score with per-component drivers.

    Blends four signals into one decision-ready answer:
    link utilization, BGP session state, queue/PFC/ECN, and collector
    freshness. Use this as the *first* tool the chatbot calls when an
    operator asks 'how's the network?' — then drill into the specific
    component tools (``detect_fabric_imbalance``, ``get_rdma_health``,
    ``get_recent_anomalies``) based on which driver is firing.

    Args:
        window_minutes: Lookback window for the score (1-60 minutes).
    """
    if not 1 <= window_minutes <= 60:
        return {"error": "window_minutes must be between 1 and 60"}
    return await get_telemetry(
        "/fabric/health",
        params={"window_minutes": window_minutes},
    )
