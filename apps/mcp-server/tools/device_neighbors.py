"""MCP tool: get_device_neighbors — LLDP-derived adjacency for one device."""
from app import mcp
from client import get_telemetry
from middleware.audit import audit_tool
from middleware.ratelimit import rate_limit


@mcp.tool()
@audit_tool("get_device_neighbors")
@rate_limit(max_calls=30, window_seconds=60)
async def get_device_neighbors(device: str) -> dict:
    """Return every LLDP neighbor advertised by a device.

    Each entry carries the local interface, the remote chassis_id /
    system name, and the remote port description. ``is_stale=true``
    flags neighbors that haven't refreshed in the last 24h — a cable
    may have been pulled. Use this to answer 'what's plugged into
    leaf1?' and 'who is leaf1 adjacent to?' without inferring topology
    from flow data.

    Args:
        device: Device hostname.
    """
    if not device or not device.strip():
        return {"error": "device is required"}
    return await get_telemetry(
        "/devices/neighbors",
        params={"device": device},
    )
