"""Shared httpx client used by every MCP tool to hit the telemetry API."""
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger(__name__)

TELEMETRY_API_URL = os.getenv("TELEMETRY_API_URL", "http://localhost:8080")

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=TELEMETRY_API_URL, timeout=15)
    return _client


async def get_telemetry(path: str, params: dict | None = None) -> dict:
    client = get_client()
    try:
        resp = await client.get(path, params=params or {})
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        log.error(f"Telemetry API error {e.response.status_code}: {path}")
        return {
            "error": f"Telemetry API returned {e.response.status_code}",
            "path": path,
        }
    except httpx.RequestError as e:
        log.error(f"Telemetry API unreachable: {e}")
        return {"error": "Telemetry API unreachable", "detail": str(e)}
