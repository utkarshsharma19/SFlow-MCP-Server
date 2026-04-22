"""MCP tool: detect_fabric_imbalance — ECMP balance check."""
from app import mcp
from client import get_telemetry
from middleware.audit import audit_tool
from middleware.ratelimit import rate_limit


@mcp.tool()
@audit_tool("detect_fabric_imbalance")
@rate_limit(max_calls=20, window_seconds=60)
async def detect_fabric_imbalance(
    device: str | None = None,
    window_minutes: int = 15,
) -> dict:
    """Detect ECMP / spine-leaf path imbalance.

    For each ECMP group on a device — configured or speed-inferred —
    return per-member utilization, coefficient of variation, and a
    flag when one member is more than 1.5x the group mean. Use this
    to answer: 'Is one of my uplinks doing all the work?'

    Args:
        device: Optional device hostname. If omitted, every device with
            recent utilization data is analyzed.
        window_minutes: Lookback window (1-60 minutes).
    """
    if not 1 <= window_minutes <= 60:
        return {"error": "window_minutes must be between 1 and 60"}

    params: dict = {"window_minutes": window_minutes}
    if device is not None:
        params["device"] = device

    return await get_telemetry("/fabric/imbalance", params=params)
