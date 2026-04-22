"""RDMA / RoCE fabric health endpoint (PR 23)."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from auth.context import TenantContext, require_role
from db import get_db
from services.rdma import get_rdma_health

router = APIRouter(prefix="/rdma", tags=["rdma"])


@router.get("/health")
async def rdma_health(
    device: str | None = Query(default=None),
    window_minutes: int = Query(default=15, ge=1, le=60),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("analyst")),
):
    return await get_rdma_health(db, ctx.tenant_id, device, window_minutes)
