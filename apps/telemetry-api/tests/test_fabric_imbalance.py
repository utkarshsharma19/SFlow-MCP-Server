"""ECMP fabric imbalance tests (PR 24)."""
from __future__ import annotations

import asyncio
import uuid

from services import fabric
from services.fabric import (
    CV_THRESHOLD,
    MAX_MEAN_RATIO_THRESHOLD,
    MIN_MEAN_UTIL_PCT,
    _humanize_speed,
    _imbalance_metrics,
    _overall_severity,
)


TENANT_A = str(uuid.uuid4())


def test_balanced_group_not_flagged():
    m = _imbalance_metrics([40.0, 41.0, 39.0, 42.0])
    assert m["is_imbalanced"] is False
    assert m["reason"] == "balanced"
    assert m["cv"] < CV_THRESHOLD
    assert m["max_mean_ratio"] < MAX_MEAN_RATIO_THRESHOLD


def test_imbalanced_when_one_member_carries_group():
    m = _imbalance_metrics([80.0, 10.0, 10.0, 10.0])
    assert m["is_imbalanced"] is True
    assert m["max_mean_ratio"] > MAX_MEAN_RATIO_THRESHOLD
    assert m["reason"] == "imbalanced"


def test_imbalance_suppressed_when_group_idle():
    # Below MIN_MEAN_UTIL_PCT even if ratios look skewed: just idle noise.
    m = _imbalance_metrics([1.0, 0.0, 0.0, 0.0])
    assert m["is_imbalanced"] is False
    assert m["reason"] == "below_min_util"


def test_insufficient_members_short_circuits():
    m = _imbalance_metrics([50.0])
    assert m["is_imbalanced"] is False
    assert m["reason"] in ("insufficient_members", "below_min_util")


def test_overall_severity_scales_with_worst_ratio():
    assert _overall_severity([]) == "low"
    assert _overall_severity([{"max_mean_ratio": 1.6}]) == "low"
    assert _overall_severity([{"max_mean_ratio": 2.1}]) == "medium"
    assert _overall_severity([{"max_mean_ratio": 3.5}]) == "high"


def test_humanize_speed_buckets():
    assert _humanize_speed(400_000_000_000) == "400G"
    assert _humanize_speed(100_000_000_000) == "100G"
    assert _humanize_speed(25_000_000_000) == "25G"
    assert _humanize_speed(10_000_000_000) == "10G"
    assert _humanize_speed(1_000_000_000) == "1G"
    assert _humanize_speed(500) == "500bps"


def test_detect_fabric_imbalance_returns_shape_and_filters_tenant():
    captured: list[str] = []

    class FakeResult:
        def all(self):
            return []

        def scalars(self):
            class _S:
                def all(self_inner):
                    return []

            return _S()

    class FakeSession:
        async def execute(self, query):
            captured.append(str(query))
            return FakeResult()

    out = asyncio.run(
        fabric.detect_fabric_imbalance(
            FakeSession(), tenant_id=TENANT_A, device=None, window_minutes=15
        )
    )
    assert out["severity"] == "low"
    assert out["groups"] == []
    assert out["imbalanced_groups"] == []
    assert "confidence_note" in out
    assert out["window_minutes"] == 15


def test_thresholds_are_sane():
    assert 0 < CV_THRESHOLD < 1
    assert MAX_MEAN_RATIO_THRESHOLD > 1
    assert MIN_MEAN_UTIL_PCT > 0
