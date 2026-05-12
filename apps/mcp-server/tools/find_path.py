"""MCP tool: find_path — per-hop trace of a src→dst flow."""
from app import mcp
from client import get_telemetry
from middleware.audit import audit_tool
from middleware.ratelimit import rate_limit


@mcp.tool()
@audit_tool("find_path")
@rate_limit(max_calls=20, window_seconds=60)
async def find_path(
    src_ip: str,
    dst_ip: str,
    window_minutes: int = 30,
) -> dict:
    """Find the set of switches a src→dst flow traversed in the window.

    Every switch that sampled the flow appears as a hop with its
    ingress interface, traffic volume, peak link utilization, and
    BGP peering health. Use this to answer 'why is A → B slow?' —
    the LLM should look at the highest-util hop first.

    Today the hops are ordered by traffic volume, not topology — the
    response sets ``ordered=false`` so the chatbot can be explicit
    about that. Once LLDP-driven adjacency is populated, ordering
    will follow the graph.

    Args:
        src_ip: Source IP (literal address, not hostname).
        dst_ip: Destination IP.
        window_minutes: Lookback window (1-120). Default 30.
    """
    if not src_ip or not dst_ip:
        return {"error": "src_ip and dst_ip are required"}
    if not 1 <= window_minutes <= 120:
        return {"error": "window_minutes must be between 1 and 120"}
    return await get_telemetry(
        "/flows/path",
        params={
            "src_ip": src_ip,
            "dst_ip": dst_ip,
            "window_minutes": window_minutes,
        },
    )
