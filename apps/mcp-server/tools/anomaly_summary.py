"""MCP tool: summarize_anomalies — LLM narrative over recent anomalies (PR 25)."""
from app import mcp
from client import get_telemetry
from middleware.audit import audit_tool
from middleware.ratelimit import rate_limit


@mcp.tool()
@audit_tool("summarize_anomalies")
@rate_limit(max_calls=10, window_seconds=60)
async def summarize_anomalies(
    scope: str = "global",
    severity_min: str = "medium",
    since_minutes: int = 30,
) -> dict:
    """Short narrative over recent anomalies for a shift handoff.

    Collapses the last anomalies into a <=3-sentence summary grounded in
    counts, severities, and types — nothing is invented beyond the stored
    events. Pair with ``get_recent_anomalies`` when the operator wants the
    raw list.

    Args:
        scope: 'global' or 'device:<hostname>' or 'interface:<dev>/<if>'.
        severity_min: Minimum severity to include (low|medium|high|critical).
        since_minutes: Lookback window (1-1440 minutes).
    """
    if severity_min not in {"low", "medium", "high", "critical"}:
        return {"error": "severity_min must be low|medium|high|critical"}
    if not 1 <= since_minutes <= 1440:
        return {"error": "since_minutes must be between 1 and 1440"}
    return await get_telemetry(
        "/anomalies/summary",
        params={
            "scope": scope,
            "severity_min": severity_min,
            "since_minutes": since_minutes,
        },
    )
