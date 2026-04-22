"""Anomaly dedup + lifecycle helpers (PR 26).

Detectors call :func:`record_anomaly` instead of inserting directly so
that recurring conditions collapse into a single open row. The open-event
partial unique index enforces the invariant at the DB layer; this module
just expresses the upsert + fingerprint rules in one place.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AnomalyEvent


def fingerprint_for(
    tenant_id: str,
    anomaly_type: str,
    scope: str,
    cause: dict[str, Any] | None = None,
) -> str:
    """Stable 64-char hex fingerprint keyed on dedup-relevant fields.

    The cause dict is optional and should carry only fields that
    distinguish distinct conditions (e.g. ``queue_id`` for per-queue PFC
    storms). Volatile fields — timestamps, rolling averages — must be
    omitted, or every tick will look like a new condition.
    """
    cause_repr = (
        json.dumps(cause, sort_keys=True, separators=(",", ":")) if cause else ""
    )
    payload = f"{tenant_id}|{anomaly_type}|{scope}|{cause_repr}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RecordedAnomaly:
    id: str
    is_new: bool
    occurrence_count: int


async def record_anomaly(
    db: AsyncSession,
    *,
    tenant_id: str,
    anomaly_type: str,
    severity: str,
    scope: str,
    summary: str,
    cause: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> RecordedAnomaly:
    """Upsert: open a new event or bump an existing open one.

    - If no open row exists for the fingerprint: insert a fresh row with
      ``occurrence_count = 1`` and ``first_seen_at = last_seen_at = now``.
    - If an open row exists: increment ``occurrence_count``, refresh
      ``last_seen_at`` and ``severity`` (severity only ever goes up), and
      merge the new cause into ``metadata_json``.
    """
    now = now or datetime.now(timezone.utc)
    fp = fingerprint_for(tenant_id, anomaly_type, scope, cause)

    stmt = (
        pg_insert(AnomalyEvent)
        .values(
            tenant_id=tenant_id,
            ts=now,
            scope=scope,
            anomaly_type=anomaly_type,
            severity=severity,
            summary=summary,
            metadata_json=metadata,
            fingerprint=fp,
            first_seen_at=now,
            last_seen_at=now,
            occurrence_count=1,
        )
        .on_conflict_do_update(
            index_elements=["tenant_id", "fingerprint"],
            index_where=AnomalyEvent.resolved_at.is_(None)
            & AnomalyEvent.fingerprint.isnot(None),
            set_={
                "last_seen_at": now,
                "occurrence_count": AnomalyEvent.occurrence_count + 1,
                "severity": _escalate_sql(severity),
                "summary": summary,
                "metadata_json": metadata,
            },
        )
        .returning(
            AnomalyEvent.id,
            AnomalyEvent.occurrence_count,
            AnomalyEvent.first_seen_at,
        )
    )
    row = (await db.execute(stmt)).one()
    await db.commit()
    return RecordedAnomaly(
        id=str(row.id),
        is_new=row.occurrence_count == 1,
        occurrence_count=row.occurrence_count,
    )


def _escalate_sql(new_severity: str):
    """SQL expression: keep the more severe of current vs new.

    Severity is stored as text; we cast via the SEVERITY_RANK CASE rather
    than rely on lexicographic ordering (which would put 'critical' < 'low').
    """
    from sqlalchemy import case

    rank = case(
        (AnomalyEvent.severity == "low", 1),
        (AnomalyEvent.severity == "medium", 2),
        (AnomalyEvent.severity == "high", 3),
        (AnomalyEvent.severity == "critical", 4),
        else_=0,
    )
    new_rank = {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(new_severity, 0)
    return case(
        (rank >= new_rank, AnomalyEvent.severity),
        else_=new_severity,
    )


async def acknowledge(
    db: AsyncSession,
    *,
    tenant_id: str,
    anomaly_id: str,
    api_key_id: str,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(timezone.utc)
    result = await db.execute(
        update(AnomalyEvent)
        .where(AnomalyEvent.id == anomaly_id)
        .where(AnomalyEvent.tenant_id == tenant_id)
        .where(AnomalyEvent.acknowledged_at.is_(None))
        .values(acknowledged_at=now, acknowledged_by_api_key_id=api_key_id)
    )
    await db.commit()
    return result.rowcount > 0


async def resolve(
    db: AsyncSession,
    *,
    tenant_id: str,
    anomaly_id: str,
    api_key_id: str,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(timezone.utc)
    result = await db.execute(
        update(AnomalyEvent)
        .where(AnomalyEvent.id == anomaly_id)
        .where(AnomalyEvent.tenant_id == tenant_id)
        .where(AnomalyEvent.resolved_at.is_(None))
        .values(resolved_at=now, resolved_by_api_key_id=api_key_id)
    )
    await db.commit()
    return result.rowcount > 0
