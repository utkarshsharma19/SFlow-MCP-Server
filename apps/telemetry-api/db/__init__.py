import os

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://flowmind:flowmind_dev@localhost/flowmind",
)

engine = create_async_engine(DATABASE_URL, echo=False, pool_size=10)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db(request: Request = None) -> AsyncSession:
    """Yield a session with ``app.tenant_id`` bound for RLS.

    When called from an authenticated route, the APIKey middleware has
    already populated ``request.state.tenant_ctx``; we bind that tenant
    onto every transaction so the Postgres RLS policies filter rows.
    Routes that legitimately cross tenants (admin backoffice) must use
    ``bypass_rls`` explicitly.
    """
    async with AsyncSessionLocal() as session:
        ctx = getattr(getattr(request, "state", None), "tenant_ctx", None) if request else None
        if ctx is not None:
            from services.rls_session import set_tenant

            await set_tenant(session, ctx.tenant_id)
        yield session
