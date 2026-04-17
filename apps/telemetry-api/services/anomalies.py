"""Anomaly detection — threshold breaches and Z-score spikes.

Two strategies run on every ingestion cycle:

1. Threshold breach: current in_util_pct or out_util_pct above 60 %. Severity
   escalates at 80 % (high) and 95 % (critical). These produce events even
   without any historical baseline.
2. Spike: current 5-minute bytes_estimated vs the matching (device, interface,
   hour_of_day) baseline from PR 7. Z = (cur - mean) / stddev, fires at Z >= 3.

Events are persisted to `anomaly_events` with a human-readable `summary` so the
MCP layer can surface them verbatim to callers.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import AsyncSessionLocal
from db.models import (
    AnomalyEvent,
    BaselineSnapshot,
    FlowSummaryMinute,
    InterfaceUtilizationMinute,
)

log = logging.getLogger(__name__)

UTIL_THRESHOLD_WARN = 60.0
UTIL_THRESHOLD_HIGH = 80.0
UTIL_THRESHOLD_CRITICAL = 95.0
SPIKE_ZSCORE_THRESHOLD = 3.0
SPIKE_LOOKBACK_MINUTES = 5


def util_severity(pct: float) -> str:
    if pct >= UTIL_THRESHOLD_CRITICAL:
        return "critical"
    if pct >= UTIL_THRESHOLD_HIGH:
        return "high"
    if pct >= UTIL_THRESHOLD_WARN:
        return "medium"
    return "low"


async def detect_threshold_breaches(session: AsyncSession) -> list[AnomalyEvent]:
    """Find the latest utilization sample per device/interface and flag breaches."""
    subq = (
        select(
            InterfaceUtilizationMinute.device,
            InterfaceUtilizationMinute.interface,
            func.max(InterfaceUtilizationMinute.ts_bucket).label("latest_ts"),
        )
        .group_by(
            InterfaceUtilizationMinute.device,
            InterfaceUtilizationMinute.interface,
        )
        .subquery()
    )
    q = (
        select(InterfaceUtilizationMinute)
        .join(
            subq,
            (InterfaceUtilizationMinute.device == subq.c.device)
            & (InterfaceUtilizationMinute.interface == subq.c.interface)
            & (InterfaceUtilizationMinute.ts_bucket == subq.c.latest_ts),
        )
        .where(
            (InterfaceUtilizationMinute.in_util_pct >= UTIL_THRESHOLD_WARN)
            | (InterfaceUtilizationMinute.out_util_pct >= UTIL_THRESHOLD_WARN)
        )
    )
    result = await session.execute(q)
    rows = result.scalars().all()

    events = []
    now = datetime.now(timezone.utc)
    for row in rows:
        max_util = max(row.in_util_pct, row.out_util_pct)
        direction = "inbound" if row.in_util_pct > row.out_util_pct else "outbound"
        events.append(
            AnomalyEvent(
                ts=now,
                scope=f"device:{row.device}/interface:{row.interface}",
                anomaly_type="threshold_breach",
                severity=util_severity(max_util),
                summary=(
                    f"{row.interface} on {row.device} is at "
                    f"{max_util:.1f}% {direction} utilization."
                ),
                metadata_json={
                    "in_util_pct": row.in_util_pct,
                    "out_util_pct": row.out_util_pct,
                    "sample_ts": row.ts_bucket.isoformat(),
                },
            )
        )
    return events


async def detect_spikes(session: AsyncSession) -> list[AnomalyEvent]:
    """Compare current 5-min bytes vs hourly baseline using Z-score."""
    now = datetime.now(timezone.utc)
    hour = now.hour
    since = now - timedelta(minutes=SPIKE_LOOKBACK_MINUTES)

    cur_q = (
        select(
            FlowSummaryMinute.device,
            FlowSummaryMinute.interface,
            func.sum(FlowSummaryMinute.bytes_estimated).label("current_bytes"),
        )
        .where(FlowSummaryMinute.ts_bucket >= since)
        .group_by(FlowSummaryMinute.device, FlowSummaryMinute.interface)
    )
    cur_rows = (await session.execute(cur_q)).all()
    current = {(r.device, r.interface): int(r.current_bytes) for r in cur_rows}

    bl_q = select(BaselineSnapshot).where(
        (BaselineSnapshot.hour_of_day == hour)
        & (BaselineSnapshot.metric == "bytes")
    )
    baselines = {
        (b.device, b.interface): b
        for b in (await session.execute(bl_q)).scalars().all()
    }

    events = []
    for (device, interface), cur_bytes in current.items():
        bl = baselines.get((device, interface))
        if bl is None or bl.sample_count < 3 or bl.stddev_value == 0:
            continue
        z = (cur_bytes - bl.mean_value) / bl.stddev_value
        if z < SPIKE_ZSCORE_THRESHOLD:
            continue
        severity = "critical" if z >= 6 else "high" if z >= 5 else "medium"
        events.append(
            AnomalyEvent(
                ts=now,
                scope=f"device:{device}/interface:{interface}",
                anomaly_type="spike",
                severity=severity,
                summary=(
                    f"Traffic spike on {interface} ({device}): "
                    f"{cur_bytes / 1e9:.2f} GB in last "
                    f"{SPIKE_LOOKBACK_MINUTES} min vs baseline "
                    f"{bl.mean_value / 1e9:.2f} GB (Z={z:.1f})."
                ),
                metadata_json={
                    "z_score": round(z, 2),
                    "current_bytes": cur_bytes,
                    "baseline_mean": bl.mean_value,
                    "baseline_stddev": bl.stddev_value,
                    "baseline_sample_count": bl.sample_count,
                },
            )
        )
    return events


async def run_anomaly_detection() -> int:
    """Run all detectors and persist events. Returns number of events written."""
    async with AsyncSessionLocal() as session:
        threshold_events = await detect_threshold_breaches(session)
        spike_events = await detect_spikes(session)
        all_events = threshold_events + spike_events
        if all_events:
            session.add_all(all_events)
            await session.commit()
    if all_events:
        log.info(
            f"Wrote {len(all_events)} anomaly events "
            f"(threshold={len(threshold_events)}, spike={len(spike_events)})"
        )
    return len(all_events)


ANOMALY_INTERVAL_SECONDS = 30


async def anomaly_loop():
    import asyncio

    log.info("Starting anomaly detection loop")
    while True:
        try:
            await run_anomaly_detection()
        except Exception as e:
            log.error(f"Anomaly detection error: {e}", exc_info=True)
        await asyncio.sleep(ANOMALY_INTERVAL_SECONDS)
