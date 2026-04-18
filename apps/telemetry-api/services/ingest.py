"""Ingestion loop — polls sFlow-RT, normalizes, and writes to Postgres.

Sampling-rate correction is the critical invariant here: every raw
byte/packet count from sFlow-RT is multiplied by sampling_rate before
persistence, and sampling_rate itself is always stored alongside the
estimate so downstream code can compute confidence notes.
"""
import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import List

import otel
from collectors.sflow_rt_client import SFlowRTClient
from db import AsyncSessionLocal
from db.models import DEFAULT_TENANT_ID, FlowSummaryMinute, InterfaceUtilizationMinute
from services.tenant_routing import SOURCE_KIND_SFLOW, TenantRouter, get_router
from shared.schemas.flow import FlowRecord
from shared.schemas.interface import InterfaceCounter

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30


def minute_bucket(dt: datetime) -> datetime:
    """Truncate a datetime to minute granularity."""
    return dt.replace(second=0, microsecond=0)


async def _resolve_tenant(router: TenantRouter | None, agent: str) -> str:
    """Look up the tenant for an sFlow agent. Falls back to DEFAULT_TENANT_ID."""
    if router is None:
        return DEFAULT_TENANT_ID
    return await router.tenant_for(SOURCE_KIND_SFLOW, agent)


async def normalize_flows(
    records: List[FlowRecord],
    router: TenantRouter | None = None,
) -> List[dict]:
    """Group raw FlowRecords into per-minute buckets and apply sampling correction.

    Key rule: bytes_estimated = raw_bytes * sampling_rate.

    Per-source → tenant routing (PR 22): each agent's tenant_id is looked
    up via the cached TenantRouter. Unmapped agents fall through to
    DEFAULT_TENANT_ID so single-tenant installs still work.
    """
    buckets: dict = defaultdict(
        lambda: {"bytes": 0, "packets": 0, "sampling_rate": 1}
    )
    for r in records:
        key = (
            minute_bucket(r.timestamp),
            r.agent,
            str(r.input_if_index),
            r.src_ip,
            r.dst_ip,
            r.protocol,
        )
        buckets[key]["bytes"] += r.bytes * r.sampling_rate
        buckets[key]["packets"] += r.packets * r.sampling_rate
        buckets[key]["sampling_rate"] = r.sampling_rate

    # Resolve tenant once per distinct agent — cheap because the router
    # is in-process cached.
    distinct_agents = {device for (_, device, _, _, _, _) in buckets.keys()}
    tenant_for_agent: dict[str, str] = {
        agent: await _resolve_tenant(router, agent) for agent in distinct_agents
    }

    rows = []
    for (ts_bucket, device, interface, src_ip, dst_ip, protocol), vals in buckets.items():
        rows.append(
            dict(
                tenant_id=tenant_for_agent[device],
                ts_bucket=ts_bucket,
                device=device,
                interface=interface,
                src_ip=src_ip,
                dst_ip=dst_ip,
                protocol=protocol,
                bytes_estimated=vals["bytes"],
                packets_estimated=vals["packets"],
                sampling_rate=vals["sampling_rate"],
            )
        )
    return rows


async def normalize_counters(
    counters: List[InterfaceCounter],
    router: TenantRouter | None = None,
) -> List[dict]:
    """Convert raw byte counters to per-minute utilization percentages.

    v1 assumes the polling window equals POLL_INTERVAL_SECONDS. v2 should
    store the previous counter snapshot and compute proper deltas.
    """
    rows = []
    now = minute_bucket(datetime.now(timezone.utc))
    distinct_agents = {c.agent for c in counters}
    tenant_for_agent: dict[str, str] = {
        agent: await _resolve_tenant(router, agent) for agent in distinct_agents
    }
    for c in counters:
        window_in_bytes = c.if_in_octets
        window_out_bytes = c.if_out_octets
        in_bps = int((window_in_bytes * 8) / POLL_INTERVAL_SECONDS)
        out_bps = int((window_out_bytes * 8) / POLL_INTERVAL_SECONDS)
        speed = c.if_speed if c.if_speed > 0 else 1_000_000_000
        rows.append(
            dict(
                tenant_id=tenant_for_agent[c.agent],
                ts_bucket=now,
                device=c.agent,
                interface=c.if_name,
                in_bps=in_bps,
                out_bps=out_bps,
                in_util_pct=min(100.0, (in_bps / speed) * 100),
                out_util_pct=min(100.0, (out_bps / speed) * 100),
                error_count=c.if_in_errors + c.if_out_errors,
            )
        )
    return rows


async def ingestion_loop(client: SFlowRTClient):
    """Main polling loop. Runs forever with POLL_INTERVAL_SECONDS cadence."""
    log.info(f"Starting ingestion loop (interval={POLL_INTERVAL_SECONDS}s)")
    tracer = otel.get_tracer("flowmind.ingest")
    router = get_router()
    while True:
        start = time.monotonic()
        span_cm = (
            tracer.start_as_current_span("ingest.cycle")
            if tracer is not None
            else _noop_cm()
        )
        try:
            with span_cm:
                ok = await client.health_check()
                otel.set_sflow_up(ok)

                flows = await client.get_top_flows(max_flows=500)
                counters = await client.get_interface_counters()

                flow_rows = await normalize_flows(flows, router)
                counter_rows = await normalize_counters(counters, router)

                async with AsyncSessionLocal() as session:
                    if flow_rows:
                        session.add_all([FlowSummaryMinute(**r) for r in flow_rows])
                    if counter_rows:
                        session.add_all(
                            [InterfaceUtilizationMinute(**r) for r in counter_rows]
                        )
                    await session.commit()

                if otel.flows_ingested is not None:
                    otel.flows_ingested.add(
                        len(flow_rows), {"result": "ok", "kind": "flow"}
                    )
                    otel.flows_ingested.add(
                        len(counter_rows), {"result": "ok", "kind": "counter"}
                    )
                if otel.ingestion_duration is not None:
                    otel.ingestion_duration.record(
                        time.monotonic() - start, {"phase": "cycle"}
                    )

                log.info(
                    f"Ingested {len(flow_rows)} flow rows, "
                    f"{len(counter_rows)} counter rows"
                )
        except Exception as e:
            log.error(f"Ingestion loop error: {e}", exc_info=True)
            if otel.flows_ingested is not None:
                otel.flows_ingested.add(1, {"result": "error"})

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


class _noop_cm:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
