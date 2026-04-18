"""Per-source → tenant mapping tests (PR 22).

Exercises the in-memory router behaviour and confirms ingest-side
normalizers consult the router for tenant assignment.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone

import pytest

from db.models import DEFAULT_TENANT_ID
from services import tenant_routing
from services.tenant_routing import (
    SOURCE_KIND_GNMI,
    SOURCE_KIND_SFLOW,
    TenantRouter,
)


TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())


def _seed_router(mapping: dict[tuple[str, str], str]) -> TenantRouter:
    """Build a router with a pre-populated cache and a future TTL.

    Avoids the DB roundtrip in `refresh()` by setting `_loaded_at` to now —
    `_maybe_refresh` then sees a fresh cache and skips the call entirely.
    """
    r = TenantRouter()
    r._cache = dict(mapping)
    r._loaded_at = time.monotonic()
    return r


def test_router_returns_default_for_unmapped_source():
    r = _seed_router({})
    assert asyncio.run(r.tenant_for(SOURCE_KIND_SFLOW, "10.0.0.1")) == DEFAULT_TENANT_ID


def test_router_returns_mapped_tenant():
    r = _seed_router({(SOURCE_KIND_SFLOW, "spine1"): TENANT_A})
    assert asyncio.run(r.tenant_for(SOURCE_KIND_SFLOW, "spine1")) == TENANT_A


def test_router_isolates_kinds():
    """Same identifier under different kinds resolves independently."""
    r = _seed_router(
        {
            (SOURCE_KIND_SFLOW, "host"): TENANT_A,
            (SOURCE_KIND_GNMI, "host"): TENANT_B,
        }
    )
    assert asyncio.run(r.tenant_for(SOURCE_KIND_SFLOW, "host")) == TENANT_A
    assert asyncio.run(r.tenant_for(SOURCE_KIND_GNMI, "host")) == TENANT_B


def test_router_refresh_swallows_errors(monkeypatch):
    """Stale cache must keep serving when DB refresh fails."""
    r = _seed_router({(SOURCE_KIND_SFLOW, "spine1"): TENANT_A})
    # Force the cache to look stale and inject a refresh that raises.
    r._loaded_at = 0.0

    async def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(r, "refresh", boom)
    # Should not raise; falls back to existing cache value.
    assert asyncio.run(r.tenant_for(SOURCE_KIND_SFLOW, "spine1")) == TENANT_A


def test_normalize_flows_routes_per_agent():
    from services.ingest import normalize_flows
    from shared.schemas.flow import FlowRecord

    router = _seed_router({(SOURCE_KIND_SFLOW, "edge1"): TENANT_A})
    now = datetime.now(timezone.utc)
    records = [
        FlowRecord(
            agent="edge1",
            input_if_index=1,
            output_if_index=2,
            src_ip="10.0.0.1",
            dst_ip="10.0.0.2",
            protocol=6,
            bytes=100,
            packets=1,
            sampling_rate=1000,
            timestamp=now,
        ),
        FlowRecord(
            agent="edge2",
            input_if_index=1,
            output_if_index=2,
            src_ip="10.0.0.3",
            dst_ip="10.0.0.4",
            protocol=6,
            bytes=200,
            packets=2,
            sampling_rate=1000,
            timestamp=now,
        ),
    ]

    rows = asyncio.run(normalize_flows(records, router))
    by_agent = {r["device"]: r["tenant_id"] for r in rows}
    assert by_agent["edge1"] == TENANT_A
    assert by_agent["edge2"] == DEFAULT_TENANT_ID


def test_normalize_counters_routes_per_agent():
    from services.ingest import normalize_counters
    from shared.schemas.interface import InterfaceCounter

    router = _seed_router({(SOURCE_KIND_SFLOW, "leaf1"): TENANT_A})
    now = datetime.now(timezone.utc)
    counters = [
        InterfaceCounter(
            agent="leaf1",
            if_index=1,
            if_name="Eth0",
            if_speed=10_000_000_000,
            if_in_octets=10_000,
            if_out_octets=20_000,
            if_in_errors=0,
            if_out_errors=0,
            timestamp=now,
        ),
        InterfaceCounter(
            agent="leaf2",
            if_index=1,
            if_name="Eth0",
            if_speed=10_000_000_000,
            if_in_octets=10_000,
            if_out_octets=20_000,
            if_in_errors=0,
            if_out_errors=0,
            timestamp=now,
        ),
    ]
    rows = asyncio.run(normalize_counters(counters, router))
    by_agent = {r["device"]: r["tenant_id"] for r in rows}
    assert by_agent["leaf1"] == TENANT_A
    assert by_agent["leaf2"] == DEFAULT_TENANT_ID


def test_normalize_gnmi_state_routes_per_device():
    from services.gnmi_ingest import normalize_interface_state
    from shared.schemas.device_state import InterfaceState

    router = _seed_router({(SOURCE_KIND_GNMI, "spine1"): TENANT_B})
    now = datetime.now(timezone.utc)
    states = [
        InterfaceState(
            device="spine1",
            interface="Ethernet0",
            admin_status="UP",
            oper_status="UP",
            timestamp=now,
        ),
        InterfaceState(
            device="spine2",
            interface="Ethernet0",
            admin_status="UP",
            oper_status="UP",
            timestamp=now,
        ),
    ]
    rows = asyncio.run(normalize_interface_state(states, router))
    by_device = {r["device"]: r["tenant_id"] for r in rows}
    assert by_device["spine1"] == TENANT_B
    assert by_device["spine2"] == DEFAULT_TENANT_ID


def test_get_router_singleton():
    r1 = tenant_routing.get_router()
    r2 = tenant_routing.get_router()
    assert r1 is r2
