"""Derive the path a flow takes through the fabric (PR 30).

A "path" here is the ordered list of (device, ingress_interface)
observations of the same (src_ip, dst_ip) tuple inside a recent window.
Every switch that sees the flow generates an sFlow sample tagged with
its hostname + the interface the packet *entered* on, so the path is
literally that sequence sorted by device-side topology.

We don't have a router-by-router timestamp on the sample (sFlow gives
us the bucket timestamp, not the per-hop traversal), so the ordering
must come from another signal. We use two:

1. If LLDP neighbors are populated (PR 31), we walk the adjacency graph
   from the closest device to the source. Today: degrades to step 2.
2. Otherwise: order by (device hostname, total bytes) and report the
   path as *unordered* — explicitly flagged in the response so the LLM
   doesn't fabricate an ordering.

Either way, the per-hop entry carries the link utilization and BGP next
hop where available — that's what an operator wants to see when asking
"why is A → B slow?"
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    BGPSessionMinute,
    FlowSummaryMinute,
    InterfaceUtilizationMinute,
)


async def find_path(
    db: AsyncSession,
    tenant_id: str,
    src_ip: str,
    dst_ip: str,
    window_minutes: int = 30,
) -> dict:
    """Trace the per-hop path of a src→dst flow over the window.

    Returns:
      - ``hops``: list of (device, interface, bytes, util) observations
      - ``ordered``: bool — True only if LLDP-driven ordering succeeded
      - ``severity``: low/medium/high based on per-hop congestion
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)

    # Aggregate the same flow across all (device, interface) observations.
    # Every switch on the path samples the same (src_ip, dst_ip), so the
    # GROUP BY is the path discovery step.
    q = (
        select(
            FlowSummaryMinute.device,
            FlowSummaryMinute.interface,
            func.sum(FlowSummaryMinute.bytes_estimated).label("bytes"),
            func.sum(FlowSummaryMinute.packets_estimated).label("packets"),
            func.min(FlowSummaryMinute.sampling_rate).label("min_sampling"),
        )
        .where(FlowSummaryMinute.tenant_id == tenant_id)
        .where(FlowSummaryMinute.ts_bucket >= since)
        .where(FlowSummaryMinute.src_ip == src_ip)
        .where(FlowSummaryMinute.dst_ip == dst_ip)
        .group_by(FlowSummaryMinute.device, FlowSummaryMinute.interface)
    )
    rows = (await db.execute(q)).all()

    if not rows:
        return {
            "src_ip": src_ip,
            "dst_ip": dst_ip,
            "window_minutes": window_minutes,
            "hops": [],
            "ordered": False,
            "hop_count": 0,
            "severity": "low",
            "confidence_note": (
                "No sFlow observations of this flow in the window. The "
                "flow either didn't traverse a sampling switch or was "
                "below the sampling rate threshold."
            ),
        }

    # Look up util per hop in a single batched query — we don't want N+1.
    util_keys = [(r.device, r.interface) for r in rows]
    util_by_key = await _hop_util(db, tenant_id, util_keys, since)
    bgp_by_device = await _bgp_for_devices(
        db, tenant_id, {r.device for r in rows}, since
    )

    # Sort by total bytes descending — heuristic stand-in for path order
    # until LLDP-driven walking exists (PR 31).
    rows_sorted = sorted(rows, key=lambda r: -int(r.bytes or 0))
    hops = []
    for r in rows_sorted:
        util = util_by_key.get((r.device, r.interface))
        hops.append(
            {
                "device": r.device,
                "ingress_interface": r.interface,
                "bytes_estimated": int(r.bytes or 0),
                "packets_estimated": int(r.packets or 0),
                "sampling_rate": int(r.min_sampling or 0),
                "peak_util_pct": util["peak"] if util else None,
                "avg_util_pct": util["avg"] if util else None,
                "bgp_peers_up": bgp_by_device.get(r.device, {}).get("up"),
                "bgp_peers_total": bgp_by_device.get(r.device, {}).get("total"),
            }
        )

    severity = _path_severity(hops)
    return {
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "window_minutes": window_minutes,
        "hops": hops,
        "ordered": False,
        "hop_count": len(hops),
        "severity": severity,
        "confidence_note": (
            "Path is the *set* of (device, ingress interface) samples that "
            "observed the flow — ordering is by traffic volume, not "
            "topological adjacency. Once LLDP neighbors are populated, "
            "ordering will follow the graph and ``ordered=true``. "
            "Utilization and BGP context per hop are exact within the "
            "sampling rate of the underlying counters."
        ),
    }


async def _hop_util(
    db: AsyncSession,
    tenant_id: str,
    keys: list[tuple[str, str]],
    since: datetime,
) -> dict[tuple[str, str], dict]:
    """Avg + peak util across the window for each (device, interface)."""
    if not keys:
        return {}
    devices = list({k[0] for k in keys})
    interfaces = list({k[1] for k in keys})
    q = (
        select(
            InterfaceUtilizationMinute.device,
            InterfaceUtilizationMinute.interface,
            func.avg(
                func.greatest(
                    InterfaceUtilizationMinute.in_util_pct,
                    InterfaceUtilizationMinute.out_util_pct,
                )
            ).label("avg"),
            func.max(
                func.greatest(
                    InterfaceUtilizationMinute.in_util_pct,
                    InterfaceUtilizationMinute.out_util_pct,
                )
            ).label("peak"),
        )
        .where(InterfaceUtilizationMinute.tenant_id == tenant_id)
        .where(InterfaceUtilizationMinute.ts_bucket >= since)
        .where(InterfaceUtilizationMinute.device.in_(devices))
        .where(InterfaceUtilizationMinute.interface.in_(interfaces))
        .group_by(
            InterfaceUtilizationMinute.device,
            InterfaceUtilizationMinute.interface,
        )
    )
    out: dict[tuple[str, str], dict] = {}
    for r in (await db.execute(q)).all():
        out[(r.device, r.interface)] = {
            "avg": round(float(r.avg or 0), 2),
            "peak": round(float(r.peak or 0), 2),
        }
    return out


async def _bgp_for_devices(
    db: AsyncSession,
    tenant_id: str,
    devices: set[str],
    since: datetime,
) -> dict[str, dict]:
    """Per-device 'how many BGP peers are up right now' for path context."""
    if not devices:
        return {}
    latest_ts_q = (
        select(
            BGPSessionMinute.device,
            BGPSessionMinute.peer_address,
            func.max(BGPSessionMinute.ts_bucket).label("ts"),
        )
        .where(BGPSessionMinute.tenant_id == tenant_id)
        .where(BGPSessionMinute.ts_bucket >= since)
        .where(BGPSessionMinute.device.in_(list(devices)))
        .group_by(BGPSessionMinute.device, BGPSessionMinute.peer_address)
    ).subquery()
    q = (
        select(
            BGPSessionMinute.device,
            BGPSessionMinute.session_state,
        )
        .join(
            latest_ts_q,
            (BGPSessionMinute.device == latest_ts_q.c.device)
            & (BGPSessionMinute.peer_address == latest_ts_q.c.peer_address)
            & (BGPSessionMinute.ts_bucket == latest_ts_q.c.ts),
        )
        .where(BGPSessionMinute.tenant_id == tenant_id)
    )
    out: dict[str, dict] = {}
    for r in (await db.execute(q)).all():
        entry = out.setdefault(r.device, {"up": 0, "total": 0})
        entry["total"] += 1
        if r.session_state == "ESTABLISHED":
            entry["up"] += 1
    return out


def _path_severity(hops: list[dict]) -> str:
    """Severity is the worst per-hop peak util along the path."""
    if not hops:
        return "low"
    worst = max((h.get("peak_util_pct") or 0) for h in hops)
    if worst >= 90:
        return "critical"
    if worst >= 80:
        return "high"
    if worst >= 60:
        return "medium"
    return "low"
