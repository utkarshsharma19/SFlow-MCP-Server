"""Tests for the intent-vs-state diff service (PR 29)."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from services import intent_diff
from services.intent_diff import _compare_bgp, _compare_interface, _severity


TENANT_A = str(uuid.uuid4())


class _Intent:
    """Minimal stand-in for DeviceIntent / BGPIntent ORM rows."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _state_iface(**over):
    base = {
        "device": "leaf1",
        "interface": "Eth0",
        "admin_status": "UP",
        "oper_status": "UP",
        "speed_bps": 100_000_000_000,
        "mtu": 9216,
        "description": "to-spine1",
        "ts_bucket": datetime.now(timezone.utc).isoformat(),
    }
    base.update(over)
    return base


def test_compare_interface_ignores_null_intent_fields():
    intent = _Intent(
        expected_admin_status=None,
        expected_oper_status=None,
        expected_speed_bps=None,
        expected_mtu=None,
        expected_description=None,
    )
    diffs = _compare_interface(intent, _state_iface())
    assert diffs == []


def test_compare_interface_flags_each_drifted_field():
    intent = _Intent(
        expected_admin_status="UP",
        expected_oper_status="UP",
        expected_speed_bps=100_000_000_000,
        expected_mtu=9216,
        expected_description="to-spine1",
    )
    state = _state_iface(oper_status="DOWN", mtu=1500)
    diffs = _compare_interface(intent, state)
    assert any("oper" in d for d in diffs)
    assert any("mtu" in d for d in diffs)
    assert len(diffs) == 2


def test_compare_bgp_flags_state_and_as():
    intent = _Intent(expected_peer_as=65001, expected_session_state="ESTABLISHED")
    state = {"peer_as": 65999, "session_state": "ACTIVE"}
    diffs = _compare_bgp(intent, state)
    assert any("peer_as" in d for d in diffs)
    assert any("session_state" in d for d in diffs)
    assert len(diffs) == 2


def test_severity_critical_when_oper_drift_on_declared_up_link():
    iface = [
        {
            "kind": "mismatch",
            "intent": {"expected_oper_status": "UP"},
            "state": {"oper_status": "DOWN"},
        }
    ]
    assert _severity(iface, []) == "critical"


def test_severity_critical_when_bgp_declared_established_is_active():
    bgp = [
        {
            "kind": "mismatch",
            "intent": {"expected_session_state": "ESTABLISHED"},
            "state": {"session_state": "ACTIVE"},
        }
    ]
    assert _severity([], bgp) == "critical"


def test_severity_medium_for_missing_state_only():
    iface = [{"kind": "missing_state", "intent": {}, "state": None}]
    assert _severity(iface, []) == "medium"


def test_severity_low_when_only_unexpected_state():
    iface = [{"kind": "unexpected_state", "intent": None, "state": {}}]
    assert _severity(iface, []) == "low"


def test_severity_low_when_no_findings():
    assert _severity([], []) == "low"


def test_diff_intent_vs_state_returns_shape_when_empty():
    """No intent + no state should still return a valid shape, not crash."""

    class FakeResult:
        def all(self_inner):
            return []

        def scalars(self_inner):
            class _S:
                def all(_self):
                    return []

            return _S()

    class FakeSession:
        async def execute(self_inner, q):
            return FakeResult()

    out = asyncio.run(
        intent_diff.diff_intent_vs_state(
            FakeSession(), tenant_id=TENANT_A, device=None
        )
    )
    assert out["total_drift_count"] == 0
    assert out["severity"] == "low"
    assert out["interfaces"]["findings"] == []
    assert out["bgp"]["findings"] == []
    assert "confidence_note" in out
