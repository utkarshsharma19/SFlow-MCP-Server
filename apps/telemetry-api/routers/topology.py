from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from auth.context import TenantContext, require_role
from db import get_db
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
