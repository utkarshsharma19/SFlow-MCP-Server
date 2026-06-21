"""Tests for the gNMI response parsers and target parsing.

These exercise the pure parsing path with synthetic pygnmi-shaped Get
responses, so they require neither pygnmi nor a live gNMI target.
"""
from __future__ import annotations

from collectors.gnmi_client import (
    parse_targets,
    _parse_bgp_neighbors,
    _parse_interface_state,
    _parse_lldp_neighbors,
    _parse_queue_stats,
)


def test_parse_targets_handles_defaults_and_ports():
    out = parse_targets("spine1, leaf1:50051 ,bad:port,leaf2")
    assert ("spine1", 6030) in out
    assert ("leaf1", 50051) in out
    assert ("leaf2", 6030) in out
    # 'bad:port' is dropped because port is non-numeric.
    assert all(host != "bad" for host, _ in out)


def test_parse_targets_empty():
    assert parse_targets("") == []
    assert parse_targets(None) == []


def test_parse_interface_state_extracts_admin_oper_pairs():
    resp = {
        "notification": [
            {
                "update": [
                    {
                        "path": "interfaces/interface[name=Ethernet0]/state/admin-status",
                        "val": "UP",
                    },
                    {
                        "path": "interfaces/interface[name=Ethernet0]/state/oper-status",
                        "val": "UP",
                    },
                    {
                        "path": "interfaces/interface[name=Ethernet0]/state/speed",
                        "val": 100_000_000_000,
                    },
                    {
                        "path": "interfaces/interface[name=Ethernet1]/state/admin-status",
                        "val": "UP",
                    },
                    {
                        "path": "interfaces/interface[name=Ethernet1]/state/oper-status",
                        "val": "DOWN",
                    },
                ]
            }
        ]
    }
    out = _parse_interface_state("spine1", resp)
    by_iface = {s.interface: s for s in out}
    assert by_iface["Ethernet0"].oper_status == "UP"
    assert by_iface["Ethernet0"].speed_bps == 100_000_000_000
    assert by_iface["Ethernet1"].oper_status == "DOWN"
    assert by_iface["Ethernet1"].admin_status == "UP"


def test_parse_interface_state_skips_when_no_status_leaves():
    resp = {
        "notification": [
            {
                "update": [
                    {
                        "path": "interfaces/interface[name=Ethernet0]/state/description",
                        "val": "uplink",
                    }
                ]
            }
        ]
    }
    assert _parse_interface_state("spine1", resp) == []


def test_parse_bgp_neighbors_groups_by_peer_address():
    resp = {
        "notification": [
            {
                "update": [
                    {
                        "path": "bgp/neighbors/neighbor[neighbor-address=10.0.0.2]/state/session-state",
                        "val": "ESTABLISHED",
                    },
                    {
                        "path": "bgp/neighbors/neighbor[neighbor-address=10.0.0.2]/state/peer-as",
                        "val": 65001,
                    },
                    {
                        "path": "bgp/neighbors/neighbor[neighbor-address=10.0.0.3]/state/session-state",
                        "val": "ACTIVE",
                    },
                ]
            }
        ]
    }
    out = _parse_bgp_neighbors("leaf1", resp)
    by_peer = {p.peer_address: p for p in out}
    assert by_peer["10.0.0.2"].session_state == "ESTABLISHED"
    assert by_peer["10.0.0.2"].peer_as == 65001
    assert by_peer["10.0.0.3"].session_state == "ACTIVE"


def test_parse_queue_stats_includes_pfc_and_ecn():
    resp = {
        "notification": [
            {
                "update": [
                    {
                        "path": "qos/interfaces/interface[interface-id=Ethernet0]/output/queues/queue[name=3]/state/max-queue-len",
                        "val": 524288,
                    },
                    {
                        "path": "qos/interfaces/interface[interface-id=Ethernet0]/output/queues/queue[name=3]/state/pfc-pause-rx",
                        "val": 42,
                    },
                    {
                        "path": "qos/interfaces/interface[interface-id=Ethernet0]/output/queues/queue[name=3]/state/ecn-marked-pkts",
                        "val": 7,
                    },
                ]
            }
        ]
    }
    out = _parse_queue_stats("spine1", resp)
    assert len(out) == 1
    q = out[0]
    assert q.interface == "Ethernet0"
    assert q.queue_id == 3
    assert q.max_depth_bytes == 524288
    assert q.pfc_pause_rx == 42
    assert q.ecn_marked_packets == 7


def test_parse_handles_empty_response():
    assert _parse_interface_state("d", {}) == []
    assert _parse_bgp_neighbors("d", None) == []
    assert _parse_queue_stats("d", {"notification": []}) == []
    assert _parse_lldp_neighbors("d", {}) == []


def test_parse_lldp_neighbors_one_neighbor_per_port():
    resp = {
        "notification": [
            {
                "update": [
                    {
                        "path": (
                            "lldp/interfaces/interface[name=Ethernet0]/"
                            "neighbors/neighbor[id=0xaa:bb:cc:dd:ee:ff]/"
                            "state/chassis-id"
                        ),
                        "val": "aa:bb:cc:dd:ee:ff",
                    },
                    {
                        "path": (
                            "lldp/interfaces/interface[name=Ethernet0]/"
                            "neighbors/neighbor[id=0xaa:bb:cc:dd:ee:ff]/"
                            "state/system-name"
                        ),
                        "val": "spine1",
                    },
                    {
                        "path": (
                            "lldp/interfaces/interface[name=Ethernet0]/"
                            "neighbors/neighbor[id=0xaa:bb:cc:dd:ee:ff]/"
                            "state/port-id"
                        ),
                        "val": "Ethernet49",
                    },
                    {
                        "path": (
                            "lldp/interfaces/interface[name=Ethernet0]/"
                            "neighbors/neighbor[id=0xaa:bb:cc:dd:ee:ff]/"
                            "state/port-description"
                        ),
                        "val": "to-leaf1",
                    },
                    {
                        "path": (
                            "lldp/interfaces/interface[name=Ethernet0]/"
                            "neighbors/neighbor[id=0xaa:bb:cc:dd:ee:ff]/"
                            "state/management-address"
                        ),
                        "val": "10.0.0.1",
                    },
                ]
            }
        ]
    }
    out = _parse_lldp_neighbors("leaf1", resp)
    assert len(out) == 1
    n = out[0]
    assert n.device == "leaf1"
    assert n.interface == "Ethernet0"
    assert n.neighbor_chassis_id == "aa:bb:cc:dd:ee:ff"
    assert n.neighbor_system_name == "spine1"
    assert n.neighbor_port_id == "Ethernet49"
    assert n.neighbor_port_description == "to-leaf1"
    assert n.neighbor_management_address == "10.0.0.1"


def test_parse_lldp_prefers_leaf_chassis_id_over_path_key():
    """Path key may be URL-encoded; state/chassis-id is canonical."""
    resp = {
        "notification": [
            {
                "update": [
                    {
                        "path": (
                            "lldp/interfaces/interface[name=Eth0]/"
                            "neighbors/neighbor[id=URLENCODED-FORM]/"
                            "state/chassis-id"
                        ),
                        "val": "aa:bb:cc:dd:ee:ff",
                    },
                    {
                        "path": (
                            "lldp/interfaces/interface[name=Eth0]/"
                            "neighbors/neighbor[id=URLENCODED-FORM]/"
                            "state/system-name"
                        ),
                        "val": "spine1",
                    },
                ]
            }
        ]
    }
    out = _parse_lldp_neighbors("leaf1", resp)
    assert len(out) == 1
    assert out[0].neighbor_chassis_id == "aa:bb:cc:dd:ee:ff"


def test_parse_lldp_groups_multiple_neighbors_per_interface():
    """LLDP spec permits N neighbors per local port; the parser must
    keep them distinct rather than collapse to one row."""
    resp = {
        "notification": [
            {
                "update": [
                    {
                        "path": (
                            "lldp/interfaces/interface[name=Eth0]/"
                            "neighbors/neighbor[id=peer-A]/state/chassis-id"
                        ),
                        "val": "aa:aa:aa:aa:aa:aa",
                    },
                    {
                        "path": (
                            "lldp/interfaces/interface[name=Eth0]/"
                            "neighbors/neighbor[id=peer-B]/state/chassis-id"
                        ),
                        "val": "bb:bb:bb:bb:bb:bb",
                    },
                ]
            }
        ]
    }
    out = _parse_lldp_neighbors("leaf1", resp)
    chassis = sorted(n.neighbor_chassis_id for n in out)
    assert chassis == ["aa:aa:aa:aa:aa:aa", "bb:bb:bb:bb:bb:bb"]


def test_parse_lldp_skips_unrelated_paths():
    """Notifications mixed with other gNMI subscriptions must not
    spuriously emit neighbor rows."""
    resp = {
        "notification": [
            {
                "update": [
                    {
                        "path": "interfaces/interface[name=Eth0]/state/oper-status",
                        "val": "UP",
                    },
                ]
            }
        ]
    }
    assert _parse_lldp_neighbors("leaf1", resp) == []
