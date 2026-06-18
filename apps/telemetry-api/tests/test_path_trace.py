"""Path trace tests (PR 30)."""
from __future__ import annotations

import uuid

from services.path_trace import (
    _order_rows_by_devices,
    _path_severity,
    _topology_order,
)


TENANT_A = str(uuid.uuid4())


def test_path_severity_empty():
    assert _path_severity([]) == "low"


def test_path_severity_uses_worst_hop():
    """Single saturated hop is enough to escalate the whole path."""
    hops = [
        {"peak_util_pct": 12.0},
        {"peak_util_pct": 92.0},
        {"peak_util_pct": 40.0},
    ]
    assert _path_severity(hops) == "critical"


def test_path_severity_high_band():
    hops = [{"peak_util_pct": 85.0}]
    assert _path_severity(hops) == "high"


def test_path_severity_medium_band():
    hops = [{"peak_util_pct": 70.0}]
    assert _path_severity(hops) == "medium"


def test_path_severity_low_band():
    hops = [{"peak_util_pct": 10.0}, {"peak_util_pct": 30.0}]
    assert _path_severity(hops) == "low"


def test_path_severity_tolerates_none_peaks():
    """A hop with no util data shouldn't crash severity assessment."""
    hops = [{"peak_util_pct": None}, {"peak_util_pct": 50.0}]
    assert _path_severity(hops) == "low"


# ---------------------------------------------------------------------------
# Topology ordering
# ---------------------------------------------------------------------------

def test_topology_order_single_device():
    assert _topology_order({"leaf1"}, {}) == ["leaf1"]


def test_topology_order_empty_devices():
    assert _topology_order(set(), {}) == []


def test_topology_order_three_hop_chain():
    """leaf1 ↔ spine1 ↔ leaf2 — head→tail must visit all three."""
    devices = {"leaf1", "spine1", "leaf2"}
    adj = {
        "leaf1": {"spine1"},
        "spine1": {"leaf1", "leaf2"},
        "leaf2": {"spine1"},
    }
    out = _topology_order(devices, adj)
    assert out is not None
    assert out[1] == "spine1"
    assert set(out) == devices


def test_topology_order_only_one_side_reported():
    """LLDP can be unidirectional in the cache if one switch hasn't
    refreshed yet. Symmetrization should still produce the chain."""
    devices = {"leaf1", "spine1", "leaf2"}
    # Only leaf1 and leaf2 reported; spine1 missed this poll
    adj = {
        "leaf1": {"spine1"},
        "leaf2": {"spine1"},
    }
    out = _topology_order(devices, adj)
    assert out is not None
    assert out[1] == "spine1"


def test_topology_order_branching_returns_none():
    """A star (3 candidates all adjacent to one center) is NOT a chain
    — the function must give up so the caller falls back."""
    devices = {"leaf1", "leaf2", "leaf3", "spine1"}
    adj = {
        "leaf1": {"spine1"},
        "leaf2": {"spine1"},
        "leaf3": {"spine1"},
        "spine1": {"leaf1", "leaf2", "leaf3"},
    }
    assert _topology_order(devices, adj) is None


def test_topology_order_disconnected_returns_none():
    """If LLDP says leaf1↔spine1 and leaf2↔spine2 with no link between
    the two pairs, we can't form one ordered chain across all four."""
    devices = {"leaf1", "spine1", "leaf2", "spine2"}
    adj = {
        "leaf1": {"spine1"},
        "spine1": {"leaf1"},
        "leaf2": {"spine2"},
        "spine2": {"leaf2"},
    }
    assert _topology_order(devices, adj) is None


def test_topology_order_no_lldp_returns_none():
    """Empty adjacency on a multi-device candidate set → fallback."""
    devices = {"a", "b", "c"}
    assert _topology_order(devices, {}) is None


def test_topology_order_ignores_self_loops():
    """A device with a path-key bug pointing at itself shouldn't be
    treated as an adjacency."""
    devices = {"leaf1", "spine1"}
    adj = {"leaf1": {"leaf1", "spine1"}, "spine1": {"leaf1"}}
    out = _topology_order(devices, adj)
    # We strip the self-edge during symmetrization-time loading, so
    # the chain check still passes
    assert out is not None
    assert set(out) == devices


def test_order_rows_by_devices_respects_device_order():
    class R:
        def __init__(self, device, iface, bytes_):
            self.device = device
            self.interface = iface
            self.bytes = bytes_

    rows = [
        R("leaf2", "Eth1", 100),
        R("leaf1", "Eth1", 50),
        R("spine1", "Eth1", 200),
    ]
    out = _order_rows_by_devices(rows, ["leaf1", "spine1", "leaf2"])
    assert [r.device for r in out] == ["leaf1", "spine1", "leaf2"]


def test_order_rows_by_devices_sorts_within_device_by_bytes():
    class R:
        def __init__(self, device, iface, bytes_):
            self.device = device
            self.interface = iface
            self.bytes = bytes_

    rows = [
        R("leaf1", "Eth2", 10),
        R("leaf1", "Eth1", 100),
    ]
    out = _order_rows_by_devices(rows, ["leaf1"])
    assert [r.interface for r in out] == ["Eth1", "Eth2"]
