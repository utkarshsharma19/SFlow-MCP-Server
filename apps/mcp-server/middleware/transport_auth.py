"""Transport-layer auth for the MCP streamable-HTTP / SSE surfaces (PR 31).

The telemetry-API behind us already enforces tenant + role via the
``X-API-Key`` header — but that key lives in the MCP server's *own*
environment as ``TELEMETRY_API_KEY``. Anyone who reaches port 8090
calls any tool with that identity, no matter where they came from.
That's fine for stdio (the calling process is the identity) and fine
on a private network, but a real network exposure needs a separate
front-door key on the MCP itself.

This middleware is a tiny ASGI shim. When ``MCP_TRANSPORT_KEY`` is set
in the environment, every incoming HTTP request must carry the same
key in ``X-MCP-Key`` (or query string ``mcp_key=...`` as a fallback for
clients that can't add headers). When the env is unset the middleware
is wired but no-ops — keeping the dev-mode "just works" path unchanged.

We intentionally do not introspect the JSON-RPC body. The MCP protocol
multiplexes tool calls inside a long-lived streamable HTTP request;
parsing the body to authorize per-tool would add latency and a parser
that would drift from the MCP spec. Transport-level + telemetry-API
auth is the right layering.

The middleware is *not* applied to ``/health`` or ``/metrics`` because
the MCP server doesn't expose those today — and won't, since the
telemetry-API owns metrics. If we add them later, exempt them here.
"""
from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable

from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

KEY_HEADER = "x-mcp-key"
KEY_QUERY_PARAM = "mcp_key"
ENV_VAR = "MCP_TRANSPORT_KEY"


class TransportAuthMiddleware:
    """ASGI middleware. Rejects HTTP requests missing a valid MCP key.

    Pure ASGI — no Starlette ``BaseHTTPMiddleware``. The FastMCP
    streamable-HTTP transport keeps the request alive while the client
    streams MCP frames; ``BaseHTTPMiddleware`` materializes the request
    body, which would defeat streaming. Pure ASGI is the contract that
    leaves streaming intact.
    """

    def __init__(self, app: ASGIApp, *, key: str | None = None) -> None:
        self.app = app
        self.expected_key = key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # No key configured → middleware no-ops. Operators get the
        # original dev-mode behavior; production deploys set the env.
        if not self.expected_key:
            await self.app(scope, receive, send)
            return

        if _is_authorized(scope, self.expected_key):
            await self.app(scope, receive, send)
            return

        await _reject_unauthorized(scope, send)


def _is_authorized(scope: Scope, expected_key: str) -> bool:
    """Header first, query-string fallback."""
    headers = {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in scope.get("headers", [])
    }
    if headers.get(KEY_HEADER) == expected_key:
        return True

    raw_qs = scope.get("query_string", b"")
    if not raw_qs:
        return False
    # Parse query string by hand rather than urllib.parse_qs because
    # we only need one key and want to avoid surprise unquoting differences.
    for pair in raw_qs.split(b"&"):
        if b"=" not in pair:
            continue
        k, _, v = pair.partition(b"=")
        if k.decode("latin-1") == KEY_QUERY_PARAM and v.decode("latin-1") == expected_key:
            return True
    return False


async def _reject_unauthorized(scope: Scope, send: Send) -> None:
    body = b'{"error":"unauthorized","detail":"missing or invalid X-MCP-Key"}'
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        # Hint legitimate clients which header to send next time.
        (b"www-authenticate", b'MCPKey realm="flowmind"'),
    ]
    await send(
        {"type": "http.response.start", "status": 401, "headers": headers}
    )
    await send({"type": "http.response.body", "body": body})


def get_transport_key() -> str | None:
    """Read the configured key once at startup; ``None`` disables auth."""
    return os.getenv(ENV_VAR) or None
