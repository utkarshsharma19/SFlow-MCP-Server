"""Resolve a chatbot user id → tenant-scoped API key (PR 31).

The chat gateway is a single trusted process. It holds a high-privilege
*gateway key* that is used ONLY to call ``/chat-user-keys/resolve`` —
which then returns a per-user, tenant-scoped key. Every subsequent
FlowMind call the gateway makes for that user uses the per-user key,
so the tenant_id/audit/quota path stays correct.

Threat model:

* Compromising the gateway key gives an attacker the ability to mint
  arbitrary user-scoped keys *within the tenants whose admins have
  already created mappings*. They cannot create new mappings — that
  requires a tenant_admin key for the target tenant.

* The per-user key itself is a normal ``api_keys`` row (sha256-hashed
  on disk). We never store its plaintext; the resolver returns the
  plaintext only at *creation* time (matching ``seed.py create-key``).
  After that, the gateway must store the plaintext itself.

That second point means the gateway needs a small persistent cache:
once it has resolved a user, it remembers the plaintext key for that
user. If the cache is lost, the user's mapping has to be re-created.
This is intentional — we trade a small UX cost for "the plaintext key
exists nowhere except where it is used".
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.context import hash_api_key
from db.models import APIKey, ChatUserKey
from services.rls_session import bypass_rls


KEY_PLAINTEXT_PREFIX = "fmu_"  # FlowMind User — distinguish from fm_ admin keys


async def resolve_user(
    db: AsyncSession,
    *,
    chat_user_id: str,
    now: Optional[datetime] = None,
) -> dict | None:
    """Look up the (tenant, api_key_id) for a chat user.

    Returns ``None`` if the user has no active mapping. The plaintext
    key is *not* in the response — by design, we never have it. The
    gateway must have cached it from the ``create_user_key`` response.
    """
    now = now or datetime.now(timezone.utc)
    async with bypass_rls(db):
        row = (
            await db.execute(
                select(ChatUserKey)
                .where(ChatUserKey.chat_user_id == chat_user_id)
                .where(ChatUserKey.is_active.is_(True))
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        await db.execute(
            update(ChatUserKey)
            .where(ChatUserKey.id == row.id)
            .values(last_resolved_at=now)
        )
        await db.commit()
        return {
            "chat_user_id": row.chat_user_id,
            "tenant_id": str(row.tenant_id),
            "api_key_id": str(row.api_key_id),
        }


async def create_user_key(
    db: AsyncSession,
    *,
    tenant_id: str,
    chat_user_id: str,
    role: str,
    description: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Mint a new chatbot-user API key and bind it to a chat user.

    Returns the plaintext key in the response. The caller (a tenant
    admin via the admin router) is responsible for delivering it to
    the chat gateway securely. We never store the plaintext.

    Idempotency: re-running this for the same (chat_user_id, tenant)
    deactivates the old binding and mints a fresh key. The old
    ``api_keys`` row stays alive (and active) until you separately
    revoke it — that's what ``rotated_from_id`` is for.
    """
    now = now or datetime.now(timezone.utc)
    plaintext = f"{KEY_PLAINTEXT_PREFIX}{secrets.token_urlsafe(32)}"
    key_hash = hash_api_key(plaintext)
    key_prefix = plaintext[:8]

    # Atomicity: flush the new APIKey so it gets a server-assigned id
    # we can reference in the ChatUserKey row, but do NOT commit yet.
    # The whole operation (key + mapping) lands in one transaction —
    # if the mapping insert fails, the rollback also discards the
    # never-used APIKey row, avoiding an orphaned active key.
    new_key = APIKey(
        tenant_id=tenant_id,
        key_hash=key_hash,
        key_prefix=key_prefix,
        role=role,
        name=f"chat-user:{chat_user_id}",
    )
    db.add(new_key)
    await db.flush()

    # Deactivate the prior mapping (if any) and add the new one.
    existing = (
        await db.execute(
            select(ChatUserKey)
            .where(ChatUserKey.chat_user_id == chat_user_id)
            .where(ChatUserKey.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.api_key_id = new_key.id
        existing.is_active = True
        existing.description = description or existing.description
        existing.last_resolved_at = None
    else:
        db.add(
            ChatUserKey(
                chat_user_id=chat_user_id,
                tenant_id=tenant_id,
                api_key_id=new_key.id,
                description=description,
            )
        )
    await db.commit()
    await db.refresh(new_key)

    return {
        "chat_user_id": chat_user_id,
        "tenant_id": tenant_id,
        "api_key_id": str(new_key.id),
        "api_key": plaintext,
        "role": role,
        "key_prefix": key_prefix,
    }


async def revoke_user(
    db: AsyncSession,
    *,
    tenant_id: str,
    chat_user_id: str,
) -> bool:
    """Disable the (chat_user_id, tenant_id) mapping. Idempotent."""
    result = await db.execute(
        update(ChatUserKey)
        .where(ChatUserKey.tenant_id == tenant_id)
        .where(ChatUserKey.chat_user_id == chat_user_id)
        .values(is_active=False)
    )
    await db.commit()
    return result.rowcount > 0
