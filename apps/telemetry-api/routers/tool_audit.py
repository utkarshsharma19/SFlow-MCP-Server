"""MCP-facing: tool call audit + quota endpoints (PR 28)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.context import TenantContext, require_role
from db import get_db
from db.models import TenantQuota, ToolCallAudit
from services.tool_audit import (
    consume_quota,
    current_period_start,
    record_tool_call,
    set_quota,
)

router = APIRouter(prefix="/tool-audit", tags=["tool-audit"])


@router.post("/consume")
async def consume(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    tool_name = payload["tool_name"]
    if not ctx.may_call_tool(tool_name):
        raise HTTPException(
            status_code=403,
            detail=f"api key is not allowed to call {tool_name}",
        )
    decision = await consume_quota(
        db,
        tenant_id=ctx.tenant_id,
        tool_name=tool_name,
        response_bytes=int(payload.get("response_bytes", 0)),
    )
    if not decision.allowed:
        raise HTTPException(status_code=429, detail=decision.to_response())
    return decision.to_response()


@router.post("/record")
async def record(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    await record_tool_call(
        db,
        tenant_id=ctx.tenant_id,
        api_key_id=ctx.api_key_id,
        tool_name=payload["tool_name"],
        args_hash=payload["args_hash"],
        args_truncated=payload.get("args_truncated"),
        response_bytes=payload.get("response_bytes"),
        confidence_band=payload.get("confidence_band"),
        status=payload.get("status", "ok"),
        duration_ms=payload.get("duration_ms"),
    )
    return {"status": "recorded"}


@router.get("/recent")
async def recent(
    tool_name: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    q = (
        select(ToolCallAudit)
        .where(ToolCallAudit.tenant_id == ctx.tenant_id)
        .order_by(ToolCallAudit.ts.desc())
        .limit(limit)
    )
    if tool_name:
        q = q.where(ToolCallAudit.tool_name == tool_name)
    rows = (await db.execute(q)).scalars().all()
    return {
        "calls": [
            {
                "ts": r.ts.isoformat(),
                "tool_name": r.tool_name,
                "args_hash": r.args_hash,
                "response_bytes": r.response_bytes,
                "confidence_band": r.confidence_band,
                "status": r.status,
                "duration_ms": r.duration_ms,
            }
            for r in rows
        ]
    }


@router.get("/quota")
async def get_quota(
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    period = current_period_start()
    rows = (
        await db.execute(
            select(TenantQuota)
            .where(TenantQuota.tenant_id == ctx.tenant_id)
            .where(TenantQuota.period_start == period)
        )
    ).scalars().all()
    return {
        "period_start": period.isoformat(),
        "quotas": [
            {
                "tool_name": r.tool_name,
                "calls_this_period": r.calls_this_period,
                "bytes_out_this_period": r.bytes_out_this_period,
                "call_limit": r.call_limit,
                "byte_limit": r.byte_limit,
            }
            for r in rows
        ],
    }


@router.post("/quota")
async def put_quota(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    await set_quota(
        db,
        tenant_id=ctx.tenant_id,
        tool_name=payload["tool_name"],
        call_limit=payload.get("call_limit"),
        byte_limit=payload.get("byte_limit"),
    )
    return {"status": "ok"}
