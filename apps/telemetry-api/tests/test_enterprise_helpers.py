"""Unit tests for pure helpers added in PR 26–28."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.anomaly_dedup import fingerprint_for
from services.source_freshness import STALE_THRESHOLDS, classify
from services.tool_audit import current_period_start


def test_fingerprint_is_stable_across_key_order_in_cause():
    a = fingerprint_for("t1", "link_saturation", "device:leaf1", {"queue": 3, "dir": "rx"})
    b = fingerprint_for("t1", "link_saturation", "device:leaf1", {"dir": "rx", "queue": 3})
    assert a == b


def test_fingerprint_differs_on_cause():
    a = fingerprint_for("t1", "pfc_storm", "device:spine1", {"queue": 3})
    b = fingerprint_for("t1", "pfc_storm", "device:spine1", {"queue": 4})
    assert a != b


def test_fingerprint_isolates_tenants():
    a = fingerprint_for("t1", "link_saturation", "device:leaf1")
    b = fingerprint_for("t2", "link_saturation", "device:leaf1")
    assert a != b


def test_fingerprint_is_64_hex_chars():
    fp = fingerprint_for("t1", "x", "device:y")
    assert len(fp) == 64
    int(fp, 16)  # parses


def test_classify_fresh_within_threshold():
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    last = now - STALE_THRESHOLDS["sflow"] + timedelta(seconds=10)
    assert classify(now, last, "sflow") == "fresh"


def test_classify_stale_past_threshold():
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    last = now - STALE_THRESHOLDS["sflow"] - timedelta(seconds=10)
    assert classify(now, last, "sflow") == "stale"


def test_classify_silent_past_5x_threshold():
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    last = now - STALE_THRESHOLDS["gnmi"] * 6
    assert classify(now, last, "gnmi") == "silent"


def test_classify_unknown_kind_falls_back_to_default():
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    last = now - timedelta(seconds=30)
    assert classify(now, last, "made_up_kind") == "fresh"


def test_current_period_start_is_day_anchored_utc():
    ps = current_period_start(datetime(2026, 4, 18, 14, 33, 17, tzinfo=timezone.utc))
    assert ps == datetime(2026, 4, 18, 0, 0, 0, tzinfo=timezone.utc)
