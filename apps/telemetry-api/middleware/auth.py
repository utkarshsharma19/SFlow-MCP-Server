"""API-key authentication middleware.

Accepts keys via `X-API-Key` header or `api_key` query param. Keys are
loaded from the `API_KEYS` env var (comma-separated). `/health`, `/docs`,
`/openapi.json`, and `/redoc` are always exempt so liveness probes and
developers can still reach documentation without a key.
"""
import os

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

EXEMPT_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}


def _load_keys() -> set[str]:
    raw = os.getenv("API_KEYS", "dev-insecure-key")
    return {k.strip() for k in raw.split(",") if k.strip()}


class APIKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self._keys = _load_keys()

    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)
        key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if not key or key not in self._keys:
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid or missing API key"},
            )
        return await call_next(request)
