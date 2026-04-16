from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from services.interfaces import get_interface_utilization

router = APIRouter(prefix="/interfaces", tags=["interfaces"])


@router.get("/utilization")
async def interface_utilization(
    device: str = Query(...),
    interface: str = Query(...),
    window_minutes: int = Query(default=15, ge=1, le=60),
    db: AsyncSession = Depends(get_db),
):
    return await get_interface_utilization(db, device, interface, window_minutes)
