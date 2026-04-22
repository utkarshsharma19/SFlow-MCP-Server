"""FlowMind MCP server entry point.

Tool registration happens via @mcp.tool() decorators on each module
in apps/mcp-server/tools/. Importing the module is enough to register.
"""
import argparse
import logging

from app import mcp

logging.basicConfig(level="INFO")

# Side-effect imports: each module registers its tool via @mcp.tool()
# or resource via @mcp.resource()
from resources import inventory  # noqa: E402, F401
from tools import (  # noqa: E402, F401
    anomaly_summary,
    compare_windows,
    device_state,
    explain_hot_link,
    fabric_imbalance,
    link_utilization,
    protocol_mix,
    rdma_health,
    recent_anomalies,
    top_talkers,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the FlowMind MCP server.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
        help="MCP transport to run. Use stdio for local MCP clients.",
    )
    args = parser.parse_args()
    mcp.run(args.transport)


if __name__ == "__main__":
    main()
