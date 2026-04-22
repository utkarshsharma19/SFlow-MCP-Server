"""Fabric imbalance endpoint (PR 24)."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from auth.context import TenantContext, require_role
from db import get_db
from services.fabric import detect_fabric_imbalance

router = APIRouter(prefix="/fabric", tags=["fabric"])


@router.get("/imbalance")
async def fabric_imbalance(
    device: str | None = Query(default=None),
    window_minutes: int = Query(default=15, ge=1, le=60),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("analyst")),
):
    return await detect_fabric_imbalance(db, ctx.tenant_id, device, window_minutes)
