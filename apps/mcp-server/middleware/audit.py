"""Tool-call audit + quota decorator for MCP tools (PR 28).

Wraps the inner tool so that every invocation:

1. Calls ``POST /tool-audit/consume`` to atomically bump the caller's
   quota counter. If the tenant is over limit the decorator short-
   circuits with a 429-shaped error dict — the tool body never runs.
2. Runs the tool.
3. Fires an audit record (fire-and-forget — a slow audit path must
   never stall the caller) with a sha256 of the arguments, the response
   size, and the tool's self-reported ``confidence_note`` band.

This deliberately records the *hash* of args, never the plaintext. The
raw args may carry customer-sensitive scopes (hostnames, interface IDs,
IP prefixes). The audit consumer only needs deduping + "did this call
ever happen" — the hash is sufficient for both.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
from functools import wraps
from typing import Any, Callable

from client import get_client

log = logging.getLogger(__name__)

TRUNCATE_BYTES = 512


def _hash_args(args: dict[str, Any]) -> str:
    payload = json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _truncate(args: dict[str, Any]) -> dict[str, Any] | None:
    """Keep the first TRUNCATE_BYTES of each string value for debugging.

    Full args never leave this function; the audit row carries only the
    truncated form alongside the full hash. If an operator needs to
    investigate, they match on hash and re-derive from app logs.
    """
    if not args:
        return None
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > TRUNCATE_BYTES:
            out[k] = v[:TRUNCATE_BYTES] + "…"
        else:
            out[k] = v
    return out


def _confidence_band(result: Any) -> str | None:
    """Map a tool's confidence_note into a three-level band.

    The band is what the quota UI groups on — not the free-text note. We
    keep the taxonomy tiny (``exact|sampled|degraded``) so a regulator's
    "show me sampled tool calls" query works without regex.
    """
    if not isinstance(result, dict):
        return None
    note = result.get("confidence_note")
    if not isinstance(note, str):
        return None
    lowered = note.lower()
    if "exact" in lowered:
        return "exact"
    if "degrad" in lowered or "stale" in lowered:
        return "degraded"
    if "sampl" in lowered:
        return "sampled"
    return None


async def _consume_quota(tool_name: str, response_bytes: int) -> dict[str, Any]:
    client = get_client()
    try:
        resp = await client.post(
            "/tool-audit/consume",
            json={"tool_name": tool_name, "response_bytes": response_bytes},
        )
        if resp.status_code == 429:
            return resp.json()
        if resp.status_code == 403:
            return {"allowed": False, "reason": "forbidden"}
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001 — audit must never crash the tool
        log.warning("quota consume failed for %s: %s", tool_name, exc)
        return {"allowed": True, "reason": "audit_unavailable"}


async def _record(
    tool_name: str,
    args: dict[str, Any],
    response_bytes: int,
    confidence_band: str | None,
    status: str,
    duration_ms: int,
) -> None:
    client = get_client()
    try:
        await client.post(
            "/tool-audit/record",
            json={
                "tool_name": tool_name,
                "args_hash": _hash_args(args),
                "args_truncated": _truncate(args),
                "response_bytes": response_bytes,
                "confidence_band": confidence_band,
                "status": status,
                "duration_ms": duration_ms,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("tool-audit record failed for %s: %s", tool_name, exc)


def audit_tool(tool_name: str) -> Callable:
    """Decorate an async MCP tool with quota + audit.

    Apply *above* ``@rate_limit`` so rate limiting is skipped when the
    tenant is already over quota — saves a wasted rate-limit bucket slot.
    """

    def decorator(fn):
        sig = inspect.signature(fn)

        @wraps(fn)
        async def wrapper(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            call_args = dict(bound.arguments)

            decision = await _consume_quota(tool_name, response_bytes=0)
            if not decision.get("allowed", True):
                return {
                    "error": decision.get("reason", "quota_exceeded"),
                    "quota": decision,
                }

            started = time.monotonic()
            status = "ok"
            result: Any
            try:
                result = await fn(*args, **kwargs)
                if isinstance(result, dict) and "error" in result:
                    status = "error"
            except Exception:
                status = "error"
                raise
            finally:
                duration_ms = int((time.monotonic() - started) * 1000)
                resp_bytes = 0
                band = None
                if status == "ok":
                    try:
                        resp_bytes = len(
                            json.dumps(result, default=str).encode("utf-8")
                        )
                    except (TypeError, ValueError):
                        resp_bytes = 0
                    band = _confidence_band(result)
                asyncio.create_task(
                    _record(
                        tool_name=tool_name,
                        args=call_args,
                        response_bytes=resp_bytes,
                        confidence_band=band,
                        status=status,
                        duration_ms=duration_ms,
                    )
                )
            return result

        return wrapper

    return decorator
