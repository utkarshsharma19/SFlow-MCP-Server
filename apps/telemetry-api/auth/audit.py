"""Append-only audit logging.

Writes happen on a best-effort async path — audit failures must never
block the primary response. If the DB is down the middleware logs a
warning and moves on.
"""
from __future__ import annotations

import logging

from db import AsyncSessionLocal
from db.models import AuditLog

log = logging.getLogger(__name__)


async def record_access(
    *,
    tenant_id: str,
    api_key_id: str | None,
    role: str,
    method: str,
    path: str,
    status_code: int,
    scope: str | None = None,
    duration_ms: int | None = None,
) -> None:
    try:
        async with AsyncSessionLocal() as session:
            session.add(
                AuditLog(
                    tenant_id=tenant_id,
                    api_key_id=api_key_id,
                    role=role,
                    method=method,
                    path=path[:512],
                    status_code=status_code,
                    scope=scope,
                    duration_ms=duration_ms,
                )
            )
            await session.commit()
    except Exception as e:
        log.warning(f"audit write failed for {method} {path}: {e}")
