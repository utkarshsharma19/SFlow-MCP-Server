"""FlowMind MCP server entry point.

Tool registration happens via @mcp.tool() decorators on each module
in apps/mcp-server/tools/. Importing the module is enough to register.

For HTTP transports we wrap FastMCP's Starlette app with
``TransportAuthMiddleware`` so port 8090 isn't an open gateway to
``TELEMETRY_API_KEY``. Stdio remains unauthenticated because the
identity is the calling process.
"""
import argparse
import logging
import os

from app import mcp
from middleware.transport_auth import TransportAuthMiddleware, get_transport_key
from shared.logging import configure_logging

configure_logging("flowmind-mcp-server", level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

# Side-effect imports: each module registers its tool via @mcp.tool()
# or resource via @mcp.resource()
from resources import inventory  # noqa: E402, F401
from tools import (  # noqa: E402, F401
    acknowledge_anomaly,
    anomaly_summary,
    compare_windows,
    device_neighbors,
    device_state,
    explain_hot_link,
    fabric_health,
    fabric_imbalance,
    find_path,
    intent_diff,
    link_history,
    link_utilization,
    protocol_mix,
    rdma_health,
    recent_anomalies,
    top_offenders,
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

    if args.transport == "stdio":
        mcp.run(args.transport)
        return

    # For HTTP transports, build the app ourselves so we can wrap it
    # with the transport-auth middleware. FastMCP.run() would otherwise
    # construct + serve without giving us an injection point.
    import uvicorn

    if args.transport == "streamable-http":
        app = mcp.streamable_http_app()
    else:
        app = mcp.sse_app()

    key = get_transport_key()
    if key:
        app = TransportAuthMiddleware(app, key=key)
        log.info("MCP transport auth enabled (X-MCP-Key required)")
    else:
        log.warning(
            "MCP_TRANSPORT_KEY unset — transport is open. Set it to "
            "require a per-request MCP key on top of telemetry-API auth."
        )

    uvicorn.run(
        app,
        host=os.getenv("MCP_HOST", "127.0.0.1"),
        port=int(os.getenv("MCP_PORT", "8090")),
        log_level=os.getenv("LOG_LEVEL", "INFO").lower(),
    )


if __name__ == "__main__":
    main()
