from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from services.anomalies_query import get_recent_anomalies

router = APIRouter(prefix="/anomalies", tags=["anomalies"])


@router.get("/recent")
async def recent(
    scope: str = Query(default="global"),
    severity_min: str = Query(default="medium", pattern="^(low|medium|high|critical)$"),
    since_minutes: int = Query(default=30, ge=1, le=1440),
    db: AsyncSession = Depends(get_db),
):
    return await get_recent_anomalies(db, scope, severity_min, since_minutes)
