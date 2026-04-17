"""Shared FastMCP application instance."""

import os

from mcp.server.fastmcp import FastMCP


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


mcp = FastMCP(
    "FlowMind Network Telemetry",
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=_env_int("MCP_PORT", 8000),
    json_response=True,
)
