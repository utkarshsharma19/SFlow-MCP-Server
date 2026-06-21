"""Chat-session router shape tests (PR 31).

We exercise the serializer + validation logic. Round-trip integration
against a real DB is covered by the migration smoke test + a future
e2e suite.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from routers.chat import _serialize


class _Row:
    """ChatSession stand-in with just the attributes _serialize touches."""

    def __init__(self, **kw):
        self.id = kw.get("id", uuid.uuid4())
        self.tenant_id = kw.get("tenant_id", uuid.uuid4())
        self.api_key_id = kw.get("api_key_id")
        self.user_label = kw.get("user_label")
        self.started_at = kw.get("started_at", datetime.now(timezone.utc))
        self.ended_at = kw.get("ended_at")
        self.tokens_in = kw.get("tokens_in", 0)
        self.tokens_out = kw.get("tokens_out", 0)
        self.tool_calls = kw.get("tool_calls", 0)


def test_serialize_returns_iso_timestamps_and_string_ids():
    row = _Row(
        api_key_id=uuid.uuid4(),
        user_label="alice",
        tokens_in=10,
        tokens_out=20,
        tool_calls=2,
    )
    out = _serialize(row)
    assert isinstance(out["id"], str)
    assert isinstance(out["tenant_id"], str)
    assert isinstance(out["api_key_id"], str)
    assert out["user_label"] == "alice"
    assert out["tokens_in"] == 10
    assert out["tokens_out"] == 20
    assert out["tool_calls"] == 2
    # Timestamp is ISO format
    datetime.fromisoformat(out["started_at"])
    assert out["ended_at"] is None


def test_serialize_handles_optional_fields():
    """api_key_id / user_label / ended_at are all nullable — make sure
    the serializer hands back None, not the literal string 'None'."""
    row = _Row(api_key_id=None, user_label=None, ended_at=None)
    out = _serialize(row)
    assert out["api_key_id"] is None
    assert out["user_label"] is None
    assert out["ended_at"] is None


def test_serialize_emits_ended_at_when_set():
    ended = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    row = _Row(ended_at=ended)
    out = _serialize(row)
    assert out["ended_at"] == ended.isoformat()
