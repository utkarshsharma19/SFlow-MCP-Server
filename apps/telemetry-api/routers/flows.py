from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from services.explain_link import explain_hot_link
from services.flows import get_top_talkers
from services.protocol_mix import summarize_protocol_mix

router = APIRouter(prefix="/flows", tags=["flows"])


@router.get("/top-talkers")
async def top_talkers(
    window_minutes: int = Query(default=15, ge=1, le=60),
    scope: str = Query(default="global"),
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    return await get_top_talkers(db, window_minutes, scope, limit)


@router.get("/explain-link")
async def explain_link(
    device: str = Query(...),
    interface: str = Query(...),
    window_minutes: int = Query(default=15, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
):
    return await explain_hot_link(db, device, interface, window_minutes)


@router.get("/protocol-mix")
async def protocol_mix(
    scope: str = Query(default="global"),
    window_minutes: int = Query(default=15, ge=1, le=60),
    db: AsyncSession = Depends(get_db),
):
    return await summarize_protocol_mix(db, scope, window_minutes)
