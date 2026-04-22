"""Severity assessment tests for the RDMA / RoCE health service (PR 23)."""
from __future__ import annotations

import asyncio
import uuid

from services import rdma
from services.rdma import _assess


TENANT_A = str(uuid.uuid4())


def _q(**overrides):
    base = dict(
        device="spine1",
        interface="Eth0",
        queue_id=3,
        pfc_pause_rx=0,
        pfc_pause_tx=0,
        ecn_marked_packets=0,
        dropped_packets=0,
        peak_depth_bytes=0,
    )
    base.update(overrides)
    return base


def test_assess_critical_when_drops_and_pfc_on_same_queue():
    sev, drivers = _assess(0.7, [_q(pfc_pause_rx=10, dropped_packets=5)])
    assert sev == "critical"
    assert any("Lossless drop" in d for d in drivers)


def test_assess_high_when_sustained_pfc_without_drops():
    sev, drivers = _assess(0.5, [_q(pfc_pause_rx=2000)])
    assert sev == "high"
    assert any("Sustained PFC" in d for d in drivers)


def test_assess_medium_when_only_ecn_signaling():
    sev, drivers = _assess(0.3, [_q(ecn_marked_packets=200)])
    assert sev == "medium"
    assert any("ECN" in d for d in drivers)


def test_assess_low_when_below_thresholds():
    # Some PFC but under storm threshold, no drops, no ECN.
    sev, _ = _assess(0.2, [_q(pfc_pause_rx=10)])
    assert sev == "low"


def test_assess_low_when_no_signals():
    sev, _ = _assess(0.0, [])
    assert sev == "low"


def test_get_rdma_health_filters_by_tenant_and_returns_shape():
    captured: list[str] = []

    class FakeResult:
        def all(self):
            return []

        def scalar(self):
            return 0

    class FakeSession:
        async def execute(self, query):
            captured.append(str(query))
            return FakeResult()

    out = asyncio.run(
        rdma.get_rdma_health(FakeSession(), tenant_id=TENANT_A, window_minutes=15)
    )
    assert all("tenant_id" in q for q in captured)
    assert out["severity"] == "low"
    assert out["roce_share_estimated"] == 0.0
    assert "confidence_note" in out
    assert "queues_with_congestion_signals" in out
