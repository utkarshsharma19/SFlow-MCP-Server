from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from auth.context import TenantContext, require_role
from db import get_db
from services.interfaces import get_interface_utilization

router = APIRouter(prefix="/interfaces", tags=["interfaces"])


@router.get("/utilization")
async def interface_utilization(
    device: str = Query(...),
    interface: str = Query(...),
    window_minutes: int = Query(default=15, ge=1, le=60),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    return await get_interface_utilization(
        db, ctx.tenant_id, device, interface, window_minutes
    )
