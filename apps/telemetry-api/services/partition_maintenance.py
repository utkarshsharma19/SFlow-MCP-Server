"""Periodic partition + retention worker (PR 26).

Runs once an hour in the API lifespan. Two jobs:

1. Call ``ensure_monthly_partitions`` for every partitioned parent table
   so that a month rollover never lands in a partition that doesn't
   exist yet (which would crash the ingest path).

2. Call ``drop_partitions_older_than`` against configured retention
   windows. Retention is coarse on purpose: monthly-granularity
   partitions can't drop a few days at a time.

Retention is expressed per-table because flow samples are cheap to
retain for compliance but queue counters explode quickly on large
fabrics.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta

from sqlalchemy import text

from db import AsyncSessionLocal

log = logging.getLogger(__name__)

PARTITIONED_TABLES = (
    "flow_summary_minute",
    "interface_utilization_minute",
    "queue_stats_minute",
)

# Default retention — overridable via env for regulated tenants who
# need longer windows. Expressed as Postgres intervals.
RETENTION_DEFAULTS = {
    "flow_summary_minute": os.getenv("FLOWMIND_RETENTION_FLOW", "90 days"),
    "interface_utilization_minute": os.getenv("FLOWMIND_RETENTION_UTIL", "180 days"),
    "queue_stats_minute": os.getenv("FLOWMIND_RETENTION_QUEUE", "30 days"),
}

TICK_INTERVAL = timedelta(hours=1)


async def _tick() -> None:
    async with AsyncSessionLocal() as session:
        for table in PARTITIONED_TABLES:
            # Keep two months of forward partitions so a mid-month deploy
            # doesn't leave us one-off when the rollover hits.
            await session.execute(
                text("SELECT ensure_monthly_partitions(:table, 2)"),
                {"table": table},
            )
            dropped = (
                await session.execute(
                    text(
                        "SELECT drop_partitions_older_than(:table, :older::interval)"
                    ),
                    {"table": table, "older": RETENTION_DEFAULTS[table]},
                )
            ).scalar()
            if dropped:
                log.info(
                    "retention: dropped %s partition(s) from %s", dropped, table
                )
        await session.commit()


async def partition_maintenance_loop() -> None:
    """Tick forever. Designed to run inside FastAPI's lifespan."""
    while True:
        try:
            await _tick()
        except Exception:  # noqa: BLE001 — logged + retry on next tick
            log.exception("partition maintenance tick failed")
        await asyncio.sleep(TICK_INTERVAL.total_seconds())
