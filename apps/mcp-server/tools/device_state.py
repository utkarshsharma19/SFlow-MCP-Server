"""MCP tool: get_device_state — gNMI/OpenConfig snapshot per device."""
from app import mcp
from client import get_telemetry
from middleware.audit import audit_tool
from middleware.ratelimit import rate_limit


@mcp.tool()
@audit_tool("get_device_state")
@rate_limit(max_calls=30, window_seconds=60)
async def get_device_state(device: str, window_minutes: int = 15) -> dict:
    """Return the latest interface, BGP, and queue state for a device.

    Sourced from gNMI / OpenConfig — exact (non-sampled) telemetry. Use
    this to answer: 'Is the device healthy? Are any interfaces flapping?
    Are BGP peers up? Is any queue running deep?' Useful as a first
    diagnosis step before drilling into sFlow flow data.

    Args:
        device: Device hostname.
        window_minutes: Lookback window for state samples (1-60).
    """
    if not 1 <= window_minutes <= 60:
        return {"error": "window_minutes must be between 1 and 60"}
    if not device:
        return {"error": "device is required"}

    return await get_telemetry(
        "/devices/state",
        params={"device": device, "window_minutes": window_minutes},
    )
