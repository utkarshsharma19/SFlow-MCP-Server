from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from auth.context import TenantContext, require_role
from db import get_db
from services.top_offenders import get_top_offenders
from services.topology import list_devices, list_interfaces

router = APIRouter(prefix="/topology", tags=["topology"])


@router.get("/devices")
async def devices(
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    return await list_devices(db, ctx.tenant_id)


@router.get("/interfaces")
async def interfaces(
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    return await list_interfaces(db, ctx.tenant_id)


@router.get("/top-offenders")
async def top_offenders(
    scope: str = Query(default="device", pattern="^(device|interface)$"),
    window_minutes: int = Query(default=60, ge=1, le=1440),
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    return await get_top_offenders(
        db, ctx.tenant_id, scope, window_minutes, limit
    )
