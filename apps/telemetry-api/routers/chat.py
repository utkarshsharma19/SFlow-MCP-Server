"""Chat session CRUD endpoints (PR 31).

These let an external chatbot gateway track per-conversation token use
against the same ``tenant_quotas`` rows that the MCP tool path bumps,
plus a per-session breakdown the operator can audit ("what did
operator X actually ask the bot last week?").

Endpoints:

  POST   /chat-sessions             — open a new session, returns id
  PATCH  /chat-sessions/{id}        — bump tokens_in/out/tool_calls + charge
                                      the tenant quota
  POST   /chat-sessions/{id}/end    — set ended_at
  GET    /chat-sessions/{id}        — read back (auditing/UI)
  GET    /chat-sessions             — list recent sessions for the tenant

All endpoints are viewer-role on read, viewer on write — the chat
gateway holds a viewer key by default. Higher role isn't needed because
each session is bound to its tenant by RLS; you can't accidentally
write a session into another tenant's record.

Token accounting: PATCH also calls ``charge_tokens`` on the tenant
quota so the existing 429-on-overrun path on ``/tool-audit/consume``
includes LLM tokens.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.context import TenantContext, require_role
from db import get_db
from db.models_chat import ChatSession
from services.tool_audit import charge_tokens

router = APIRouter(prefix="/chat-sessions", tags=["chat-sessions"])


@router.post("")
async def create_session(
    payload: dict = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    """Open a new session row. ``user_label`` is freeform — the gateway
    typically passes the chat user's display name or an opaque id."""
    user_label = payload.get("user_label")
    if user_label is not None and not isinstance(user_label, str):
        raise HTTPException(
            status_code=400, detail="user_label must be a string when provided"
        )
    row = ChatSession(
        tenant_id=ctx.tenant_id,
        api_key_id=ctx.api_key_id,
        user_label=(user_label or None),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _serialize(row)


@router.patch("/{session_id}")
async def bump_session(
    session_id: str = Path(...),
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    """Increment per-session counters and charge the tenant token quota.

    Body fields (all optional, all default to 0):
      tokens_in, tokens_out, tool_calls

    If the tenant is over the token limit, returns 429 — exactly the
    same shape as ``/tool-audit/consume``. The chat gateway should
    treat that as "stop sending more turns this period".
    """
    tokens_in = int(payload.get("tokens_in", 0))
    tokens_out = int(payload.get("tokens_out", 0))
    tool_calls = int(payload.get("tool_calls", 0))
    if min(tokens_in, tokens_out, tool_calls) < 0:
        raise HTTPException(status_code=400, detail="counters must be >= 0")

    row = await _load(db, ctx.tenant_id, session_id)
    row.tokens_in += tokens_in
    row.tokens_out += tokens_out
    row.tool_calls += tool_calls
    await db.commit()
    await db.refresh(row)

    decision = None
    total_tokens = tokens_in + tokens_out
    if total_tokens > 0:
        decision = await charge_tokens(
            db,
            tenant_id=ctx.tenant_id,
            tokens=total_tokens,
        )
        if not decision.allowed:
            raise HTTPException(status_code=429, detail=decision.to_response())

    out = _serialize(row)
    if decision is not None:
        out["quota"] = decision.to_response()
    return out


@router.post("/{session_id}/end")
async def end_session(
    session_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    row = await _load(db, ctx.tenant_id, session_id)
    if row.ended_at is None:
        row.ended_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(row)
    return _serialize(row)


@router.get("/{session_id}")
async def get_session(
    session_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    row = await _load(db, ctx.tenant_id, session_id)
    return _serialize(row)


@router.get("")
async def list_sessions(
    limit: int = Query(default=50, ge=1, le=500),
    open_only: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    q = (
        select(ChatSession)
        .where(ChatSession.tenant_id == ctx.tenant_id)
        .order_by(ChatSession.started_at.desc())
        .limit(limit)
    )
    if open_only:
        q = q.where(ChatSession.ended_at.is_(None))
    rows = (await db.execute(q)).scalars().all()
    return {"sessions": [_serialize(r) for r in rows], "total": len(rows)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _load(
    db: AsyncSession, tenant_id: str, session_id: str
) -> ChatSession:
    q = (
        select(ChatSession)
        .where(ChatSession.tenant_id == tenant_id)
        .where(ChatSession.id == session_id)
    )
    row = (await db.execute(q)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="chat session not found")
    return row


def _serialize(row: ChatSession) -> dict:
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "api_key_id": str(row.api_key_id) if row.api_key_id else None,
        "user_label": row.user_label,
        "started_at": row.started_at.isoformat(),
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "tokens_in": int(row.tokens_in),
        "tokens_out": int(row.tokens_out),
        "tool_calls": int(row.tool_calls),
    }
