"""Path trace tests (PR 30)."""
from __future__ import annotations

import uuid

from services.path_trace import _path_severity


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
