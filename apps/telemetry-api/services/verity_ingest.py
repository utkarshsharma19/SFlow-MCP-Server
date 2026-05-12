"""Verity intent ingest loop (PR 30).

Polls the Verity controller on ``VERITY_POLL_INTERVAL_SECONDS`` and
upserts ``device_intent`` / ``bgp_intent`` rows tagged ``source='verity'``.
Tenant routing reuses the ``collector_sources`` table — operators map
``verity:<controller_id>`` to a tenant via the seed CLI just like sFlow
agents and gNMI targets, and every row this loop writes is bound to
that tenant.

The loop is *additive only*: it never deletes intent rows. A real
orchestrator integration would also handle removals (interface deleted
from intent → row should go away), but that requires a stable
inventory id we don't yet model and would risk wiping out manually
seeded rows in mixed installs. Follow-up.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from collectors.verity_client import (
    DeclaredBGPPeer,
    DeclaredInterface,
    VerityClient,
)
from db import AsyncSessionLocal
from db.models import BGPIntent, DEFAULT_TENANT_ID, DeviceIntent
from services.rls_session import set_tenant
from services.tenant_routing import TenantRouter, get_router

log = logging.getLogger(__name__)

SOURCE_KIND_VERITY = "verity"
VERITY_POLL_INTERVAL_SECONDS = int(
    os.getenv("VERITY_POLL_INTERVAL_SECONDS", "300")
)
VERITY_CONTROLLER_ID = os.getenv("VERITY_CONTROLLER_ID", "default")


async def _tenant_for_controller(router: TenantRouter | None) -> str:
    if router is None:
        return DEFAULT_TENANT_ID
    return await router.tenant_for(SOURCE_KIND_VERITY, VERITY_CONTROLLER_ID)


async def ingest_once(
    db: AsyncSession,
    client: VerityClient,
    router: TenantRouter | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """One pull → upsert cycle. Returns counters for observability."""
    now = now or datetime.now(timezone.utc)
    tenant_id = await _tenant_for_controller(router)

    # All writes happen inside this tenant's RLS context — even though
    # the policy is permissive for the app role, future tightening
    # (refusing cross-tenant writes) shouldn't break this loop.
    await set_tenant(db, tenant_id)

    interfaces = await client.list_declared_interfaces()
    peers = await client.list_declared_bgp_peers()

    iface_count = await _upsert_interfaces(db, tenant_id, interfaces, now)
    bgp_count = await _upsert_bgp(db, tenant_id, peers, now)
    await db.commit()
    return {
        "tenant_id": tenant_id,
        "interfaces_upserted": iface_count,
        "bgp_peers_upserted": bgp_count,
        "as_of": now.isoformat(),
    }


async def _upsert_interfaces(
    db: AsyncSession,
    tenant_id: str,
    interfaces: list[DeclaredInterface],
    now: datetime,
) -> int:
    n = 0
    for decl in interfaces:
        stmt = (
            pg_insert(DeviceIntent)
            .values(
                tenant_id=tenant_id,
                device=decl.device,
                interface=decl.interface,
                expected_admin_status=decl.admin_status,
                expected_oper_status=decl.oper_status,
                expected_speed_bps=decl.speed_bps,
                expected_mtu=decl.mtu,
                expected_description=decl.description,
                source=SOURCE_KIND_VERITY,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_device_intent_iface",
                set_={
                    "expected_admin_status": decl.admin_status,
                    "expected_oper_status": decl.oper_status,
                    "expected_speed_bps": decl.speed_bps,
                    "expected_mtu": decl.mtu,
                    "expected_description": decl.description,
                    "source": SOURCE_KIND_VERITY,
                    "updated_at": now,
                },
            )
        )
        await db.execute(stmt)
        n += 1
    return n


async def _upsert_bgp(
    db: AsyncSession,
    tenant_id: str,
    peers: list[DeclaredBGPPeer],
    now: datetime,
) -> int:
    n = 0
    for decl in peers:
        stmt = (
            pg_insert(BGPIntent)
            .values(
                tenant_id=tenant_id,
                device=decl.device,
                peer_address=decl.peer_address,
                expected_peer_as=decl.peer_as,
                expected_session_state=decl.session_state,
                source=SOURCE_KIND_VERITY,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_bgp_intent_peer",
                set_={
                    "expected_peer_as": decl.peer_as,
                    "expected_session_state": decl.session_state,
                    "source": SOURCE_KIND_VERITY,
                    "updated_at": now,
                },
            )
        )
        await db.execute(stmt)
        n += 1
    return n


async def verity_ingest_loop() -> None:
    """Long-running loop entered from main.py's lifespan."""
    client = VerityClient()
    if not client.is_configured:
        log.info(
            "Verity collector idle — set VERITY_BASE_URL and VERITY_TOKEN to enable."
        )
        # Stay alive but quiet — leaving the task running and idle means
        # operators can flip the env in a config reload without
        # restarting the service.
        while True:
            await asyncio.sleep(VERITY_POLL_INTERVAL_SECONDS)
    router = get_router()
    try:
        while True:
            try:
                async with AsyncSessionLocal() as db:
                    summary = await ingest_once(db, client, router)
                    log.info("verity intent ingest: %s", summary)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("verity ingest tick failed")
            await asyncio.sleep(VERITY_POLL_INTERVAL_SECONDS)
    finally:
        await client.close()
