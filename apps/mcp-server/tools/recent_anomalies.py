from client import get_telemetry
from middleware.ratelimit import rate_limit
from server import mcp


@mcp.tool()
@rate_limit(max_calls=30, window_seconds=60)
async def get_recent_anomalies(
    scope: str = "global",
    severity_min: str = "medium",
    since_minutes: int = 30,
) -> dict:
    """Return anomaly events detected recently for a scope.

    Filters by minimum severity (low|medium|high|critical). Use this to
    answer: 'Is something wrong right now?'

    Args:
        scope: 'global' or 'device:<hostname>' or 'interface:<dev>/<if>'.
        severity_min: Minimum severity to include.
        since_minutes: Lookback window (1-1440 minutes).
    """
    if severity_min not in {"low", "medium", "high", "critical"}:
        return {"error": "severity_min must be low|medium|high|critical"}
    if not 1 <= since_minutes <= 1440:
        return {"error": "since_minutes must be between 1 and 1440"}
    return await get_telemetry(
        "/anomalies/recent",
        params={
            "scope": scope,
            "severity_min": severity_min,
            "since_minutes": since_minutes,
        },
    )
