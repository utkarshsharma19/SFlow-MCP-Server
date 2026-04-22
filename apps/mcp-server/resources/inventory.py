"""MCP resources for slow-changing inventory data.

Resources differ from tools: they carry reference data the model reads
for context, not analytics results. MCP clients may cache these, so
never use resources for live metrics — use tools instead.
"""
from app import mcp
from client import get_telemetry


@mcp.resource("inventory://devices")
async def device_inventory() -> dict:
    """Network device inventory derived from recent telemetry.

    Hostnames, interface counts, last-seen timestamps. Use this to
    understand what devices exist before calling other tools.
    """
    return await get_telemetry("/topology/devices")


@mcp.resource("inventory://interfaces")
async def interface_inventory() -> dict:
    """Interface inventory: (device, interface) pairs observed recently.

    Includes peak utilization and source (counters vs flows-only). Use
    this to resolve interface names before calling get_interface_utilization
    or explain_hot_link.
    """
    return await get_telemetry("/topology/interfaces")


@mcp.resource("inventory://gnmi-sources")
async def gnmi_source_inventory() -> dict:
    """Devices with active gNMI / OpenConfig telemetry in the last 24 hours.

    Use this to discover which devices support get_device_state before
    calling it. Empty list means no gNMI targets are configured or the
    pygnmi extra is not installed.
    """
    return await get_telemetry("/devices/gnmi-sources")
