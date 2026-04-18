"""gNMI ingestion loop — pulls device state from configured targets.

Mirrors the structure of services/ingest.py but for OpenConfig YANG paths.
Persists into device_state_minute, bgp_session_minute, queue_stats_minute.
Tenant assignment uses DEFAULT_TENANT_ID until PR 22 introduces per-source
mapping.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import List

import otel
from collectors.gnmi_client import GNMIClient
from db import AsyncSessionLocal
from db.models import (
    BGPSessionMinute,
    DEFAULT_TENANT_ID,
    DeviceStateMinute,
    QueueStatsMinute,
)
from services.tenant_routing import SOURCE_KIND_GNMI, TenantRouter, get_router
from shared.schemas.device_state import (
    BGPNeighborState,
    InterfaceState,
    QueueState,
)

log = logging.getLogger(__name__)

GNMI_POLL_INTERVAL_SECONDS = int(os.getenv("GNMI_POLL_INTERVAL_SECONDS", "60"))


def _bucket(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


async def _resolve_devices(
    devices: set[str], router: TenantRouter | None
) -> dict[str, str]:
    if router is None:
        return {d: DEFAULT_TENANT_ID for d in devices}
    return {d: await router.tenant_for(SOURCE_KIND_GNMI, d) for d in devices}


async def normalize_interface_state(
    states: List[InterfaceState],
    router: TenantRouter | None = None,
) -> list[dict]:
    bucket = _bucket(datetime.now(timezone.utc))
    tenants = await _resolve_devices({s.device for s in states}, router)
    return [
        dict(
            tenant_id=tenants[s.device],
            ts_bucket=bucket,
            device=s.device,
            interface=s.interface,
            admin_status=s.admin_status,
            oper_status=s.oper_status,
            last_change=s.last_change,
            speed_bps=s.speed_bps,
            mtu=s.mtu,
            description=s.description,
        )
        for s in states
    ]


async def normalize_bgp_neighbors(
    sessions: List[BGPNeighborState],
    router: TenantRouter | None = None,
) -> list[dict]:
    bucket = _bucket(datetime.now(timezone.utc))
    tenants = await _resolve_devices({s.device for s in sessions}, router)
    return [
        dict(
            tenant_id=tenants[s.device],
            ts_bucket=bucket,
            device=s.device,
            peer_address=s.peer_address,
            peer_as=s.peer_as,
            session_state=s.session_state,
            uptime_seconds=s.uptime_seconds,
            prefixes_received=s.prefixes_received,
            prefixes_sent=s.prefixes_sent,
            last_error=s.last_error,
        )
        for s in sessions
    ]


async def normalize_queue_stats(
    queues: List[QueueState],
    router: TenantRouter | None = None,
) -> list[dict]:
    bucket = _bucket(datetime.now(timezone.utc))
    tenants = await _resolve_devices({q.device for q in queues}, router)
    return [
        dict(
            tenant_id=tenants[q.device],
            ts_bucket=bucket,
            device=q.device,
            interface=q.interface,
            queue_id=q.queue_id,
            traffic_class=q.traffic_class,
            max_depth_bytes=q.max_depth_bytes,
            avg_depth_bytes=q.avg_depth_bytes,
            pfc_pause_rx=q.pfc_pause_rx,
            pfc_pause_tx=q.pfc_pause_tx,
            ecn_marked_packets=q.ecn_marked_packets,
            dropped_packets=q.dropped_packets,
        )
        for q in queues
    ]


async def gnmi_ingestion_loop(client: GNMIClient) -> None:
    """Poll all gNMI targets every GNMI_POLL_INTERVAL_SECONDS."""
    if not client.enabled:
        log.info(
            "gNMI ingestion idle — no targets configured or pygnmi missing. "
            "Loop will continue to no-op."
        )

    log.info(f"Starting gNMI ingestion loop (interval={GNMI_POLL_INTERVAL_SECONDS}s)")
    tracer = otel.get_tracer("flowmind.gnmi")
    router = get_router()

    while True:
        start = time.monotonic()
        span_cm = (
            tracer.start_as_current_span("gnmi.cycle")
            if tracer is not None
            else _noop_cm()
        )
        try:
            with span_cm:
                if not client.enabled:
                    await asyncio.sleep(GNMI_POLL_INTERVAL_SECONDS)
                    continue

                interfaces = await client.get_interface_state()
                neighbors = await client.get_bgp_neighbors()
                queues = await client.get_queue_stats()

                if_rows = await normalize_interface_state(interfaces, router)
                bgp_rows = await normalize_bgp_neighbors(neighbors, router)
                queue_rows = await normalize_queue_stats(queues, router)

                async with AsyncSessionLocal() as session:
                    if if_rows:
                        session.add_all([DeviceStateMinute(**r) for r in if_rows])
                    if bgp_rows:
                        session.add_all([BGPSessionMinute(**r) for r in bgp_rows])
                    if queue_rows:
                        session.add_all([QueueStatsMinute(**r) for r in queue_rows])
                    await session.commit()

                if otel.flows_ingested is not None:
                    otel.flows_ingested.add(
                        len(if_rows), {"result": "ok", "kind": "gnmi_iface"}
                    )
                    otel.flows_ingested.add(
                        len(bgp_rows), {"result": "ok", "kind": "gnmi_bgp"}
                    )
                    otel.flows_ingested.add(
                        len(queue_rows), {"result": "ok", "kind": "gnmi_queue"}
                    )
                if otel.ingestion_duration is not None:
                    otel.ingestion_duration.record(
                        time.monotonic() - start, {"phase": "gnmi_cycle"}
                    )

                log.info(
                    f"gNMI: {len(if_rows)} iface, {len(bgp_rows)} bgp, "
                    f"{len(queue_rows)} queue rows"
                )
        except Exception as e:
            log.error(f"gNMI ingestion error: {e}", exc_info=True)
            if otel.flows_ingested is not None:
                otel.flows_ingested.add(1, {"result": "error", "kind": "gnmi"})

        await asyncio.sleep(GNMI_POLL_INTERVAL_SECONDS)


class _noop_cm:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
