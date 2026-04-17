"""API-key authentication middleware (DB-backed, tenant + role aware).

Replaces the static-key implementation from PR 11. Keys are looked up
by sha256 hash in the `api_keys` table, must belong to an active tenant,
and must carry one of the four canonical roles. On a valid request, a
`TenantContext` is attached to `request.state.tenant_ctx` and an audit
record is written after the response completes.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from starlette.middleware.base import BaseHTTPMiddleware

from auth.audit import record_access
from auth.context import TenantContext, hash_api_key
from db import AsyncSessionLocal
from db.models import APIKey, Tenant

log = logging.getLogger(__name__)

EXEMPT_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}


class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if not key:
            return JSONResponse(
                status_code=401,
                content={"error": "missing X-API-Key"},
            )

        ctx = await _lookup_context(key)
        if ctx is None:
            return JSONResponse(
                status_code=401,
                content={"error": "invalid or inactive API key"},
            )

        request.state.tenant_ctx = ctx
        started = time.monotonic()
        response = await call_next(request)
        duration_ms = int((time.monotonic() - started) * 1000)

        # Audit the access. Don't block the response if this fails.
        await record_access(
            tenant_id=ctx.tenant_id,
            api_key_id=ctx.api_key_id,
            role=ctx.role,
            method=request.method,
            path=str(request.url.path),
            status_code=response.status_code,
            scope=request.query_params.get("scope"),
            duration_ms=duration_ms,
        )
        return response


async def _lookup_context(key: str) -> TenantContext | None:
    key_hash = hash_api_key(key)
    async with AsyncSessionLocal() as session:
        q = (
            select(APIKey, Tenant)
            .join(Tenant, Tenant.id == APIKey.tenant_id)
            .where(APIKey.key_hash == key_hash)
            .where(APIKey.is_active.is_(True))
            .where(Tenant.is_active.is_(True))
        )
        row = (await session.execute(q)).first()
        if row is None:
            return None
        api_key, tenant = row

        # Fire-and-forget last_used_at bump. Outside the read transaction
        # so a slow write can't stall auth.
        await session.execute(
            update(APIKey)
            .where(APIKey.id == api_key.id)
            .values(last_used_at=datetime.now(timezone.utc))
        )
        await session.commit()

        return TenantContext(
            tenant_id=str(tenant.id),
            api_key_id=str(api_key.id),
            role=api_key.role,
        )
