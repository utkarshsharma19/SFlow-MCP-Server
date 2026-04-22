"""Tenant-isolation + shape tests for the device-state service."""
from __future__ import annotations

import asyncio
import uuid

from services import device_state


TENANT_A = str(uuid.uuid4())


def test_get_device_state_filters_by_tenant():
    captured: list[str] = []

    class FakeResult:
        def all(self):
            return []

        def scalars(self):
            return self

    class FakeSession:
        async def execute(self, query):
            captured.append(str(query))
            return FakeResult()

    asyncio.run(
        device_state.get_device_state(
            FakeSession(), tenant_id=TENANT_A, device="spine1", window_minutes=15
        )
    )
    assert captured, "service must execute at least one query"
    assert all("tenant_id" in sql for sql in captured)


def test_get_device_state_no_data_returns_helpful_note():
    class FakeResult:
        def all(self):
            return []

        def scalars(self):
            return self

    class FakeSession:
        async def execute(self, query):
            return FakeResult()

    out = asyncio.run(
        device_state.get_device_state(
            FakeSession(), tenant_id=TENANT_A, device="spine1", window_minutes=15
        )
    )
    assert out["device"] == "spine1"
    assert out["interfaces"] == []
    assert "No gNMI samples" in out["confidence_note"]
