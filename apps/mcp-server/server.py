"""FlowMind MCP server entry point.

Tool registration happens via @mcp.tool() decorators on each module
in apps/mcp-server/tools/. Importing the module is enough to register.
"""
import logging

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level="INFO")

mcp = FastMCP("FlowMind Network Telemetry", json_response=True)

# Side-effect imports: each module registers its tool via @mcp.tool()
# or resource via @mcp.resource()
from resources import inventory  # noqa: E402, F401
from tools import (  # noqa: E402, F401
    compare_windows,
    explain_hot_link,
    link_utilization,
    protocol_mix,
    recent_anomalies,
    top_talkers,
)


if __name__ == "__main__":
    mcp.run()
