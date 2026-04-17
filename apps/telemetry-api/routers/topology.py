from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from services.topology import list_devices, list_interfaces

router = APIRouter(prefix="/topology", tags=["topology"])


@router.get("/devices")
async def devices(db: AsyncSession = Depends(get_db)):
    return await list_devices(db)


@router.get("/interfaces")
async def interfaces(db: AsyncSession = Depends(get_db)):
    return await list_interfaces(db)
