"""Tenant + role context carried through every authenticated request.

The middleware populates `request.state.tenant_ctx`; routers pull it via
the `get_tenant_context` FastAPI dependency and pass the tenant_id into
service-layer queries. Role enforcement uses `require_role(min_role)`.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

ROLE_RANK = {
    "viewer": 1,
    "analyst": 2,
    "operator": 3,
    "tenant_admin": 4,
}

VALID_ROLES = set(ROLE_RANK)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    api_key_id: str
    role: str
    # PR 27 — per-key scoping. ``tool_allowlist is None`` means unrestricted.
    tool_allowlist: tuple[str, ...] | None = None
    rate_limit_per_minute: int | None = None

    def has_role(self, min_role: str) -> bool:
        needed = ROLE_RANK.get(min_role)
        have = ROLE_RANK.get(self.role)
        if needed is None or have is None:
            return False
        return have >= needed

    def may_call_tool(self, tool_name: str) -> bool:
        if self.tool_allowlist is None:
            return True
        return tool_name in self.tool_allowlist


def get_tenant_context(request: Request) -> TenantContext:
    ctx = getattr(request.state, "tenant_ctx", None)
    if ctx is None:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return ctx


def require_role(min_role: str):
    """FastAPI dependency: enforce a minimum role.

    Usage:
        @router.get("/foo", dependencies=[Depends(require_role("operator"))])
    """
    if min_role not in VALID_ROLES:
        raise ValueError(f"unknown role: {min_role}")

    def _enforce(ctx: TenantContext = Depends(get_tenant_context)) -> TenantContext:
        if not ctx.has_role(min_role):
            raise HTTPException(
                status_code=403,
                detail=f"requires role >= {min_role}, have {ctx.role}",
            )
        return ctx

    return _enforce
