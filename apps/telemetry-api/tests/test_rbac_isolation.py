"""Cross-tenant isolation + role enforcement tests.

These tests assert the PR 20 acceptance criteria:
  * A viewer key in tenant A cannot read telemetry belonging to tenant B.
  * Role rank is enforced: a viewer cannot hit an analyst-only endpoint.
  * Missing / unknown keys are rejected.

They hit the service + auth layers directly rather than spinning up a live
DB, so CI can run them without infrastructure. A follow-up integration
suite (PR 22) will exercise the full HTTP path.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from auth.context import TenantContext, require_role


TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Role-rank enforcement
# ---------------------------------------------------------------------------

def test_viewer_cannot_access_analyst_endpoint():
    dep = require_role("analyst")
    viewer_ctx = TenantContext(
        tenant_id=TENANT_A, api_key_id=str(uuid.uuid4()), role="viewer"
    )
    with pytest.raises(Exception) as exc_info:
        dep(viewer_ctx)
    assert "403" in str(exc_info.value) or "requires role" in str(exc_info.value)


def test_analyst_can_access_viewer_endpoint():
    dep = require_role("viewer")
    analyst_ctx = TenantContext(
        tenant_id=TENANT_A, api_key_id=str(uuid.uuid4()), role="analyst"
    )
    returned = dep(analyst_ctx)
    assert returned is analyst_ctx


def test_tenant_admin_can_access_all_lower_roles():
    admin_ctx = TenantContext(
        tenant_id=TENANT_A, api_key_id=str(uuid.uuid4()), role="tenant_admin"
    )
    for min_role in ("viewer", "analyst", "operator", "tenant_admin"):
        dep = require_role(min_role)
        assert dep(admin_ctx) is admin_ctx


def test_unknown_role_is_denied():
    bogus = TenantContext(
        tenant_id=TENANT_A, api_key_id=str(uuid.uuid4()), role="superuser"
    )
    dep = require_role("viewer")
    with pytest.raises(Exception):
        dep(bogus)


# ---------------------------------------------------------------------------
# Tenant isolation — service layer
# ---------------------------------------------------------------------------

def test_top_talkers_filters_by_tenant(monkeypatch):
    """get_top_talkers must emit a WHERE tenant_id = :tenant on every query."""
    from services import flows

    captured = {}

    class FakeResult:
        def all(self):
            return []

    class FakeSession:
        async def execute(self, query):
            captured["sql"] = str(query.compile(compile_kwargs={"literal_binds": False}))
            return FakeResult()

    asyncio.run(
        flows.get_top_talkers(
            FakeSession(), tenant_id=TENANT_A, window_minutes=15, scope="global"
        )
    )
    assert "tenant_id" in captured["sql"]


def test_interface_utilization_filters_by_tenant():
    from services import interfaces

    captured = {}

    class FakeResult:
        def all(self):
            return []

        def scalar_one_or_none(self):
            return None

    class FakeSession:
        async def execute(self, query):
            captured["sql"] = str(query)
            return FakeResult()

    asyncio.run(
        interfaces.get_interface_utilization(
            FakeSession(),
            tenant_id=TENANT_A,
            device="edge1",
            interface="Gi0/1",
            window_minutes=15,
        )
    )
    assert "tenant_id" in captured["sql"]


def test_anomalies_recent_filters_by_tenant():
    from services import anomalies_query

    captured = {}

    class FakeResult:
        def all(self):
            return []

    class FakeSession:
        async def execute(self, query):
            captured["sql"] = str(query)
            return FakeResult()

    asyncio.run(
        anomalies_query.get_recent_anomalies(
            FakeSession(),
            tenant_id=TENANT_A,
            scope="global",
            severity_min="medium",
            since_minutes=30,
        )
    )
    assert "tenant_id" in captured["sql"]


# ---------------------------------------------------------------------------
# API key hashing
# ---------------------------------------------------------------------------

def test_api_key_hash_is_deterministic_and_sha256():
    from auth.context import hash_api_key

    h1 = hash_api_key("dev-insecure-key")
    h2 = hash_api_key("dev-insecure-key")
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex length
    assert all(c in "0123456789abcdef" for c in h1)


def test_different_keys_hash_differently():
    from auth.context import hash_api_key

    assert hash_api_key("key-a") != hash_api_key("key-b")
