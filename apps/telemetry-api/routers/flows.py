from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from services.flows import get_top_talkers

router = APIRouter(prefix="/flows", tags=["flows"])


@router.get("/top-talkers")
async def top_talkers(
    window_minutes: int = Query(default=15, ge=1, le=60),
    scope: str = Query(default="global"),
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    return await get_top_talkers(db, window_minutes, scope, limit)
