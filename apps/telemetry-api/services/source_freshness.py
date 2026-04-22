"""Per-source ingest heartbeats + stale-source detection (PR 26).

Ingestion loops call :func:`record_heartbeat` on every successful tick;
the ``scan_stale_sources`` coroutine then compares ``last_ingest_ts``
against per-kind thresholds and emits a ``collector_silent`` anomaly
for anything that's drifted out of the fresh band.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import SourceFreshness
from services.anomaly_dedup import record_anomaly

# Thresholds are per-source-kind because cadences differ: sFlow pushes
# every minute, gNMI can be dial-out at 5–10s, so "silent" means
# different things. Operators can override via env vars in future work.
STALE_THRESHOLDS: dict[str, timedelta] = {
    "sflow": timedelta(minutes=3),
    "gnmi": timedelta(seconds=90),
}
SILENT_MULTIPLIER = 5  # stale × this = silent (critical)


async def record_heartbeat(
    db: AsyncSession,
    *,
    tenant_id: str,
    source_kind: str,
    device: str,
    sample_count: int,
    now: datetime | None = None,
) -> None:
    """Upsert the heartbeat row for a source.

    ``sample_count`` is the number of rows written on this tick — a
    non-zero ingest but a wildly low count is a secondary signal the
    baseline detector can use later.
    """
    now = now or datetime.now(timezone.utc)
    stmt = (
        pg_insert(SourceFreshness)
        .values(
            tenant_id=tenant_id,
            source_kind=source_kind,
            device=device,
            last_ingest_ts=now,
            last_sample_count=sample_count,
            status="fresh",
            updated_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_source_freshness",
            set_={
                "last_ingest_ts": now,
                "last_sample_count": sample_count,
                "status": "fresh",
                "updated_at": now,
            },
        )
    )
    await db.execute(stmt)
    await db.commit()


def classify(now: datetime, last_ingest: datetime, source_kind: str) -> str:
    """Return one of ``fresh|stale|silent`` based on last_ingest age."""
    threshold = STALE_THRESHOLDS.get(source_kind, timedelta(minutes=5))
    age = now - last_ingest
    if age >= threshold * SILENT_MULTIPLIER:
        return "silent"
    if age >= threshold:
        return "stale"
    return "fresh"


async def scan_stale_sources(
    db: AsyncSession,
    now: datetime | None = None,
) -> list[dict]:
    """Scan every tracked source, promote status, and open anomalies.

    Silent sources open a ``collector_silent`` anomaly. Stale ones
    (transient drift) are just reported in the return value so the
    operator UI can warn without paging.
    """
    now = now or datetime.now(timezone.utc)
    rows: Iterable[SourceFreshness] = (
        await db.execute(select(SourceFreshness))
    ).scalars().all()

    findings: list[dict] = []
    for row in rows:
        new_status = classify(now, row.last_ingest_ts, row.source_kind)
        if new_status != row.status:
            row.status = new_status
            row.updated_at = now

        if new_status == "silent":
            await record_anomaly(
                db,
                tenant_id=str(row.tenant_id),
                anomaly_type="collector_silent",
                severity="critical",
                scope=f"device:{row.device}",
                summary=(
                    f"{row.source_kind} collector for {row.device} has not "
                    f"ingested since {row.last_ingest_ts.isoformat()}"
                ),
                cause={"source_kind": row.source_kind, "device": row.device},
                metadata={
                    "last_ingest_ts": row.last_ingest_ts.isoformat(),
                    "last_sample_count": row.last_sample_count,
                },
                now=now,
            )

        findings.append(
            {
                "tenant_id": str(row.tenant_id),
                "source_kind": row.source_kind,
                "device": row.device,
                "last_ingest_ts": row.last_ingest_ts.isoformat(),
                "status": new_status,
            }
        )

    await db.commit()
    return findings
