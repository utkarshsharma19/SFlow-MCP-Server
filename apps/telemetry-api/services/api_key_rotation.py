"""API key lifecycle: mint, rotate, revoke (PR 27)."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from auth.context import hash_api_key
from db.models import APIKey

KEY_PREFIX = "fm_live_"


@dataclass(frozen=True)
class MintedKey:
    id: str
    plaintext: str      # shown once at creation, never persisted
    prefix: str
    expires_at: datetime | None


def _generate_key() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    key = f"{KEY_PREFIX}{raw}"
    prefix = key[:8]
    return key, prefix


async def mint_key(
    db: AsyncSession,
    *,
    tenant_id: str,
    role: str,
    name: str,
    tool_allowlist: Sequence[str] | None = None,
    rate_limit_per_minute: int | None = None,
    ttl: timedelta | None = None,
) -> MintedKey:
    key, prefix = _generate_key()
    key_hash = hash_api_key(key)
    expires_at = datetime.now(timezone.utc) + ttl if ttl else None

    row = (
        await db.execute(
            pg_insert(APIKey)
            .values(
                tenant_id=tenant_id,
                key_hash=key_hash,
                key_prefix=prefix,
                role=role,
                name=name,
                tool_allowlist=list(tool_allowlist) if tool_allowlist else None,
                rate_limit_per_minute=rate_limit_per_minute,
                expires_at=expires_at,
            )
            .returning(APIKey.id)
        )
    ).one()
    await db.commit()
    return MintedKey(
        id=str(row.id),
        plaintext=key,
        prefix=prefix,
        expires_at=expires_at,
    )


async def rotate_key(
    db: AsyncSession,
    *,
    tenant_id: str,
    old_key_id: str,
    grace: timedelta = timedelta(hours=24),
) -> MintedKey:
    """Mint a new key that inherits the old key's scope, then age out the old.

    The old key stays active for ``grace`` so callers can swap without an
    outage. ``rotated_from_id`` on the new row keeps the audit chain.
    """
    existing = (
        await db.execute(
            APIKey.__table__.select()
            .where(APIKey.id == old_key_id)
            .where(APIKey.tenant_id == tenant_id)
        )
    ).first()
    if existing is None:
        raise ValueError("old_key_id not found in tenant")

    key, prefix = _generate_key()
    key_hash = hash_api_key(key)
    now = datetime.now(timezone.utc)
    minted = (
        await db.execute(
            pg_insert(APIKey)
            .values(
                tenant_id=tenant_id,
                key_hash=key_hash,
                key_prefix=prefix,
                role=existing.role,
                name=f"{existing.name} (rotated {now.date()})",
                tool_allowlist=existing.tool_allowlist,
                rate_limit_per_minute=existing.rate_limit_per_minute,
                rotated_from_id=old_key_id,
                # New key inherits whatever ttl the old one had, or none.
                expires_at=existing.expires_at,
            )
            .returning(APIKey.id)
        )
    ).one()

    # Age the old key out.
    await db.execute(
        update(APIKey)
        .where(APIKey.id == old_key_id)
        .values(expires_at=now + grace)
    )
    await db.commit()
    return MintedKey(id=str(minted.id), plaintext=key, prefix=prefix, expires_at=existing.expires_at)


async def revoke_key(
    db: AsyncSession, *, tenant_id: str, key_id: str
) -> bool:
    result = await db.execute(
        update(APIKey)
        .where(APIKey.id == key_id)
        .where(APIKey.tenant_id == tenant_id)
        .values(is_active=False, expires_at=datetime.now(timezone.utc))
    )
    await db.commit()
    return result.rowcount > 0
