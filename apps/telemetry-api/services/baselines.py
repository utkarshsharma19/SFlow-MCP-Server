"""Rolling baseline computation — feeds spike detection in anomalies.py.

Strategy: for each (tenant, device, interface, hour_of_day) tuple, compute
the mean and population stddev of per-minute bytes_estimated over a 7-day
rolling window. Diurnal bucketing keeps the baseline honest for traffic
patterns that vary by time of day. Tenant isolation from PR 20 ensures
one tenant's baseline never contaminates another's.
"""
import asyncio
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from db import AsyncSessionLocal
from db.models import BaselineSnapshot, FlowSummaryMinute

log = logging.getLogger(__name__)

BASELINE_WINDOW_DAYS = 7
BASELINE_INTERVAL_SECONDS = 300   # recompute every 5 minutes
MIN_SAMPLES_FOR_BASELINE = 3


async def compute_baselines():
    """Compute rolling baselines per tenant/device/interface/hour_of_day."""
    since = datetime.now(timezone.utc) - timedelta(days=BASELINE_WINDOW_DAYS)
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        q = (
            select(
                FlowSummaryMinute.tenant_id,
                FlowSummaryMinute.device,
                FlowSummaryMinute.interface,
                func.extract("hour", FlowSummaryMinute.ts_bucket).label("hour"),
                FlowSummaryMinute.ts_bucket,
                func.sum(FlowSummaryMinute.bytes_estimated).label("total_bytes"),
            )
            .where(FlowSummaryMinute.ts_bucket >= since)
            .group_by(
                FlowSummaryMinute.tenant_id,
                FlowSummaryMinute.device,
                FlowSummaryMinute.interface,
                "hour",
                FlowSummaryMinute.ts_bucket,
            )
        )
        rows = (await session.execute(q)).all()

    grouped: dict = defaultdict(list)
    for row in rows:
        key = (str(row.tenant_id), row.device, row.interface, int(row.hour))
        grouped[key].append(float(row.total_bytes))

    snapshots = []
    for (tenant_id, device, interface, hour), values in grouped.items():
        n = len(values)
        if n < MIN_SAMPLES_FOR_BASELINE:
            continue
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n
        stddev = math.sqrt(variance)
        snapshots.append(
            BaselineSnapshot(
                tenant_id=tenant_id,
                computed_at=now,
                device=device,
                interface=interface,
                hour_of_day=hour,
                metric="bytes",
                mean_value=mean,
                stddev_value=stddev,
                sample_count=n,
            )
        )

    if snapshots:
        async with AsyncSessionLocal() as session:
            session.add_all(snapshots)
            await session.commit()

    log.info(f"Computed {len(snapshots)} baseline snapshots")


async def baseline_loop():
    log.info("Starting baseline computation loop")
    while True:
        try:
            await compute_baselines()
        except Exception as e:
            log.error(f"Baseline computation error: {e}", exc_info=True)
        await asyncio.sleep(BASELINE_INTERVAL_SECONDS)
