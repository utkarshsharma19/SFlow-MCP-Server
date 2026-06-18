"""HTTP surface for the chat-user → API key resolver (PR 31).

Two callers:

  • The chat gateway calls ``POST /chat-user-keys/resolve`` per turn
    (or once per session) to look up which tenant-scoped key to use
    for a given chat user. This endpoint requires a *gateway* key —
    a role=tenant_admin key in the well-known "gateway" tenant, or
    any tenant_admin key. We don't add a separate role; the operator
    can use the tool_allowlist column to restrict the gateway key to
    just this endpoint.

  • A tenant admin calls ``POST /chat-user-keys`` to register a new
    chatbot user under their tenant. This mints the per-user key and
    returns the plaintext exactly once.

  • A tenant admin calls ``DELETE /chat-user-keys/{chat_user_id}`` to
    revoke a user's binding.
"""
from fastapi import APIRouter, Body, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from auth.context import TenantContext, VALID_ROLES, require_role
from db import get_db
from services.chat_user_keys import (
    create_user_key,
    resolve_user,
    revoke_user,
)

router = APIRouter(prefix="/chat-user-keys", tags=["chat-user-keys"])


@router.post("/resolve")
async def resolve(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    """Resolve a chat user → tenant + api_key_id (no plaintext)."""
    chat_user_id = payload.get("chat_user_id")
    if not chat_user_id or not isinstance(chat_user_id, str):
        raise HTTPException(
            status_code=400, detail="chat_user_id (string) is required"
        )
    result = await resolve_user(db, chat_user_id=chat_user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="no active mapping")
    return result


@router.post("")
async def create(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    """Register a chat user under the calling admin's tenant.

    Body:
      chat_user_id  — required string
      role          — one of viewer/analyst/operator/tenant_admin
      description   — optional freeform string
    """
    chat_user_id = payload.get("chat_user_id")
    role = payload.get("role", "viewer")
    description = payload.get("description")

    if not chat_user_id or not isinstance(chat_user_id, str):
        raise HTTPException(
            status_code=400, detail="chat_user_id (string) is required"
        )
    if role not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"role must be one of {sorted(VALID_ROLES)}",
        )

    return await create_user_key(
        db,
        tenant_id=ctx.tenant_id,
        chat_user_id=chat_user_id,
        role=role,
        description=description,
    )


@router.delete("/{chat_user_id}")
async def revoke(
    chat_user_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    ok = await revoke_user(
        db, tenant_id=ctx.tenant_id, chat_user_id=chat_user_id
    )
    if not ok:
        raise HTTPException(status_code=404, detail="mapping not found")
    return {"chat_user_id": chat_user_id, "status": "revoked"}
