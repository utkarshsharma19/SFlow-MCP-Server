"""Tenant + API key seeding CLI.

Operators use this to create additional tenants and issue API keys beyond
the default seeded by migration 0003. Plaintext keys are printed to stdout
once — never stored — so the operator is responsible for delivering them
securely.

Usage:
    python -m scripts.seed create-tenant --slug acme --name "Acme Corp"
    python -m scripts.seed create-key --tenant-slug acme --role analyst --name "alice-laptop"
"""
from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

from sqlalchemy import select

from auth.context import VALID_ROLES, hash_api_key
from db import AsyncSessionLocal
from db.models import APIKey, Tenant


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

    args = parser.parse_args()
    if args.cmd == "create-tenant":
        asyncio.run(_create_tenant(args.slug, args.name))
    elif args.cmd == "create-key":
        asyncio.run(_create_key(args.tenant_slug, args.role, args.name))


if __name__ == "__main__":
    main()
