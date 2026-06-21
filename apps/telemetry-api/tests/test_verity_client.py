"""Verity client parser tests (PR 30)."""
from __future__ import annotations

from collectors.verity_client import (
    DeclaredBGPPeer,
    DeclaredInterface,
    _canonical_bgp_state,
    _canonical_status,
    _parse_bgp,
    _parse_interfaces,
    _to_int,
)


def test_to_int_handles_blanks_and_garbage():
    assert _to_int(None) is None
    assert _to_int("") is None
    assert _to_int("abc") is None
    assert _to_int("42") == 42
    assert _to_int(42) == 42


def test_canonical_status_filters_unknown():
    assert _canonical_status("up") == "UP"
    assert _canonical_status("DOWN") == "DOWN"
    assert _canonical_status(None) is None
    assert _canonical_status("BOGUS") is None


def test_canonical_bgp_state_accepts_full_set():
    for s in (
        "IDLE",
        "CONNECT",
        "ACTIVE",
        "OPENSENT",
        "OPENCONFIRM",
        "ESTABLISHED",
    ):
        assert _canonical_bgp_state(s) == s
    assert _canonical_bgp_state("BROKEN") is None


def test_parse_interfaces_items_envelope():
    payload = {
        "items": [
            {
                "name": "leaf1",
                "ports": [
                    {
                        "name": "Ethernet0",
                        "admin": "UP",
                        "speed_bps": "100000000000",
                        "mtu": 9216,
                        "description": "to-spine1",
                    },
                    {
                        # Missing 'name' — skipped silently rather than crashing
                        "admin": "UP",
                    },
                ],
            }
        ]
    }
    out = list(_parse_interfaces(payload))
    assert len(out) == 1
    decl = out[0]
    assert isinstance(decl, DeclaredInterface)
    assert decl.device == "leaf1"
    assert decl.interface == "Ethernet0"
    assert decl.speed_bps == 100_000_000_000
    assert decl.mtu == 9216
    assert decl.admin_status == "UP"


def test_parse_interfaces_bare_list():
    """The Verity API has been seen returning a bare list — handle both."""
    payload = [
        {
            "hostname": "leaf2",
            "ports": [{"interface": "Eth1", "admin": "DOWN"}],
        }
    ]
    out = list(_parse_interfaces(payload))
    assert out[0].device == "leaf2"
    assert out[0].interface == "Eth1"
    assert out[0].admin_status == "DOWN"


def test_parse_interfaces_skips_malformed_top_level():
    """A nonsense response shouldn't take the ingest loop down."""
    assert list(_parse_interfaces("not a dict")) == []
    assert list(_parse_interfaces({"unrelated": 1})) == []


def test_parse_bgp_handles_camelcase_and_snake():
    """Verity is inconsistent across versions — accept both casings."""
    payload = {
        "items": [
            {"device": "leaf1", "peer": "10.0.0.1", "peerAs": 65001},
            {
                "switch": "leaf2",
                "peer_address": "10.0.0.2",
                "peer_as": 65002,
                "expectedState": "ESTABLISHED",
            },
        ]
    }
    out = list(_parse_bgp(payload))
    assert len(out) == 2
    assert isinstance(out[0], DeclaredBGPPeer)
    assert out[0].device == "leaf1"
    assert out[0].peer_address == "10.0.0.1"
    assert out[0].peer_as == 65001
    assert out[1].session_state == "ESTABLISHED"


def test_parse_bgp_skips_partial_rows():
    payload = {"items": [{"peer": "10.0.0.1"}, {"device": "leaf1"}]}
    assert list(_parse_bgp(payload)) == []
