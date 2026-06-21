"""Tenant + API key + collector-source seeding CLI.

Operators use this to create additional tenants and issue API keys beyond
the default seeded by migration 0003. Plaintext keys are printed to stdout
once — never stored — so the operator is responsible for delivering them
securely.

PR 22 adds the source-mapping subcommands so the ingestion loops can
route per-device telemetry to the right tenant. PR 24 adds
`define-ecmp-group` so the imbalance detector can use operator-curated
member sets instead of the speed-based heuristic.

Usage:
    python -m scripts.seed create-tenant --slug acme --name "Acme Corp"
    python -m scripts.seed create-key --tenant-slug acme --role analyst --name "alice-laptop"
    python -m scripts.seed map-source --kind sflow --identifier 10.0.1.5 --tenant-slug acme
    python -m scripts.seed map-source --kind gnmi --identifier spine1 --tenant-slug acme
    python -m scripts.seed list-sources
    python -m scripts.seed define-ecmp-group --tenant-slug acme \
        --device leaf1 --group-name uplinks-to-spines \
        --members Ethernet49,Ethernet50,Ethernet51,Ethernet52
    python -m scripts.seed list-ecmp-groups
"""
from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

from sqlalchemy import select

from auth.context import VALID_ROLES, hash_api_key
from db import AsyncSessionLocal
from db.models import (
    APIKey,
    BGPIntent,
    CollectorSource,
    DeviceIntent,
    ECMPGroup,
    Tenant,
    WebhookSubscription,
)

VALID_SOURCE_KINDS = {"sflow", "gnmi", "verity"}


async def _create_tenant(slug: str, name: str) -> None:
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(select(Tenant).where(Tenant.slug == slug))
        ).scalar_one_or_none()
        if existing is not None:
            print(f"tenant '{slug}' already exists: id={existing.id}")
            return
        tenant = Tenant(slug=slug, name=name)
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        print(f"created tenant id={tenant.id} slug={slug}")


async def _create_key(tenant_slug: str, role: str, name: str) -> None:
    if role not in VALID_ROLES:
        print(f"error: role must be one of {sorted(VALID_ROLES)}", file=sys.stderr)
        sys.exit(2)

    async with AsyncSessionLocal() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == tenant_slug))
        ).scalar_one_or_none()
        if tenant is None:
            print(f"error: tenant '{tenant_slug}' not found", file=sys.stderr)
            sys.exit(2)

        plaintext = f"fm_{secrets.token_urlsafe(32)}"
        api_key = APIKey(
            tenant_id=tenant.id,
            key_hash=hash_api_key(plaintext),
            role=role,
            name=name,
        )
        session.add(api_key)
        await session.commit()
        await session.refresh(api_key)

        # Plaintext is printed once, here. It is not recoverable afterwards.
        print(f"created api_key id={api_key.id} tenant={tenant_slug} role={role}")
        print(f"KEY: {plaintext}")
        print("(store this securely — it cannot be retrieved again)")


async def _map_source(
    kind: str, identifier: str, tenant_slug: str, description: str | None
) -> None:
    if kind not in VALID_SOURCE_KINDS:
        print(f"error: kind must be one of {sorted(VALID_SOURCE_KINDS)}", file=sys.stderr)
        sys.exit(2)

    async with AsyncSessionLocal() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == tenant_slug))
        ).scalar_one_or_none()
        if tenant is None:
            print(f"error: tenant '{tenant_slug}' not found", file=sys.stderr)
            sys.exit(2)

        existing = (
            await session.execute(
                select(CollectorSource).where(
                    (CollectorSource.source_kind == kind)
                    & (CollectorSource.source_identifier == identifier)
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.tenant_id = tenant.id
            existing.is_active = True
            existing.description = description or existing.description
            await session.commit()
            print(
                f"updated mapping {kind}:{identifier} -> tenant={tenant_slug} "
                f"(id={existing.id})"
            )
            return

        src = CollectorSource(
            tenant_id=tenant.id,
            source_kind=kind,
            source_identifier=identifier,
            description=description,
        )
        session.add(src)
        await session.commit()
        await session.refresh(src)
        print(
            f"created mapping {kind}:{identifier} -> tenant={tenant_slug} (id={src.id})"
        )


async def _list_sources() -> None:
    async with AsyncSessionLocal() as session:
        q = (
            select(CollectorSource, Tenant)
            .join(Tenant, Tenant.id == CollectorSource.tenant_id)
            .order_by(CollectorSource.source_kind, CollectorSource.source_identifier)
        )
        rows = (await session.execute(q)).all()

    if not rows:
        print("(no collector source mappings — all sources fall back to default tenant)")
        return
    print(f"{'KIND':<8} {'IDENTIFIER':<40} {'TENANT':<24} ACTIVE")
    for src, tenant in rows:
        print(
            f"{src.source_kind:<8} {src.source_identifier:<40} "
            f"{tenant.slug:<24} {src.is_active}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="seed")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_tenant = sub.add_parser("create-tenant")
    p_tenant.add_argument("--slug", required=True)
    p_tenant.add_argument("--name", required=True)

    p_key = sub.add_parser("create-key")
    p_key.add_argument("--tenant-slug", required=True)
    p_key.add_argument("--role", required=True, help="viewer|analyst|operator|tenant_admin")
    p_key.add_argument("--name", required=True)

    p_map = sub.add_parser("map-source")
    p_map.add_argument("--kind", required=True, help="sflow|gnmi")
    p_map.add_argument("--identifier", required=True, help="agent IP / hostname")
    p_map.add_argument("--tenant-slug", required=True)
    p_map.add_argument("--description", required=False, default=None)

    sub.add_parser("list-sources")

    p_intent = sub.add_parser(
        "set-intent",
        help="declare expected state for a device's interface",
    )
    p_intent.add_argument("--tenant-slug", required=True)
    p_intent.add_argument("--device", required=True)
    p_intent.add_argument("--interface", required=True)
    p_intent.add_argument("--admin-status", required=False, default=None)
    p_intent.add_argument("--oper-status", required=False, default=None)
    p_intent.add_argument("--speed-bps", type=int, required=False, default=None)
    p_intent.add_argument("--mtu", type=int, required=False, default=None)
    p_intent.add_argument("--description", required=False, default=None)
    p_intent.add_argument(
        "--source", required=False, default="manual",
        help="verity|yaml|manual (free-form label)",
    )

    p_bgp_intent = sub.add_parser(
        "set-bgp-intent",
        help="declare expected BGP peer state on a device",
    )
    p_bgp_intent.add_argument("--tenant-slug", required=True)
    p_bgp_intent.add_argument("--device", required=True)
    p_bgp_intent.add_argument("--peer-address", required=True)
    p_bgp_intent.add_argument("--peer-as", type=int, required=False, default=None)
    p_bgp_intent.add_argument(
        "--session-state",
        required=False,
        default=None,
        help="usually ESTABLISHED",
    )
    p_bgp_intent.add_argument("--source", required=False, default="manual")

    p_webhook = sub.add_parser(
        "create-webhook",
        help="register a critical-anomaly webhook subscription",
    )
    p_webhook.add_argument("--tenant-slug", required=True)
    p_webhook.add_argument("--target-url", required=True)
    p_webhook.add_argument(
        "--severity-min",
        required=False,
        default="critical",
        help="low|medium|high|critical",
    )
    p_webhook.add_argument("--description", required=False, default=None)
    p_webhook.add_argument(
        "--secret",
        required=False,
        default=None,
        help="HMAC secret. Auto-generated if omitted.",
    )

    args = parser.parse_args()
    if args.cmd == "create-tenant":
        asyncio.run(_create_tenant(args.slug, args.name))
    elif args.cmd == "create-key":
        asyncio.run(_create_key(args.tenant_slug, args.role, args.name))
    elif args.cmd == "map-source":
        asyncio.run(
            _map_source(args.kind, args.identifier, args.tenant_slug, args.description)
        )
    elif args.cmd == "list-sources":
        asyncio.run(_list_sources())
    elif args.cmd == "set-intent":
        asyncio.run(
            _set_intent(
                tenant_slug=args.tenant_slug,
                device=args.device,
                interface=args.interface,
                admin_status=args.admin_status,
                oper_status=args.oper_status,
                speed_bps=args.speed_bps,
                mtu=args.mtu,
                description=args.description,
                source=args.source,
            )
        )
    elif args.cmd == "set-bgp-intent":
        asyncio.run(
            _set_bgp_intent(
                tenant_slug=args.tenant_slug,
                device=args.device,
                peer_address=args.peer_address,
                peer_as=args.peer_as,
                session_state=args.session_state,
                source=args.source,
            )
        )
    elif args.cmd == "create-webhook":
        asyncio.run(
            _create_webhook(
                tenant_slug=args.tenant_slug,
                target_url=args.target_url,
                severity_min=args.severity_min,
                description=args.description,
                secret_plaintext=args.secret,
            )
        )


async def _create_webhook(
    *,
    tenant_slug: str,
    target_url: str,
    severity_min: str,
    description: str | None,
    secret_plaintext: str | None,
) -> None:
    from services.crypto import store_secret
    from services.rls_session import set_tenant

    if severity_min not in {"low", "medium", "high", "critical"}:
        print(
            "error: severity_min must be low|medium|high|critical",
            file=sys.stderr,
        )
        sys.exit(2)

    plaintext = secret_plaintext or f"whk_{secrets.token_urlsafe(32)}"
    secret_ref = f"webhook-{secrets.token_hex(8)}"

    async with AsyncSessionLocal() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == tenant_slug))
        ).scalar_one_or_none()
        if tenant is None:
            print(f"error: tenant '{tenant_slug}' not found", file=sys.stderr)
            sys.exit(2)

        await set_tenant(session, str(tenant.id))

        await store_secret(
            session,
            tenant_id=str(tenant.id),
            kind="webhook_secret",
            ref=secret_ref,
            plaintext=plaintext,
        )
        sub = WebhookSubscription(
            tenant_id=tenant.id,
            target_url=target_url,
            secret_ref=secret_ref,
            severity_min=severity_min,
            description=description,
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        print(f"created webhook subscription id={sub.id} secret_ref={secret_ref}")
        print(f"SECRET: {plaintext}")
        print(
            "(verify deliveries with: SELECT signature header sha256=HMAC-SHA256(secret, body))"
        )


async def _set_intent(
    *,
    tenant_slug: str,
    device: str,
    interface: str,
    admin_status: str | None,
    oper_status: str | None,
    speed_bps: int | None,
    mtu: int | None,
    description: str | None,
    source: str,
) -> None:
    async with AsyncSessionLocal() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == tenant_slug))
        ).scalar_one_or_none()
        if tenant is None:
            print(f"error: tenant '{tenant_slug}' not found", file=sys.stderr)
            sys.exit(2)

        from services.rls_session import set_tenant

        await set_tenant(session, str(tenant.id))

        existing = (
            await session.execute(
                select(DeviceIntent)
                .where(DeviceIntent.tenant_id == tenant.id)
                .where(DeviceIntent.device == device)
                .where(DeviceIntent.interface == interface)
            )
        ).scalar_one_or_none()
        if existing is None:
            row = DeviceIntent(
                tenant_id=tenant.id,
                device=device,
                interface=interface,
                expected_admin_status=admin_status,
                expected_oper_status=oper_status,
                expected_speed_bps=speed_bps,
                expected_mtu=mtu,
                expected_description=description,
                source=source,
            )
            session.add(row)
            await session.commit()
            print(f"created intent for {device}/{interface} (source={source})")
            return
        existing.expected_admin_status = admin_status
        existing.expected_oper_status = oper_status
        existing.expected_speed_bps = speed_bps
        existing.expected_mtu = mtu
        existing.expected_description = description
        existing.source = source
        await session.commit()
        print(f"updated intent for {device}/{interface}")


async def _set_bgp_intent(
    *,
    tenant_slug: str,
    device: str,
    peer_address: str,
    peer_as: int | None,
    session_state: str | None,
    source: str,
) -> None:
    async with AsyncSessionLocal() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == tenant_slug))
        ).scalar_one_or_none()
        if tenant is None:
            print(f"error: tenant '{tenant_slug}' not found", file=sys.stderr)
            sys.exit(2)

        from services.rls_session import set_tenant

        await set_tenant(session, str(tenant.id))

        existing = (
            await session.execute(
                select(BGPIntent)
                .where(BGPIntent.tenant_id == tenant.id)
                .where(BGPIntent.device == device)
                .where(BGPIntent.peer_address == peer_address)
            )
        ).scalar_one_or_none()
        if existing is None:
            row = BGPIntent(
                tenant_id=tenant.id,
                device=device,
                peer_address=peer_address,
                expected_peer_as=peer_as,
                expected_session_state=session_state,
                source=source,
            )
            session.add(row)
            await session.commit()
            print(
                f"created BGP intent for {device} peer={peer_address} "
                f"(source={source})"
            )
            return
        existing.expected_peer_as = peer_as
        existing.expected_session_state = session_state
        existing.source = source
        await session.commit()
        print(f"updated BGP intent for {device} peer={peer_address}")


if __name__ == "__main__":
    main()
