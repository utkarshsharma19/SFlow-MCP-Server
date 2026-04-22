"""Set Postgres session variables that drive row-level security (PR 27).

Every DB session used by a request binds ``app.tenant_id`` to the caller's
tenant before any query runs. The RLS policies added in migration 0008
use that variable to filter rows, so even a SQL-level bug that forgets
to ``WHERE tenant_id = :t`` will not leak cross-tenant rows.

Ingest paths — which legitimately need to write into multiple tenants in
a single worker — call :func:`bypass_rls` only inside a ``SET LOCAL``
block so the bypass never survives a commit.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def set_tenant(session: AsyncSession, tenant_id: str) -> None:
    """Bind ``app.tenant_id`` for the rest of the transaction.

    Uses ``SET LOCAL`` — the binding is scoped to the current transaction
    and is rolled back with it, so a failed request cannot leak tenant
    context into a reused session.
    """
    # asyncpg rejects ``SET LOCAL :name = :value`` because SET takes an
    # identifier, not a parameter. We pass the tenant via format_string
    # only after validating it as a UUID to close off injection.
    _ensure_uuid(tenant_id)
    await session.execute(text(f"SET LOCAL app.tenant_id = '{tenant_id}'"))


@asynccontextmanager
async def bypass_rls(session: AsyncSession) -> AsyncIterator[None]:
    """Temporarily drop tenant isolation inside a single transaction.

    Only ingest loops and admin tooling should use this; every call site
    must be auditable. The setting is cleared on exit.
    """
    await session.execute(text("SET LOCAL app.rls_bypass = 'on'"))
    try:
        yield
    finally:
        await session.execute(text("SET LOCAL app.rls_bypass = 'off'"))


def _ensure_uuid(value: str) -> None:
    import uuid

    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"tenant_id must be a UUID, got {value!r}") from exc
