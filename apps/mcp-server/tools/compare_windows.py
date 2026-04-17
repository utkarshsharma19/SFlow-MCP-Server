from app import mcp
from client import get_telemetry
from middleware.ratelimit import rate_limit


@mcp.tool()
@rate_limit(max_calls=10, window_seconds=60)
async def compare_traffic_windows(
    scope: str = "global",
    baseline_start: str = "",
    baseline_end: str = "",
    current_start: str = "",
    current_end: str = "",
) -> dict:
    """Compare traffic volume between two time windows.

    Returns baseline vs current bytes, delta percentage, and direction.
    Use this to answer: 'Did traffic change vs an hour ago / yesterday?'

    Args:
        scope: 'global' or 'device:<hostname>'.
        baseline_start/end: Reference window as ISO 8601 strings.
        current_start/end: Current window as ISO 8601 strings.
    """
    missing = [
        n for n, v in [
            ("baseline_start", baseline_start),
            ("baseline_end", baseline_end),
            ("current_start", current_start),
            ("current_end", current_end),
        ] if not v
    ]
    if missing:
        return {"error": f"missing required ISO 8601 timestamps: {', '.join(missing)}"}
    return await get_telemetry(
        "/traffic/compare",
        params={
            "scope": scope,
            "baseline_start": baseline_start,
            "baseline_end": baseline_end,
            "current_start": current_start,
            "current_end": current_end,
        },
    )
