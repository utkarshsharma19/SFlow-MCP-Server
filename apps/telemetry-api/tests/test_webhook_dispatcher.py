"""Webhook dispatcher unit tests (PR 30)."""
from __future__ import annotations

import hmac
import hashlib
import json
import uuid
from datetime import datetime, timezone

from services.webhook_dispatcher import (
    SEVERITY_RANK,
    SIGNATURE_HEADER,
    build_payload,
    sign_payload,
)


def test_sign_payload_matches_independent_hmac():
    """The signature format must be reproducible by a recipient with the secret."""
    secret = "whk_test_secret"
    body = json.dumps({"a": 1, "b": 2}, separators=(",", ":")).encode("utf-8")
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    assert sign_payload(secret, body) == expected


def test_sign_payload_is_deterministic():
    secret = "x"
    body = b"hello"
    assert sign_payload(secret, body) == sign_payload(secret, body)


def test_sign_payload_changes_when_secret_changes():
    body = b"hello"
    assert sign_payload("a", body) != sign_payload("b", body)


def test_signature_header_constant():
    """If we change this, every existing consumer breaks — pin it in tests."""
    assert SIGNATURE_HEADER == "X-FlowMind-Signature"


def test_severity_rank_ordering():
    assert SEVERITY_RANK["critical"] > SEVERITY_RANK["high"]
    assert SEVERITY_RANK["high"] > SEVERITY_RANK["medium"]
    assert SEVERITY_RANK["medium"] > SEVERITY_RANK["low"]


class _FakeAnomaly:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", uuid.uuid4())
        self.tenant_id = kwargs.get("tenant_id", uuid.uuid4())
        self.anomaly_type = kwargs.get("anomaly_type", "pfc_storm")
        self.severity = kwargs.get("severity", "critical")
        self.scope = kwargs.get("scope", "device:leaf1")
        self.summary = kwargs.get("summary", "PFC storm on leaf1")
        self.first_seen_at = kwargs.get("first_seen_at", datetime.now(timezone.utc))
        self.last_seen_at = kwargs.get("last_seen_at", datetime.now(timezone.utc))
        self.occurrence_count = kwargs.get("occurrence_count", 1)
        self.metadata_json = kwargs.get("metadata_json", {"queue_id": 3})


def test_build_payload_includes_required_fields():
    """Receivers depend on this shape — every key must be present."""
    anomaly = _FakeAnomaly()
    payload = build_payload(anomaly)
    for required in (
        "type",
        "anomaly_id",
        "tenant_id",
        "anomaly_type",
        "severity",
        "scope",
        "summary",
        "first_seen_at",
        "last_seen_at",
        "occurrence_count",
        "metadata",
    ):
        assert required in payload
    assert payload["type"] == "flowmind.anomaly"


def test_build_payload_handles_null_timestamps():
    """The dedup upsert path can leave first_seen_at unset on legacy rows."""
    anomaly = _FakeAnomaly(first_seen_at=None, last_seen_at=None)
    payload = build_payload(anomaly)
    assert payload["first_seen_at"] is None
    assert payload["last_seen_at"] is None
