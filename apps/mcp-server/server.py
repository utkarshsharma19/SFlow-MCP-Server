"""FlowMind MCP server — stub. Tools registered in later PRs."""
import logging

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level="INFO")

mcp = FastMCP("FlowMind Network Telemetry", json_response=True)


if __name__ == "__main__":
    mcp.run()
