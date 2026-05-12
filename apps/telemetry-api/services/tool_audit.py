"""Tool-call audit trail + per-tenant quota accounting (PR 28).

Two concerns, one service because they share the write path:

- ``record_tool_call`` appends an immutable audit row describing what the
  LLM invoked. Caller passes the already-hashed arguments — this service
  never sees plaintext arguments, which matters when the tool payload
  could carry sensitive IPs, hostnames, or operator IDs.

- ``consume_quota`` atomically increments the current period's counters
  and returns whether the caller is still under limit. Returning the
  remaining budget lets the MCP server attach it to the response so the
  model can back off before hitting a hard 429.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def current_period_start(now: datetime | None = None) -> datetime:
    """Day-anchored period start (UTC). Keeps reporting grids aligned."""
    now = now or datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    calls_this_period: int
    call_limit: int | None
    bytes_out_this_period: int
    byte_limit: int | None
    llm_tokens_this_period: int
    token_limit: int | None
    reason: str

    def to_response(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "calls_this_period": self.calls_this_period,
            "call_limit": self.call_limit,
            "bytes_out_this_period": self.bytes_out_this_period,
            "byte_limit": self.byte_limit,
            "llm_tokens_this_period": self.llm_tokens_this_period,
            "token_limit": self.token_limit,
            "reason": self.reason,
        }


async def consume_quota(
    db: AsyncSession,
    *,
    tenant_id: str,
    tool_name: str,
    response_bytes: int = 0,
    now: datetime | None = None,
) -> QuotaDecision:
    """Atomically bump counters and return whether the caller is within limits.

    Uses ``INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING`` so a
    single round-trip does both the check and the increment. Limits can
    be pre-seeded via admin tooling; a NULL limit means unlimited.
    """
    period_start = current_period_start(now)
    row = (
        await db.execute(
            text(
                """
                INSERT INTO tenant_quotas
                    (tenant_id, tool_name, period_start,
                     calls_this_period, bytes_out_this_period,
                     call_limit, byte_limit, updated_at)
                VALUES (:tenant, :tool, :period, 1, :bytes, NULL, NULL, now())
                ON CONFLICT (tenant_id, tool_name, period_start) DO UPDATE
                SET calls_this_period = tenant_quotas.calls_this_period + 1,
                    bytes_out_this_period =
                        tenant_quotas.bytes_out_this_period + EXCLUDED.bytes_out_this_period,
                    updated_at = now()
                RETURNING calls_this_period, bytes_out_this_period,
                          llm_tokens_this_period,
                          call_limit, byte_limit, token_limit
                """
            ),
            {
                "tenant": tenant_id,
                "tool": tool_name,
                "period": period_start,
                "bytes": response_bytes,
            },
        )
    ).one()
    await db.commit()

    over_calls = row.call_limit is not None and row.calls_this_period > row.call_limit
    over_bytes = row.byte_limit is not None and row.bytes_out_this_period > row.byte_limit
    over_tokens = (
        row.token_limit is not None and row.llm_tokens_this_period > row.token_limit
    )
    allowed = not (over_calls or over_bytes or over_tokens)
    if allowed:
        reason = "ok"
    elif over_calls:
        reason = "call_limit_exceeded"
    elif over_bytes:
        reason = "byte_limit_exceeded"
    else:
        reason = "token_limit_exceeded"
    return QuotaDecision(
        allowed=allowed,
        calls_this_period=row.calls_this_period,
        call_limit=row.call_limit,
        bytes_out_this_period=row.bytes_out_this_period,
        byte_limit=row.byte_limit,
        llm_tokens_this_period=row.llm_tokens_this_period,
        token_limit=row.token_limit,
        reason=reason,
    )


async def charge_tokens(
    db: AsyncSession,
    *,
    tenant_id: str,
    tool_name: str = "*",
    tokens: int,
    now: datetime | None = None,
) -> QuotaDecision:
    """Atomically add ``tokens`` to the current period's token counter.

    The chat gateway calls this once per turn with the (prompt +
    completion) token count from Anthropic's response. We default
    ``tool_name='*'`` because tokens are billed at the conversation
    level, not the tool level — operators set the limit on the wildcard
    row.
    """
    period_start = current_period_start(now)
    row = (
        await db.execute(
            text(
                """
                INSERT INTO tenant_quotas
                    (tenant_id, tool_name, period_start,
                     calls_this_period, bytes_out_this_period,
                     llm_tokens_this_period,
                     call_limit, byte_limit, token_limit, updated_at)
                VALUES (:tenant, :tool, :period, 0, 0, :tokens,
                        NULL, NULL, NULL, now())
                ON CONFLICT (tenant_id, tool_name, period_start) DO UPDATE
                SET llm_tokens_this_period =
                        tenant_quotas.llm_tokens_this_period + EXCLUDED.llm_tokens_this_period,
                    updated_at = now()
                RETURNING calls_this_period, bytes_out_this_period,
                          llm_tokens_this_period,
                          call_limit, byte_limit, token_limit
                """
            ),
            {
                "tenant": tenant_id,
                "tool": tool_name,
                "period": period_start,
                "tokens": int(tokens),
            },
        )
    ).one()
    await db.commit()

    over = row.token_limit is not None and row.llm_tokens_this_period > row.token_limit
    return QuotaDecision(
        allowed=not over,
        calls_this_period=row.calls_this_period,
        call_limit=row.call_limit,
        bytes_out_this_period=row.bytes_out_this_period,
        byte_limit=row.byte_limit,
        llm_tokens_this_period=row.llm_tokens_this_period,
        token_limit=row.token_limit,
        reason="ok" if not over else "token_limit_exceeded",
    )


async def record_tool_call(
    db: AsyncSession,
    *,
    tenant_id: str,
    api_key_id: str | None,
    tool_name: str,
    args_hash: str,
    args_truncated: dict[str, Any] | None,
    response_bytes: int | None,
    confidence_band: str | None,
    status: str,
    duration_ms: int | None,
) -> None:
    await db.execute(
        text(
            """
            INSERT INTO tool_call_audit (
                tenant_id, api_key_id, tool_name, args_hash, args_truncated,
                response_bytes, confidence_band, status, duration_ms
            ) VALUES (
                :tenant, :key, :tool, :hash, :args::jsonb,
                :bytes, :band, :status, :duration
            )
            """
        ),
        {
            "tenant": tenant_id,
            "key": api_key_id,
            "tool": tool_name,
            "hash": args_hash,
            "args": _json_dump(args_truncated),
            "bytes": response_bytes,
            "band": confidence_band,
            "status": status,
            "duration": duration_ms,
        },
    )
    await db.commit()


def _json_dump(value: Any) -> str | None:
    if value is None:
        return None
    import json

    return json.dumps(value, default=str)


async def set_quota(
    db: AsyncSession,
    *,
    tenant_id: str,
    tool_name: str,
    call_limit: int | None,
    byte_limit: int | None,
    token_limit: int | None = None,
    period_start: datetime | None = None,
) -> None:
    """Admin-facing: set (or clear) limits for the current period."""
    period = period_start or current_period_start()
    await db.execute(
        text(
            """
            INSERT INTO tenant_quotas
                (tenant_id, tool_name, period_start,
                 call_limit, byte_limit, token_limit, updated_at)
            VALUES (:tenant, :tool, :period, :call, :byte, :token, now())
            ON CONFLICT (tenant_id, tool_name, period_start) DO UPDATE
            SET call_limit = EXCLUDED.call_limit,
                byte_limit = EXCLUDED.byte_limit,
                token_limit = EXCLUDED.token_limit,
                updated_at = now()
            """
        ),
        {
            "tenant": tenant_id,
            "tool": tool_name,
            "period": period,
            "call": call_limit,
            "byte": byte_limit,
            "token": token_limit,
        },
    )
    await db.commit()
