"""Tests for the link-history drill-down service (PR 30)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from services.link_history import _bucket_series, _detect_flaps, _summary


TENANT_A = str(uuid.uuid4())


class _UtilRow:
    def __init__(self, ts, in_pct, out_pct, errors=0):
        self.ts_bucket = ts
        self.in_util_pct = in_pct
        self.out_util_pct = out_pct
        self.error_count = errors


class _StateRow:
    def __init__(self, ts, last_change, admin="UP", oper="UP"):
        self.ts_bucket = ts
        self.last_change = last_change
        self.admin_status = admin
        self.oper_status = oper


def test_bucket_series_emits_zero_filled_bins():
    """A 30-min window at 5-min buckets must return 6 entries even if empty."""
    since = datetime.now(timezone.utc) - timedelta(minutes=30)
    series = _bucket_series([], since, bucket_minutes=5, bucket_count=6)
    assert len(series) == 6
    for b in series:
        assert b["samples"] == 0
        assert b["max_util_pct"] == 0.0


def test_bucket_series_aggregates_within_bucket():
    """Two samples in the same 5-min bucket should average + max correctly."""
    since = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=10)
    rows = [
        _UtilRow(since + timedelta(minutes=0), 40, 30, errors=2),
        _UtilRow(since + timedelta(minutes=2), 60, 50, errors=3),
        _UtilRow(since + timedelta(minutes=7), 70, 60, errors=1),
    ]
    series = _bucket_series(rows, since, bucket_minutes=5, bucket_count=2)
    assert len(series) == 2
    # First bucket: average (40+60)/2=50 in, peak 60
    assert series[0]["avg_in_util_pct"] == 50.0
    assert series[0]["max_in_util_pct"] == 60.0
    assert series[0]["errors"] == 5
    assert series[0]["samples"] == 2
    # Second bucket: lone row
    assert series[1]["max_in_util_pct"] == 70.0
    assert series[1]["samples"] == 1


def test_detect_flaps_dedups_by_last_change():
    """A last_change observed in N consecutive samples is still ONE flap."""
    base = datetime.now(timezone.utc) - timedelta(minutes=30)
    flap_at = base + timedelta(minutes=10)
    rows = [
        _StateRow(base + timedelta(minutes=11), flap_at, oper="DOWN"),
        _StateRow(base + timedelta(minutes=12), flap_at, oper="UP"),
        _StateRow(base + timedelta(minutes=13), flap_at, oper="UP"),
    ]
    flaps = _detect_flaps(rows, since=base)
    assert len(flaps) == 1
    assert flaps[0]["last_change"] == flap_at.isoformat()


def test_detect_flaps_skips_changes_before_window():
    """Pre-window last_change values are stable history, not flaps."""
    since = datetime.now(timezone.utc) - timedelta(minutes=30)
    pre = since - timedelta(hours=2)
    rows = [_StateRow(since + timedelta(minutes=5), pre)]
    assert _detect_flaps(rows, since) == []


def test_summary_handles_empty_series():
    out = _summary([])
    assert out["peak_util_pct"] == 0.0
    assert out["total_errors"] == 0
    assert out["buckets_with_samples"] == 0


def test_summary_aggregates_real_buckets():
    series = [
        {"max_util_pct": 30.0, "errors": 1, "samples": 2},
        {"max_util_pct": 90.0, "errors": 5, "samples": 1},
        {"max_util_pct": 50.0, "errors": 0, "samples": 1},
    ]
    s = _summary(series)
    assert s["peak_util_pct"] == 90.0
    assert s["total_errors"] == 6
    assert s["buckets_with_samples"] == 3
