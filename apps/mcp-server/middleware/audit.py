"""Tool-call audit + quota decorator for MCP tools (PR 28 + hardening PR 29).

Wraps the inner tool so that every invocation:

1. Calls ``POST /tool-audit/consume`` to atomically bump the caller's
   quota counter. If the tenant is over limit the decorator short-
   circuits with a 429-shaped error dict — the tool body never runs.
2. Validates args against a prompt-injection guardrail. Suspicious
   input (LLM control tokens, role-claim phrases, oversized strings)
   short-circuits with ``status=rejected`` — the tool body never runs.
3. Runs the tool.
4. Enforces a per-tool response-size cap. Oversized responses are
   replaced with a ``response_too_large`` error so the LLM can back
   off; the audit row records the original byte count + ``truncated``
   status so operators can tune caps.
5. Fires an audit record (fire-and-forget — a slow audit path must
   never stall the caller) with a salted sha256 of the arguments, the
   response size, and the tool's self-reported ``confidence_note`` band.

The hash is *salted* per deployment (``MCP_ARGS_HASH_SALT`` env var) so
audit hashes are not comparable across tenants or deployments — a
regulator looking at one tenant's audit log cannot correlate tool calls
against another tenant's log by hash.

This deliberately records the *hash* of args, never the plaintext. The
raw args may carry customer-sensitive scopes (hostnames, interface IDs,
IP prefixes). The audit consumer only needs deduping + "did this call
ever happen" — the salted hash is sufficient for both.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import time
from functools import wraps
from typing import Any, Callable

from client import get_client

log = logging.getLogger(__name__)

TRUNCATE_BYTES = 512
DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024  # 256 KiB; override per tool via decorator
MAX_ARG_STRING_LEN = 2048  # per-field cap; longer values look like injected payloads

_ARGS_HASH_SALT = os.getenv("MCP_ARGS_HASH_SALT", "")

# Patterns that almost never appear in legitimate telemetry args but are
# cheap for an attacker to inject. Keep the set tight — false positives
# here degrade the product.
_INJECTION_PATTERNS = re.compile(
    r"(?ix)"
    r"<\|(?:im_start|im_end|system|user|assistant|endoftext)\|>"  # chat-ml style
    r"|\[INST\]|\[/INST\]"                                         # llama instruct
    r"|</?s>"                                                      # sentence tokens
    r"|ignore\s+(?:all\s+)?previous\s+instructions"
    r"|disregard\s+(?:the\s+)?(?:above|prior)"
    r"|you\s+are\s+now\s+(?:a|an|the)\s+"
    r"|system\s*:\s*you\s+(?:are|must)"
)


def _hash_args(args: dict[str, Any]) -> str:
    """Salted sha256 of canonical-json args.

    Salt comes from ``MCP_ARGS_HASH_SALT`` — rotate it (via key mgmt) to
    invalidate old hashes. When unset, falls back to unsalted sha256 so
    dev setups still work, logged as a warning on first call.
    """
    payload = json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))
    if _ARGS_HASH_SALT:
        return hmac.new(
            _ARGS_HASH_SALT.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    if not getattr(_hash_args, "_warned", False):
        log.warning(
            "MCP_ARGS_HASH_SALT not set; audit hashes are cross-tenant comparable"
        )
        _hash_args._warned = True  # type: ignore[attr-defined]
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _looks_like_injection(args: dict[str, Any]) -> str | None:
    """Return a short reason string if any arg looks injected, else None.

    Two checks per string value: regex match against known control
    tokens/role-claim phrases, and a length cap. Non-string values pass
    through — network IDs and integers can't carry prompt payloads.
    """
    for k, v in args.items():
        if not isinstance(v, str):
            continue
        if len(v) > MAX_ARG_STRING_LEN:
            return f"arg_too_long:{k}"
        if _INJECTION_PATTERNS.search(v):
            return f"suspicious_pattern:{k}"
    return None


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


def audit_tool(
    tool_name: str,
    *,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> Callable:
    """Decorate an async MCP tool with quota + audit + guardrails.

    Apply *above* ``@rate_limit`` so rate limiting is skipped when the
    tenant is already over quota — saves a wasted rate-limit bucket slot.

    ``max_response_bytes`` bounds the JSON-encoded response size. Tools
    whose natural output is bulk (``get_recent_anomalies`` with high
    ``limit``) should raise it explicitly; most tools should not need
    to. Oversized responses are replaced with a structured error, not
    truncated — silent truncation would let the model act on partial
    data without knowing.
    """

    def decorator(fn):
        sig = inspect.signature(fn)

        @wraps(fn)
        async def wrapper(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            call_args = dict(bound.arguments)

            injected = _looks_like_injection(call_args)
            if injected is not None:
                log.warning("rejecting %s call: %s", tool_name, injected)
                asyncio.create_task(
                    _record(
                        tool_name=tool_name,
                        args=call_args,
                        response_bytes=0,
                        confidence_band=None,
                        status="rejected",
                        duration_ms=0,
                    )
                )
                return {"error": "args_rejected", "reason": injected}

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
                    if resp_bytes > max_response_bytes:
                        log.warning(
                            "%s response %d bytes exceeds cap %d; replacing",
                            tool_name,
                            resp_bytes,
                            max_response_bytes,
                        )
                        status = "truncated"
                        result = {
                            "error": "response_too_large",
                            "tool": tool_name,
                            "response_bytes": resp_bytes,
                            "limit": max_response_bytes,
                            "hint": "narrow the window, lower the limit, or request a summary",
                        }
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
