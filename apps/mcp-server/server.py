"""FlowMind MCP server entry point.

Tool registration happens via @mcp.tool() decorators on each module
in apps/mcp-server/tools/. Importing the module is enough to register.
"""
import logging

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level="INFO")

mcp = FastMCP("FlowMind Network Telemetry", json_response=True)

# Side-effect imports: each module registers its tool via @mcp.tool()
from tools import link_utilization, top_talkers  # noqa: E402, F401


if __name__ == "__main__":
    mcp.run()
