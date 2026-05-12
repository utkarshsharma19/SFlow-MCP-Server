"""Shared httpx client used by every MCP tool to hit the telemetry API.

Includes a per-path circuit breaker (PR 30). If telemetry-API is slow or
throwing 5xx for a particular endpoint, the breaker trips after
``CB_FAILURE_THRESHOLD`` consecutive failures and short-circuits new
requests for ``CB_COOLDOWN_SECONDS`` with a structured
``telemetry_unavailable`` error. After cooldown the breaker enters
half-open and the next request is allowed through to probe recovery.
"""
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger(__name__)

TELEMETRY_API_URL = os.getenv("TELEMETRY_API_URL", "http://localhost:8080")
TELEMETRY_API_KEY = os.getenv("TELEMETRY_API_KEY", "dev-insecure-key")

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=TELEMETRY_API_URL,
            timeout=15,
            headers={"X-API-Key": TELEMETRY_API_KEY},
        )
    return _client


# ---------------------------------------------------------------------------
# Per-path circuit breaker
# ---------------------------------------------------------------------------

CB_FAILURE_THRESHOLD = int(os.getenv("MCP_CB_FAILURE_THRESHOLD", "5"))
CB_COOLDOWN_SECONDS = float(os.getenv("MCP_CB_COOLDOWN_SECONDS", "30"))


@dataclass
class _BreakerState:
    """One state machine per telemetry path.

    Three logical states:
      * closed (failures < threshold)   — pass through
      * open   (opened_at + cooldown)   — fast-fail
      * half-open (after cooldown)      — let the next call probe

    We model these with two fields rather than an enum because the
    transitions are short and the math (was-the-cooldown-fast-failed?)
    is easier to read inline than as a state-machine table.
    """

    failures: int = 0
    opened_at: float = 0.0


_breakers: dict[str, _BreakerState] = {}


def _breaker_for(path: str) -> _BreakerState:
    if path not in _breakers:
        _breakers[path] = _BreakerState()
    return _breakers[path]


def _circuit_is_open(state: _BreakerState, now: float) -> bool:
    if state.failures < CB_FAILURE_THRESHOLD:
        return False
    if now - state.opened_at >= CB_COOLDOWN_SECONDS:
        # Half-open: allow the next caller through. We *don't* reset
        # failures yet — the next success in _record_success does that.
        return False
    return True


def _record_failure(state: _BreakerState, now: float) -> None:
    state.failures += 1
    if state.failures == CB_FAILURE_THRESHOLD:
        state.opened_at = now
        log.warning(
            "circuit_breaker_open: %d consecutive failures",
            state.failures,
        )


def _record_success(state: _BreakerState) -> None:
    if state.failures >= CB_FAILURE_THRESHOLD:
        log.info("circuit_breaker_closed: probe succeeded")
    state.failures = 0
    state.opened_at = 0.0


def _open_response(path: str) -> dict:
    return {
        "error": "telemetry_unavailable",
        "reason": "circuit_breaker_open",
        "path": path,
        "retry_after_seconds": int(CB_COOLDOWN_SECONDS),
    }


def _reset_breakers_for_tests() -> None:
    _breakers.clear()


async def get_telemetry(path: str, params: dict | None = None) -> dict:
    state = _breaker_for(path)
    now = time.monotonic()
    if _circuit_is_open(state, now):
        return _open_response(path)

    client = get_client()
    try:
        resp = await client.get(path, params=params or {})
        # 4xx is a *caller* problem (bad params, no auth) and should not
        # count against the breaker — those errors won't fix themselves
        # by waiting. Only 5xx and transport failures trip the wire.
        if 500 <= resp.status_code < 600:
            _record_failure(state, now)
            return {
                "error": f"Telemetry API returned {resp.status_code}",
                "path": path,
            }
        _record_success(state)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        log.error(f"Telemetry API error {e.response.status_code}: {path}")
        return {
            "error": f"Telemetry API returned {e.response.status_code}",
            "path": path,
        }
    except httpx.RequestError as e:
        _record_failure(state, now)
        log.error(f"Telemetry API unreachable: {e}")
        return {"error": "Telemetry API unreachable", "detail": str(e)}


async def post_telemetry(path: str, json: dict | None = None) -> dict:
    """POST helper for write-side tools (anomaly ack/resolve).

    Mirrors :func:`get_telemetry` so MCP tools never raise: 4xx/5xx and
    transport failures both become an ``{"error": ...}`` dict the model
    can reason about. The caller's ``X-API-Key`` is attached by the
    shared client; the underlying route enforces role + tenant.
    """
    state = _breaker_for(path)
    now = time.monotonic()
    if _circuit_is_open(state, now):
        return _open_response(path)

    client = get_client()
    try:
        resp = await client.post(path, json=json or {})
        if 500 <= resp.status_code < 600:
            _record_failure(state, now)
            return {
                "error": f"Telemetry API returned {resp.status_code}",
                "path": path,
            }
        _record_success(state)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        log.error(f"Telemetry API error {e.response.status_code}: {path}")
        body: dict = {}
        try:
            body = e.response.json()
        except ValueError:
            body = {"detail": e.response.text[:256]}
        return {
            "error": f"Telemetry API returned {e.response.status_code}",
            "path": path,
            **body,
        }
    except httpx.RequestError as e:
        _record_failure(state, now)
        log.error(f"Telemetry API unreachable: {e}")
        return {"error": "Telemetry API unreachable", "detail": str(e)}
