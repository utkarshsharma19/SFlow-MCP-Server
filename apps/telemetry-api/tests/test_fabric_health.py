"""Tests for the rolled-up fabric health service (PR 29)."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from services import fabric_health

_SINCE = datetime.now(timezone.utc) - timedelta(minutes=15)
from services.fabric_health import (
    LINK_UTIL_WARN,
    WEIGHTS,
    severity_from_score,
)


TENANT_A = str(uuid.uuid4())


def test_severity_from_score_bands():
    assert severity_from_score(1.0) == "low"
    assert severity_from_score(0.91) == "low"
    assert severity_from_score(0.80) == "medium"
    assert severity_from_score(0.60) == "high"
    assert severity_from_score(0.10) == "critical"


def test_weights_sum_to_one():
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def test_no_data_returns_perfect_score_with_caveat():
    """A tenant with zero telemetry shouldn't return 'fabric is great' silently."""

    class FakeResult:
        def all(self):
            return []

        def scalars(self):
            class _S:
                def all(_self):
                    return []

            return _S()

    class FakeSession:
        async def execute(self, query):
            return FakeResult()

    out = asyncio.run(
        fabric_health.get_fabric_health(
            FakeSession(), tenant_id=TENANT_A, window_minutes=15
        )
    )
    # Score is 1.0 because nothing is observably bad, but confidence_note
    # must mention every missing plane so the chatbot degrades language.
    assert out["overall_score"] == 1.0
    assert out["severity"] == "low"
    for plane in ("interface counters", "BGP state", "queue/PFC/ECN", "collector heartbeats"):
        assert plane in out["confidence_note"]


def test_link_score_penalty_proportional_to_hot_count():
    """3 hot interfaces out of 10 → score = 0.7 on the link component."""

    class Row:
        def __init__(self, device, iface, peak):
            self.device = device
            self.interface = iface
            self.peak_util = peak

    rows = [Row("leaf1", f"Eth{i}", 95.0 if i < 3 else 20.0) for i in range(10)]

    class FakeResult:
        def __init__(self, rs):
            self._rs = rs

        def all(self):
            return self._rs

    async def run():
        class FakeSession:
            async def execute(self, q):
                return FakeResult(rows)

        return await fabric_health._score_links(
            FakeSession(), TENANT_A, since=_SINCE
        )

    out = asyncio.run(run())
    assert out["hot_interfaces"] == 3
    assert out["interfaces_observed"] == 10
    assert out["score"] == 0.7
    assert "exceeded" in out["driver"]


def test_queue_score_drops_dominate_pfc_dominate_ecn():
    """Per the docstring: drops > sustained PFC > ECN in penalty weighting."""

    class Row:
        def __init__(self, drops=0, pfc=0, ecn=0):
            self.device = "leaf1"
            self.interface = "Eth0"
            self.queue_id = 3
            self.pfc_rx = pfc
            self.pfc_tx = 0
            self.ecn = ecn
            self.drops = drops

    async def run(rows):
        class FakeResult:
            def all(self_inner):
                return rows

        class FakeSession:
            async def execute(self_inner, q):
                return FakeResult()

        return await fabric_health._score_queues(
            FakeSession(), TENANT_A, since=_SINCE
        )

    drops_only = asyncio.run(run([Row(drops=10)]))
    pfc_only = asyncio.run(run([Row(pfc=5000)]))
    ecn_only = asyncio.run(run([Row(ecn=500)]))

    assert drops_only["score"] < pfc_only["score"] < ecn_only["score"]
    assert drops_only["queues_with_drops"] == 1
    assert pfc_only["queues_with_pfc"] == 1
    assert ecn_only["queues_with_ecn"] == 1


def test_link_threshold_does_not_flag_cold_links():
    """A link at exactly LINK_UTIL_WARN-1 must not count as hot."""

    class Row:
        def __init__(self, peak):
            self.device = "leaf1"
            self.interface = "Eth0"
            self.peak_util = peak

    rows = [Row(LINK_UTIL_WARN - 0.5)]

    async def run():
        class FakeResult:
            def all(self_inner):
                return rows

        class FakeSession:
            async def execute(self_inner, q):
                return FakeResult()

        return await fabric_health._score_links(
            FakeSession(), TENANT_A, since=_SINCE
        )

    out = asyncio.run(run())
    assert out["hot_interfaces"] == 0
    assert out["score"] == 1.0
