"""In-process cache of (source_kind, source_identifier) → tenant_id.

Hit by every flow / counter / device-state record on the ingestion path,
so a per-record DB roundtrip would be too expensive. The cache refreshes
itself every TENANT_ROUTING_TTL_SECONDS — operators issue mappings via
scripts/seed.py and they take effect on the next tick.

Falls back to DEFAULT_TENANT_ID for any source not mapped explicitly.
This preserves single-tenant installs (which never seed a mapping) and
new deployments that haven't onboarded a source yet.
"""
from __future__ import annotations

import logging
import os
import time

from sqlalchemy import select

from db import AsyncSessionLocal
from db.models import DEFAULT_TENANT_ID, CollectorSource

log = logging.getLogger(__name__)

TENANT_ROUTING_TTL_SECONDS = int(os.getenv("TENANT_ROUTING_TTL_SECONDS", "60"))

SOURCE_KIND_SFLOW = "sflow"
SOURCE_KIND_GNMI = "gnmi"


class TenantRouter:
    """Cached lookup; safe to share across the ingest loops."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], str] = {}
        self._loaded_at: float = 0.0

    async def refresh(self) -> int:
        async with AsyncSessionLocal() as session:
            q = select(CollectorSource).where(CollectorSource.is_active.is_(True))
            rows = (await session.execute(q)).scalars().all()
        new_cache = {
            (row.source_kind, row.source_identifier): str(row.tenant_id) for row in rows
        }
        self._cache = new_cache
        self._loaded_at = time.monotonic()
        return len(new_cache)

    async def _maybe_refresh(self) -> None:
        if time.monotonic() - self._loaded_at >= TENANT_ROUTING_TTL_SECONDS:
            try:
                count = await self.refresh()
                log.debug(f"tenant_routing cache refreshed ({count} mappings)")
            except Exception as e:
                # Don't crash the ingest loop on a stale cache failure;
                # fall back to whatever was loaded last (or DEFAULT).
                log.warning(f"tenant_routing refresh failed: {e}")

    async def tenant_for(self, source_kind: str, source_identifier: str) -> str:
        await self._maybe_refresh()
        return self._cache.get(
            (source_kind, source_identifier), DEFAULT_TENANT_ID
        )


_router: TenantRouter | None = None


def get_router() -> TenantRouter:
    global _router
    if _router is None:
        _router = TenantRouter()
    return _router
