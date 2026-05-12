"""MCP tool: diff_config_intent_vs_state — drift between intent and reality."""
from app import mcp
from client import get_telemetry
from middleware.audit import audit_tool
from middleware.ratelimit import rate_limit


@mcp.tool()
@audit_tool("diff_config_intent_vs_state")
@rate_limit(max_calls=20, window_seconds=60)
async def diff_config_intent_vs_state(device: str | None = None) -> dict:
    """Report where the network's *intent* disagrees with its *state*.

    Compares the cached operator/Verity intent (admin_status, oper_status,
    speed, MTU, description per interface; expected peer_as and
    session_state per BGP peer) against the latest gNMI/OpenConfig
    observation. Returns three classes of finding — ``mismatch``,
    ``missing_state``, and ``unexpected_state`` — plus a severity band
    derived from how badly the fabric disagrees with what was declared.
    Use this to answer: 'Is the network running the config the operator
    asked for?' Pair with ``get_device_state`` for the raw observation
    and ``get_fabric_health`` for the rolled-up view.

    Args:
        device: Optional device hostname to scope the diff. If omitted,
            every device with either intent or recent state in the tenant
            is checked.
    """
    params: dict = {}
    if device is not None:
        if not device.strip():
            return {"error": "device, if provided, must be non-empty"}
        params["device"] = device
    return await get_telemetry("/intent/diff", params=params)
