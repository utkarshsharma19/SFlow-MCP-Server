from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from auth.context import TenantContext, require_role
from db import get_db
from services.anomalies_query import get_recent_anomalies
from services.anomaly_dedup import acknowledge as ack_service
from services.anomaly_dedup import resolve as resolve_service
from services.anomaly_narrative import summarize_recent_anomalies

router = APIRouter(prefix="/anomalies", tags=["anomalies"])


@router.get("/recent")
async def recent(
    scope: str = Query(default="global"),
    severity_min: str = Query(default="medium", pattern="^(low|medium|high|critical)$"),
    since_minutes: int = Query(default=30, ge=1, le=1440),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    return await get_recent_anomalies(
        db, ctx.tenant_id, scope, severity_min, since_minutes
    )


@router.get("/summary")
async def summary(
    scope: str = Query(default="global"),
    severity_min: str = Query(default="medium", pattern="^(low|medium|high|critical)$"),
    since_minutes: int = Query(default=30, ge=1, le=1440),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    return await summarize_recent_anomalies(
        db, ctx.tenant_id, scope, since_minutes, severity_min
    )


@router.post("/{anomaly_id}/acknowledge")
async def acknowledge(
    anomaly_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("operator")),
):
    """Mark an open anomaly acknowledged by the calling operator.

    Idempotent: ack is a no-op if already acknowledged (rowcount=0). The
    distinction between "not found" and "already acked" doesn't matter to
    the chatbot caller — both mean "nothing changed".
    """
    ok = await ack_service(
        db,
        tenant_id=ctx.tenant_id,
        anomaly_id=anomaly_id,
        api_key_id=ctx.api_key_id,
    )
    if not ok:
        raise HTTPException(
            status_code=404,
            detail="anomaly not found, not yours, or already acknowledged",
        )
    return {
        "anomaly_id": anomaly_id,
        "status": "acknowledged",
        "acknowledged_by_api_key_id": ctx.api_key_id,
    }


@router.post("/{anomaly_id}/resolve")
async def resolve(
    anomaly_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("operator")),
):
    """Mark an anomaly resolved. Closes the dedup window — a recurrence opens a new row."""
    ok = await resolve_service(
        db,
        tenant_id=ctx.tenant_id,
        anomaly_id=anomaly_id,
        api_key_id=ctx.api_key_id,
    )
    if not ok:
        raise HTTPException(
            status_code=404,
            detail="anomaly not found, not yours, or already resolved",
        )
    return {
        "anomaly_id": anomaly_id,
        "status": "resolved",
        "resolved_by_api_key_id": ctx.api_key_id,
    }
