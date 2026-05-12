"""Per-link timeline service — the operator's drill-down primitive (PR 30).

When ``get_fabric_health`` flags a hot interface or anomaly fires on a
link, the chatbot's next question is "what's happened on this link over
the last hour?" This service produces the timeline in one round-trip:

* a minute-bucketed utilization series (sFlow-derived, sampled)
* an error/discard series from the same counter feed
* a list of flap events derived from ``DeviceStateMinute.last_change``
* a list of open anomaly_events scoped to this interface in the window

Resolution is configurable so the same tool serves "show me the last
hour" (1-min) and "show me last 24h" (5-min downsampled) without two
endpoints.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    AnomalyEvent,
    DeviceStateMinute,
    InterfaceUtilizationMinute,
)

UTIL_BREACH_THRESHOLD_PCT = 80.0
MAX_BUCKETS = 288  # 24h at 5-min, ~5h at 1-min — keeps responses bounded


async def get_link_history(
    db: AsyncSession,
    tenant_id: str,
    device: str,
    interface: str,
    window_minutes: int = 60,
    bucket_minutes: int = 5,
) -> dict:
    """Return util + errors + flaps + anomalies for one interface.

    The window is clamped at the route layer (1..1440 min); the bucket
    width is the *downsampling* granularity — utilization rows are
    stored per minute, but the chatbot rarely wants 1440 points back.
    """
    if bucket_minutes < 1:
        bucket_minutes = 1
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=window_minutes)
    bucket_count = max(1, window_minutes // bucket_minutes)
    if bucket_count > MAX_BUCKETS:
        # Bump the bucket width rather than silently truncating — the
        # chatbot should never see a half-rendered chart.
        bucket_minutes = max(1, window_minutes // MAX_BUCKETS)
        bucket_count = MAX_BUCKETS

    util_rows = await _util_rows(db, tenant_id, device, interface, since)
    state_rows = await _state_rows(db, tenant_id, device, interface, since)

    series = _bucket_series(util_rows, since, bucket_minutes, bucket_count)
    flaps = _detect_flaps(state_rows, since)
    breaches = [
        {
            "ts": b["ts"],
            "max_util_pct": b["max_util_pct"],
        }
        for b in series
        if b["max_util_pct"] >= UTIL_BREACH_THRESHOLD_PCT
    ]
    anomalies = await _scoped_anomalies(db, tenant_id, device, interface, since)

    return {
        "device": device,
        "interface": interface,
        "window_minutes": window_minutes,
        "bucket_minutes": bucket_minutes,
        "series": series,
        "summary": _summary(series),
        "threshold_breaches": breaches,
        "flaps": flaps,
        "open_anomalies": anomalies,
        "confidence_note": _confidence_note(util_rows, state_rows),
    }


async def _util_rows(
    db: AsyncSession,
    tenant_id: str,
    device: str,
    interface: str,
    since: datetime,
):
    q = (
        select(InterfaceUtilizationMinute)
        .where(InterfaceUtilizationMinute.tenant_id == tenant_id)
        .where(InterfaceUtilizationMinute.device == device)
        .where(InterfaceUtilizationMinute.interface == interface)
        .where(InterfaceUtilizationMinute.ts_bucket >= since)
        .order_by(InterfaceUtilizationMinute.ts_bucket)
    )
    return (await db.execute(q)).scalars().all()


async def _state_rows(
    db: AsyncSession,
    tenant_id: str,
    device: str,
    interface: str,
    since: datetime,
):
    q = (
        select(DeviceStateMinute)
        .where(DeviceStateMinute.tenant_id == tenant_id)
        .where(DeviceStateMinute.device == device)
        .where(DeviceStateMinute.interface == interface)
        .where(DeviceStateMinute.ts_bucket >= since)
        .order_by(DeviceStateMinute.ts_bucket)
    )
    return (await db.execute(q)).scalars().all()


def _bucket_series(
    rows,
    since: datetime,
    bucket_minutes: int,
    bucket_count: int,
) -> list[dict]:
    """Downsample minute rows into ``bucket_minutes`` bins.

    A bin with no samples is emitted with ``samples=0`` rather than
    omitted — leaving gaps in the array confuses chart code in the
    chatbot front-end far more than zeros do.
    """
    by_bucket: dict[int, list] = defaultdict(list)
    for r in rows:
        offset = int((r.ts_bucket - since).total_seconds() // 60)
        idx = min(bucket_count - 1, max(0, offset // bucket_minutes))
        by_bucket[idx].append(r)

    series = []
    for i in range(bucket_count):
        bucket_start = since + timedelta(minutes=i * bucket_minutes)
        rs = by_bucket.get(i, [])
        if rs:
            avg_in = sum(r.in_util_pct for r in rs) / len(rs)
            avg_out = sum(r.out_util_pct for r in rs) / len(rs)
            max_in = max(r.in_util_pct for r in rs)
            max_out = max(r.out_util_pct for r in rs)
            errors = sum(int(r.error_count or 0) for r in rs)
            samples = len(rs)
        else:
            avg_in = avg_out = max_in = max_out = 0.0
            errors = 0
            samples = 0
        series.append(
            {
                "ts": bucket_start.isoformat(),
                "avg_in_util_pct": round(avg_in, 2),
                "avg_out_util_pct": round(avg_out, 2),
                "max_in_util_pct": round(max_in, 2),
                "max_out_util_pct": round(max_out, 2),
                "max_util_pct": round(max(max_in, max_out), 2),
                "errors": errors,
                "samples": samples,
            }
        )
    return series


def _detect_flaps(state_rows, since: datetime) -> list[dict]:
    """A flap is a ``last_change`` transition observed inside the window.

    OpenConfig reports ``last_change`` as a timestamp on the interface
    state; if it advances between samples, the link transitioned. We
    treat each distinct ``last_change`` ≥ since as one flap and report
    the oper_status at the sample that observed it.
    """
    if not state_rows:
        return []
    flaps: list[dict] = []
    seen_changes: set[datetime] = set()
    for row in state_rows:
        if row.last_change is None:
            continue
        if row.last_change < since:
            continue
        if row.last_change in seen_changes:
            continue
        seen_changes.add(row.last_change)
        flaps.append(
            {
                "observed_at": row.ts_bucket.isoformat(),
                "last_change": row.last_change.isoformat(),
                "admin_status": row.admin_status,
                "oper_status": row.oper_status,
            }
        )
    return sorted(flaps, key=lambda f: f["last_change"])


async def _scoped_anomalies(
    db: AsyncSession,
    tenant_id: str,
    device: str,
    interface: str,
    since: datetime,
) -> list[dict]:
    """Open anomalies whose ``scope`` matches this interface.

    Detectors write scopes like ``interface:leaf1/Eth0`` and
    ``device:leaf1``; both are relevant when the operator is staring at
    one link, so we match either.
    """
    iface_scope = f"interface:{device}/{interface}"
    device_scope = f"device:{device}"
    q = (
        select(AnomalyEvent)
        .where(AnomalyEvent.tenant_id == tenant_id)
        .where(AnomalyEvent.last_seen_at >= since)
        .where(AnomalyEvent.resolved_at.is_(None))
        .where(AnomalyEvent.scope.in_((iface_scope, device_scope)))
        .order_by(AnomalyEvent.last_seen_at.desc())
        .limit(20)
    )
    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "id": str(r.id),
            "type": r.anomaly_type,
            "severity": r.severity,
            "scope": r.scope,
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
            "occurrence_count": int(r.occurrence_count),
            "summary": r.summary,
        }
        for r in rows
    ]


def _summary(series: list[dict]) -> dict:
    """Flatten the series down to the few numbers the LLM actually quotes."""
    if not series:
        return {
            "avg_util_pct": 0.0,
            "peak_util_pct": 0.0,
            "total_errors": 0,
            "buckets_with_samples": 0,
        }
    avg = sum(b["max_util_pct"] for b in series) / len(series)
    peak = max(b["max_util_pct"] for b in series)
    errs = sum(b["errors"] for b in series)
    with_data = sum(1 for b in series if b["samples"] > 0)
    return {
        "avg_util_pct": round(avg, 2),
        "peak_util_pct": round(peak, 2),
        "total_errors": int(errs),
        "buckets_with_samples": with_data,
    }


def _confidence_note(util_rows, state_rows) -> str:
    """Be honest about which feed is missing.

    The chatbot should not claim "the link looked fine" when sFlow was
    silent for the window — that's a data gap, not a healthy link.
    """
    parts = [
        "Utilization derived from sFlow-RT counter samples (sampled, ±10%)."
    ]
    if not util_rows:
        parts.append("No counter samples in window — link history is empty.")
    if not state_rows:
        parts.append("No gNMI state samples — flap detection unavailable.")
    return " ".join(parts)
