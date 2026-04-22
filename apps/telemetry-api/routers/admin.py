"""Admin surface: anomaly lifecycle, source freshness, API key rotation."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.context import TenantContext, require_role
from db import get_db
from db.models import APIKey, SourceFreshness
from services.anomaly_dedup import acknowledge, resolve
from services.api_key_rotation import mint_key, revoke_key, rotate_key

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/anomalies/{anomaly_id}/ack")
async def ack_anomaly(
    anomaly_id: str,
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("operator")),
):
    ok = await acknowledge(
        db,
        tenant_id=ctx.tenant_id,
        anomaly_id=anomaly_id,
        api_key_id=ctx.api_key_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="anomaly not found or already acked")
    return {"status": "acknowledged", "anomaly_id": anomaly_id}


@router.post("/anomalies/{anomaly_id}/resolve")
async def resolve_anomaly(
    anomaly_id: str,
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("operator")),
):
    ok = await resolve(
        db,
        tenant_id=ctx.tenant_id,
        anomaly_id=anomaly_id,
        api_key_id=ctx.api_key_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="anomaly not found or already resolved")
    return {"status": "resolved", "anomaly_id": anomaly_id}


@router.post("/keys")
async def create_key(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    ttl_days = payload.get("ttl_days")
    minted = await mint_key(
        db,
        tenant_id=ctx.tenant_id,
        role=payload.get("role", "viewer"),
        name=payload["name"],
        tool_allowlist=payload.get("tool_allowlist"),
        rate_limit_per_minute=payload.get("rate_limit_per_minute"),
        ttl=timedelta(days=ttl_days) if ttl_days else None,
    )
    return {
        "id": minted.id,
        "key": minted.plaintext,        # shown once
        "prefix": minted.prefix,
        "expires_at": minted.expires_at.isoformat() if minted.expires_at else None,
    }


@router.post("/keys/{key_id}/rotate")
async def rotate_key_route(
    key_id: str,
    grace_hours: int = Query(default=24, ge=0, le=168),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    minted = await rotate_key(
        db,
        tenant_id=ctx.tenant_id,
        old_key_id=key_id,
        grace=timedelta(hours=grace_hours),
    )
    return {
        "id": minted.id,
        "key": minted.plaintext,
        "prefix": minted.prefix,
        "expires_at": minted.expires_at.isoformat() if minted.expires_at else None,
        "rotated_from_id": key_id,
    }


@router.delete("/keys/{key_id}")
async def revoke_key_route(
    key_id: str,
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    ok = await revoke_key(db, tenant_id=ctx.tenant_id, key_id=key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="key not found")
    return {"status": "revoked", "key_id": key_id}


@router.get("/keys")
async def list_keys(
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    rows = (
        await db.execute(
            select(APIKey)
            .where(APIKey.tenant_id == ctx.tenant_id)
            .order_by(APIKey.created_at.desc())
        )
    ).scalars().all()
    return {
        "keys": [
            {
                "id": str(r.id),
                "prefix": r.key_prefix,
                "role": r.role,
                "name": r.name,
                "is_active": r.is_active,
                "created_at": r.created_at.isoformat(),
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
                "rotated_from_id": str(r.rotated_from_id) if r.rotated_from_id else None,
                "tool_allowlist": r.tool_allowlist,
                "rate_limit_per_minute": r.rate_limit_per_minute,
            }
            for r in rows
        ]
    }


@router.get("/sources/freshness")
async def sources_freshness(
    status_in: str | None = Query(default=None, description="Comma-separated statuses"),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    q = select(SourceFreshness).where(SourceFreshness.tenant_id == ctx.tenant_id)
    if status_in:
        statuses = [s.strip() for s in status_in.split(",") if s.strip()]
        q = q.where(SourceFreshness.status.in_(statuses))
    rows = (await db.execute(q)).scalars().all()
    return {
        "sources": [
            {
                "source_kind": r.source_kind,
                "device": r.device,
                "last_ingest_ts": r.last_ingest_ts.isoformat(),
                "last_sample_count": r.last_sample_count,
                "status": r.status,
            }
            for r in rows
        ]
    }
