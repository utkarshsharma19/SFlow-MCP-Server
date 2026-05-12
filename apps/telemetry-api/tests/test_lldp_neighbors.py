"""LLDP neighbor service tests (PR 30)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.lldp_neighbors import (
    STALE_NEIGHBOR_HOURS,
    _confidence_note,
    _is_stale,
)


def test_is_stale_threshold():
    now = datetime.now(timezone.utc)
    just_now = now - timedelta(minutes=10)
    yesterday = now - timedelta(hours=STALE_NEIGHBOR_HOURS + 1)
    assert _is_stale(now, just_now) is False
    assert _is_stale(now, yesterday) is True


def test_confidence_note_empty_explains_three_causes():
    """An empty neighbor list isn't a bug — explain the three legitimate causes."""
    note = _confidence_note([])
    assert "LLDP" in note
    assert "gNMI" in note
    assert "L2 adjacencies" in note


def test_confidence_note_flags_stale_count():
    neighbors = [
        {"is_stale": False},
        {"is_stale": True},
        {"is_stale": True},
    ]
    note = _confidence_note(neighbors)
    assert "3 LLDP adjacencies" in note
    assert "2 entry" in note or "2 entries" in note
    assert "pulled" in note


def test_confidence_note_no_stale_is_clean():
    neighbors = [{"is_stale": False}]
    note = _confidence_note(neighbors)
    assert "1 LLDP adjacencies" in note
    assert "pulled" not in note
