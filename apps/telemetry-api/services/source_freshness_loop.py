"""Background loop that scans source_freshness and opens silent-collector anomalies."""
from __future__ import annotations

import asyncio
import logging

from db import AsyncSessionLocal
from services.source_freshness import scan_stale_sources

log = logging.getLogger(__name__)

TICK_SECONDS = 30


async def source_freshness_loop() -> None:
    while True:
        try:
            async with AsyncSessionLocal() as session:
                await scan_stale_sources(session)
        except Exception:  # noqa: BLE001 — log + retry next tick
            log.exception("source freshness scan failed")
        await asyncio.sleep(TICK_SECONDS)
