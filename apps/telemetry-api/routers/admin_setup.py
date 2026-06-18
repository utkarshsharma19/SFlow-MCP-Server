"""Admin setup endpoints (PR 31) — HTTP equivalents of seed.py.

Without these, every tenant/source/ECMP/intent/webhook operation needs
shell access to the container. For multi-operator deployments that's a
ceiling on velocity: ops can't onboard a new switch without a deploy
engineer, and the chatbot can't drive its own setup wizard.

The split with ``admin.py``:

* ``admin.py``        — anomaly lifecycle, API key CRUD, freshness peek.
* ``admin_setup.py``  — every CRUD that today lives in seed.py:
    tenants, collector sources, ECMP groups, device intent, BGP intent,
    webhook subscriptions.

Tenant CRUD is the one operation that bypasses RLS (the caller may not
be a member of the tenant they are creating). It requires a special
``platform_admin`` capability that the tenant_admin role doesn't carry
— in practice we check for the well-known platform-admin tenant id via
env, since there is no role above tenant_admin yet. Treat that as
provisional: a future PR introduces a proper platform-admin role.
"""
from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.context import TenantContext, VALID_ROLES, require_role
from db import get_db
from db.models import (
    BGPIntent,
    CollectorSource,
    DeviceIntent,
    ECMPGroup,
    Tenant,
    WebhookSubscription,
)
from services.crypto import store_secret
from services.rls_session import bypass_rls

router = APIRouter(prefix="/admin", tags=["admin-setup"])

# Well-known tenant id whose admins can manage other tenants. Provisional
# until a platform-admin role exists. Set via env on the telemetry-api.
PLATFORM_ADMIN_TENANT_ID = os.getenv("PLATFORM_ADMIN_TENANT_ID")


# ---------------------------------------------------------------------------
# Platform-level: tenant CRUD
# ---------------------------------------------------------------------------

def _require_platform_admin(ctx: TenantContext) -> None:
    if not PLATFORM_ADMIN_TENANT_ID:
        raise HTTPException(
            status_code=503,
            detail=(
                "platform-admin endpoint disabled: set "
                "PLATFORM_ADMIN_TENANT_ID on the telemetry-api"
            ),
        )
    if ctx.tenant_id != PLATFORM_ADMIN_TENANT_ID:
        raise HTTPException(
            status_code=403,
            detail="platform-admin operations require the platform-admin tenant",
        )


@router.post("/tenants")
async def create_tenant(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    """Platform-admin only. Idempotent on slug."""
    _require_platform_admin(ctx)
    slug = payload.get("slug")
    name = payload.get("name")
    if not slug or not name:
        raise HTTPException(status_code=400, detail="slug and name are required")
    async with bypass_rls(db):
        existing = (
            await db.execute(select(Tenant).where(Tenant.slug == slug))
        ).scalar_one_or_none()
        if existing is not None:
            return _serialize_tenant(existing)
        row = Tenant(slug=slug, name=name)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return _serialize_tenant(row)


@router.get("/tenants")
async def list_tenants(
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    _require_platform_admin(ctx)
    async with bypass_rls(db):
        rows = (
            await db.execute(select(Tenant).order_by(Tenant.slug))
        ).scalars().all()
    return {"tenants": [_serialize_tenant(r) for r in rows]}


@router.delete("/tenants/{tenant_id}")
async def deactivate_tenant(
    tenant_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    """Soft delete (is_active=false). Real delete cascades data; refuse
    that destructive path here — operators can run it from psql if needed."""
    _require_platform_admin(ctx)
    async with bypass_rls(db):
        result = await db.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(is_active=False)
        )
        await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="tenant not found")
    return {"tenant_id": tenant_id, "status": "deactivated"}


# ---------------------------------------------------------------------------
# Collector source mapping (sflow / gnmi / verity → tenant)
# ---------------------------------------------------------------------------

VALID_SOURCE_KINDS = {"sflow", "gnmi", "verity"}


@router.post("/sources")
async def upsert_source(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    """Map a source kind+identifier to the calling admin's tenant.

    Replaces an existing mapping for the same (kind, identifier) —
    that's the seed.py semantics. So an operator who realizes a switch
    was assigned to the wrong tenant flips the mapping with one call
    and ingest re-routes on the next cache tick.
    """
    kind = payload.get("kind")
    identifier = payload.get("identifier")
    description = payload.get("description")
    if kind not in VALID_SOURCE_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {sorted(VALID_SOURCE_KINDS)}",
        )
    if not identifier:
        raise HTTPException(status_code=400, detail="identifier is required")

    async with bypass_rls(db):
        existing = (
            await db.execute(
                select(CollectorSource)
                .where(CollectorSource.source_kind == kind)
                .where(CollectorSource.source_identifier == identifier)
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.tenant_id = ctx.tenant_id
            existing.is_active = True
            if description is not None:
                existing.description = description
            await db.commit()
            await db.refresh(existing)
            return _serialize_source(existing)
        row = CollectorSource(
            tenant_id=ctx.tenant_id,
            source_kind=kind,
            source_identifier=identifier,
            description=description,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return _serialize_source(row)


@router.get("/sources")
async def list_sources(
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    q = (
        select(CollectorSource)
        .where(CollectorSource.tenant_id == ctx.tenant_id)
        .order_by(CollectorSource.source_kind, CollectorSource.source_identifier)
    )
    rows = (await db.execute(q)).scalars().all()
    return {"sources": [_serialize_source(r) for r in rows]}


@router.delete("/sources/{source_id}")
async def deactivate_source(
    source_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    result = await db.execute(
        update(CollectorSource)
        .where(CollectorSource.id == source_id)
        .where(CollectorSource.tenant_id == ctx.tenant_id)
        .values(is_active=False)
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="source not found")
    return {"source_id": source_id, "status": "deactivated"}


# ---------------------------------------------------------------------------
# ECMP groups
# ---------------------------------------------------------------------------

@router.post("/ecmp-groups")
async def upsert_ecmp_group(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    """Declare an operator-curated ECMP member set.

    Replaces the speed-inferred heuristic in detect_fabric_imbalance
    for this (tenant, device, group_name). Members is a list of
    interface names; empty list is allowed (group temporarily
    disabled).
    """
    device = payload.get("device")
    group_name = payload.get("group_name")
    members = payload.get("members")
    description = payload.get("description")
    if not device or not group_name:
        raise HTTPException(
            status_code=400, detail="device and group_name are required"
        )
    if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
        raise HTTPException(
            status_code=400, detail="members must be a list of interface name strings"
        )

    existing = (
        await db.execute(
            select(ECMPGroup)
            .where(ECMPGroup.tenant_id == ctx.tenant_id)
            .where(ECMPGroup.device == device)
            .where(ECMPGroup.group_name == group_name)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.members = members
        if description is not None:
            existing.description = description
        await db.commit()
        await db.refresh(existing)
        return _serialize_ecmp(existing)

    row = ECMPGroup(
        tenant_id=ctx.tenant_id,
        device=device,
        group_name=group_name,
        members=members,
        description=description,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _serialize_ecmp(row)


@router.get("/ecmp-groups")
async def list_ecmp_groups(
    device: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    q = (
        select(ECMPGroup)
        .where(ECMPGroup.tenant_id == ctx.tenant_id)
        .order_by(ECMPGroup.device, ECMPGroup.group_name)
    )
    if device:
        q = q.where(ECMPGroup.device == device)
    rows = (await db.execute(q)).scalars().all()
    return {"ecmp_groups": [_serialize_ecmp(r) for r in rows]}


@router.delete("/ecmp-groups/{group_id}")
async def delete_ecmp_group(
    group_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    result = await db.execute(
        update(ECMPGroup)
        .where(ECMPGroup.id == group_id)
        .where(ECMPGroup.tenant_id == ctx.tenant_id)
        .values(members=[])  # soft-disable; keep history
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="ecmp group not found")
    return {"group_id": group_id, "status": "members_cleared"}


# ---------------------------------------------------------------------------
# Device intent (manual; Verity collector writes the same table)
# ---------------------------------------------------------------------------

@router.post("/intent/device")
async def upsert_device_intent(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    """Set per-interface expected state. NULL fields = no opinion."""
    device = payload.get("device")
    interface = payload.get("interface")
    if not device or not interface:
        raise HTTPException(
            status_code=400, detail="device and interface are required"
        )
    existing = (
        await db.execute(
            select(DeviceIntent)
            .where(DeviceIntent.tenant_id == ctx.tenant_id)
            .where(DeviceIntent.device == device)
            .where(DeviceIntent.interface == interface)
        )
    ).scalar_one_or_none()
    fields = {
        "expected_admin_status": payload.get("expected_admin_status"),
        "expected_oper_status": payload.get("expected_oper_status"),
        "expected_speed_bps": payload.get("expected_speed_bps"),
        "expected_mtu": payload.get("expected_mtu"),
        "expected_description": payload.get("expected_description"),
        "source": payload.get("source", "manual"),
        "notes": payload.get("notes"),
    }
    if existing is not None:
        for k, v in fields.items():
            setattr(existing, k, v)
        await db.commit()
        await db.refresh(existing)
        row = existing
    else:
        row = DeviceIntent(
            tenant_id=ctx.tenant_id,
            device=device,
            interface=interface,
            **fields,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return _serialize_device_intent(row)


@router.post("/intent/bgp")
async def upsert_bgp_intent(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    device = payload.get("device")
    peer_address = payload.get("peer_address")
    if not device or not peer_address:
        raise HTTPException(
            status_code=400, detail="device and peer_address are required"
        )
    existing = (
        await db.execute(
            select(BGPIntent)
            .where(BGPIntent.tenant_id == ctx.tenant_id)
            .where(BGPIntent.device == device)
            .where(BGPIntent.peer_address == peer_address)
        )
    ).scalar_one_or_none()
    fields = {
        "expected_peer_as": payload.get("expected_peer_as"),
        "expected_session_state": payload.get("expected_session_state"),
        "source": payload.get("source", "manual"),
        "notes": payload.get("notes"),
    }
    if existing is not None:
        for k, v in fields.items():
            setattr(existing, k, v)
        await db.commit()
        await db.refresh(existing)
        row = existing
    else:
        row = BGPIntent(
            tenant_id=ctx.tenant_id,
            device=device,
            peer_address=peer_address,
            **fields,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return _serialize_bgp_intent(row)


# ---------------------------------------------------------------------------
# Webhook subscriptions
# ---------------------------------------------------------------------------

@router.post("/webhooks")
async def create_webhook(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    """Register a webhook. Auto-mints + stores the HMAC secret;
    returns the plaintext exactly once."""
    target_url = payload.get("target_url")
    severity_min = payload.get("severity_min", "critical")
    description = payload.get("description")
    secret_plaintext = payload.get("secret") or f"whk_{secrets.token_urlsafe(32)}"

    if not target_url:
        raise HTTPException(status_code=400, detail="target_url is required")
    if severity_min not in {"low", "medium", "high", "critical"}:
        raise HTTPException(
            status_code=400, detail="severity_min must be low|medium|high|critical"
        )

    secret_ref = f"webhook-{secrets.token_hex(8)}"
    await store_secret(
        db,
        tenant_id=ctx.tenant_id,
        kind="webhook_secret",
        ref=secret_ref,
        plaintext=secret_plaintext,
    )
    row = WebhookSubscription(
        tenant_id=ctx.tenant_id,
        target_url=target_url,
        secret_ref=secret_ref,
        severity_min=severity_min,
        description=description,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {
        **_serialize_webhook(row),
        "secret": secret_plaintext,
        "secret_note": "Store this — the body signature is HMAC-SHA256(secret, body).",
    }


@router.get("/webhooks")
async def list_webhooks(
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    rows = (
        await db.execute(
            select(WebhookSubscription)
            .where(WebhookSubscription.tenant_id == ctx.tenant_id)
            .order_by(WebhookSubscription.created_at.desc())
        )
    ).scalars().all()
    return {"webhooks": [_serialize_webhook(r) for r in rows]}


@router.delete("/webhooks/{webhook_id}")
async def deactivate_webhook(
    webhook_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
    ctx: TenantContext = Depends(require_role("tenant_admin")),
):
    result = await db.execute(
        update(WebhookSubscription)
        .where(WebhookSubscription.id == webhook_id)
        .where(WebhookSubscription.tenant_id == ctx.tenant_id)
        .values(is_active=False)
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="webhook not found")
    return {"webhook_id": webhook_id, "status": "deactivated"}


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _serialize_tenant(t: Tenant) -> dict:
    return {
        "id": str(t.id),
        "slug": t.slug,
        "name": t.name,
        "is_active": bool(t.is_active),
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _serialize_source(s: CollectorSource) -> dict:
    return {
        "id": str(s.id),
        "tenant_id": str(s.tenant_id),
        "source_kind": s.source_kind,
        "source_identifier": s.source_identifier,
        "description": s.description,
        "is_active": bool(s.is_active),
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _serialize_ecmp(g: ECMPGroup) -> dict:
    return {
        "id": str(g.id),
        "device": g.device,
        "group_name": g.group_name,
        "members": list(g.members or []),
        "description": g.description,
        "created_at": g.created_at.isoformat() if g.created_at else None,
    }


def _serialize_device_intent(r: DeviceIntent) -> dict:
    return {
        "id": str(r.id),
        "device": r.device,
        "interface": r.interface,
        "expected_admin_status": r.expected_admin_status,
        "expected_oper_status": r.expected_oper_status,
        "expected_speed_bps": (
            int(r.expected_speed_bps) if r.expected_speed_bps is not None else None
        ),
        "expected_mtu": (
            int(r.expected_mtu) if r.expected_mtu is not None else None
        ),
        "expected_description": r.expected_description,
        "source": r.source,
        "notes": r.notes,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _serialize_bgp_intent(r: BGPIntent) -> dict:
    return {
        "id": str(r.id),
        "device": r.device,
        "peer_address": r.peer_address,
        "expected_peer_as": (
            int(r.expected_peer_as) if r.expected_peer_as is not None else None
        ),
        "expected_session_state": r.expected_session_state,
        "source": r.source,
        "notes": r.notes,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _serialize_webhook(w: WebhookSubscription) -> dict:
    return {
        "id": str(w.id),
        "target_url": w.target_url,
        "severity_min": w.severity_min,
        "is_active": bool(w.is_active),
        "description": w.description,
        "consecutive_failures": int(w.consecutive_failures or 0),
        "last_success_at": (
            w.last_success_at.isoformat() if w.last_success_at else None
        ),
        "last_failure_at": (
            w.last_failure_at.isoformat() if w.last_failure_at else None
        ),
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }
