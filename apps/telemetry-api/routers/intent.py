"""Intent-vs-state diff endpoint (PR 29).

Read-only diff. Writes to the intent tables happen via ``scripts/seed.py``
or the future Verity sync connector — not over HTTP — so a leaked
viewer key can't reshape what "intent" means.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from auth.context import TenantContext, require_role
from db import get_db
from services.intent_diff import diff_intent_vs_state

router = APIRouter(prefix="/intent", tags=["intent"])


@router.get("/diff")
async def intent_diff(
    device: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("viewer")),
):
    return await diff_intent_vs_state(db, ctx.tenant_id, device)
