from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from services.traffic_compare import compare_windows

router = APIRouter(prefix="/traffic", tags=["traffic"])


def _parse_iso(name: str, value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"{name} must be ISO-8601 (e.g. 2026-04-17T10:00:00+00:00): {e}",
        )


@router.get("/compare")
async def compare(
    scope: str = Query(default="global"),
    baseline_start: str = Query(...),
    baseline_end: str = Query(...),
    current_start: str = Query(...),
    current_end: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    bs = _parse_iso("baseline_start", baseline_start)
    be = _parse_iso("baseline_end", baseline_end)
    cs = _parse_iso("current_start", current_start)
    ce = _parse_iso("current_end", current_end)
    if be <= bs or ce <= cs:
        raise HTTPException(status_code=400, detail="End must be after start for both windows")
    return await compare_windows(db, scope, bs, be, cs, ce)
