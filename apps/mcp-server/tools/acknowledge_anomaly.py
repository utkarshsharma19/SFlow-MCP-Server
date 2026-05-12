"""MCP tool: acknowledge_anomaly — operator-side anomaly lifecycle.

Distinct from every other tool in the surface so far: this is a *write*
tool. It requires role >= operator on the underlying API key. Viewers
and analysts get a 403 — and the audit decorator records the attempt
either way (status=error), so the audit trail captures privilege probes.
"""
from app import mcp
from client import post_telemetry
from middleware.audit import audit_tool
from middleware.ratelimit import rate_limit


ALLOWED_ACTIONS = {"acknowledge", "resolve"}


@mcp.tool()
@audit_tool("acknowledge_anomaly")
@rate_limit(max_calls=30, window_seconds=60)
async def acknowledge_anomaly(
    anomaly_id: str,
    action: str = "acknowledge",
) -> dict:
    """Acknowledge or resolve an open anomaly.

    Idempotent on both sides — re-acknowledging an already-acked event
    or resolving an already-resolved one returns a structured error and
    does nothing. Resolving closes the dedup window: the same condition
    recurring afterwards opens a *new* anomaly_event row rather than
    bumping the resolved one.

    The caller's role must be >= operator. Audit trail records who
    performed the action via the calling API key's identifier.

    Args:
        anomaly_id: UUID of the anomaly_event row to act on. Get this
            from ``get_recent_anomalies``.
        action: ``acknowledge`` (default) or ``resolve``.
    """
    if action not in ALLOWED_ACTIONS:
        return {
            "error": f"action must be one of {sorted(ALLOWED_ACTIONS)}",
        }
    if not anomaly_id or not anomaly_id.strip():
        return {"error": "anomaly_id is required"}

    suffix = "acknowledge" if action == "acknowledge" else "resolve"
    return await post_telemetry(f"/anomalies/{anomaly_id}/{suffix}")
