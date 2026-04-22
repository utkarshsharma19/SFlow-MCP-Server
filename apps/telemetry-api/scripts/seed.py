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
from db.models import APIKey, CollectorSource, ECMPGroup, Tenant

VALID_SOURCE_KINDS = {"sflow", "gnmi"}


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


if __name__ == "__main__":
    main()
