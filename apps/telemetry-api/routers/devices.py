"""gNMI-derived device-state endpoints (PR 21)."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from auth.context import TenantContext, require_role
from db import get_db
from services.device_state import get_device_state, list_devices_with_gnmi

router = APIRouter(prefix="/devices", tags=["devices"])


@router.get("/state")
async def device_state(
    device: str = Query(...),
    window_minutes: int = Query(default=15, ge=1, le=60),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    return await get_device_state(db, ctx.tenant_id, device, window_minutes)


@router.get("/gnmi-sources")
async def gnmi_sources(
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    devices = await list_devices_with_gnmi(db, ctx.tenant_id)
    return {"devices": devices, "total": len(devices)}
