"""Top-offenders ranking tests (PR 30)."""
from __future__ import annotations

from services.top_offenders import (
    SEVERITY_WEIGHT,
    _ensure,
    _normalize_and_combine,
    _parse_scope,
)


def test_parse_scope_device():
    assert _parse_scope("device:leaf1") == ("device", "leaf1", None)


def test_parse_scope_interface():
    assert _parse_scope("interface:leaf1/Eth0") == ("interface", "leaf1", "Eth0")


def test_parse_scope_global_returns_none():
    assert _parse_scope("global") is None
    assert _parse_scope("unknown:foo") is None


def test_severity_weight_monotonic():
    assert (
        SEVERITY_WEIGHT["low"]
        < SEVERITY_WEIGHT["medium"]
        < SEVERITY_WEIGHT["high"]
        < SEVERITY_WEIGHT["critical"]
    )


def test_ensure_initializes_entry_shape():
    by_key: dict = {}
    e = _ensure(by_key, ("leaf1",), "device")
    assert e["device"] == "leaf1"
    assert e["anomaly_score"] == 0
    assert e["anomaly_breakdown"] == {
        "low": 0,
        "medium": 0,
        "high": 0,
        "critical": 0,
    }
    # Calling twice returns the same row, not a new one
    e2 = _ensure(by_key, ("leaf1",), "device")
    assert e2 is e


def test_normalize_and_combine_weights():
    """Critical anomaly carries more than max errors when both saturate."""
    rows = [
        {
            "device": "leaf1",
            "anomaly_score": 100,
            "flap_count": 0,
            "error_count": 0,
            "hot_bucket_count": 0,
        },
        {
            "device": "leaf2",
            "anomaly_score": 0,
            "flap_count": 0,
            "error_count": 100,
            "hot_bucket_count": 0,
        },
    ]
    _normalize_and_combine(rows)
    # Anomaly weight (0.4) > Error weight (0.15) — leaf1 should rank higher
    leaf1 = next(r for r in rows if r["device"] == "leaf1")
    leaf2 = next(r for r in rows if r["device"] == "leaf2")
    assert leaf1["composite_score"] > leaf2["composite_score"]
    assert leaf1["composite_score"] == 0.4
    assert leaf2["composite_score"] == 0.15


def test_normalize_handles_empty():
    """Should not crash on empty input."""
    rows: list = []
    _normalize_and_combine(rows)
    assert rows == []


def test_drivers_only_list_nonzero_signals():
    rows = [
        {
            "device": "leaf1",
            "anomaly_score": 5,
            "flap_count": 0,
            "error_count": 0,
            "hot_bucket_count": 3,
        }
    ]
    _normalize_and_combine(rows)
    drivers = rows[0]["drivers"]
    assert any("anomaly" in d for d in drivers)
    assert any("hot bucket" in d for d in drivers)
    assert not any("flap" in d for d in drivers)
    assert not any("error" in d for d in drivers)
