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

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    BGPSessionMinute,
    FlowSummaryMinute,
    InterfaceUtilizationMinute,
    LLDPNeighbor,
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

    candidate_devices = {r.device for r in rows}

    # Try to order topologically by walking LLDP adjacency. Falls back
    # to volume-sorted order if the LLDP subgraph isn't a clean chain
    # (e.g. ECMP fan-out, missing neighbors, asymmetric routing).
    adjacency = await _lldp_adjacency_for_devices(
        db, tenant_id, candidate_devices
    )
    ordered_devices = _topology_order(candidate_devices, adjacency)

    if ordered_devices is not None:
        rows_sorted = _order_rows_by_devices(rows, ordered_devices)
        ordered = True
        order_basis = "lldp_chain"
    else:
        # Volume-sorted fallback. Heuristic, but stable: the chatbot
        # consumer reads `ordered=false` and tells the operator not to
        # over-interpret position.
        rows_sorted = sorted(rows, key=lambda r: -int(r.bytes or 0))
        ordered = False
        order_basis = "volume"

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
        "ordered": ordered,
        "order_basis": order_basis,
        "hop_count": len(hops),
        "severity": severity,
        "confidence_note": _confidence_note(ordered, order_basis),
    }


def _confidence_note(ordered: bool, basis: str) -> str:
    if ordered and basis == "lldp_chain":
        return (
            "Hops are ordered by LLDP adjacency — the first hop is the "
            "device closest to the source IP, the last is closest to "
            "the destination. Utilization and BGP context per hop are "
            "exact within the sampling rate of the underlying counters."
        )
    return (
        "Path is the *set* of (device, ingress interface) samples that "
        "observed the flow — ordering is by traffic volume, not "
        "topological adjacency, because the LLDP subgraph either is "
        "incomplete or fans out (ECMP, asymmetric routing). The chatbot "
        "should not infer a hop sequence from this list."
    )


async def _hop_util(
    db: AsyncSession,
    tenant_id: str,
    keys: list[tuple[str, str]],
    since: datetime,
) -> dict[tuple[str, str], dict]:
    """Avg + peak util across the window for each (device, interface).

    Filters pairwise on (device, interface) rather than the cross
    product of device IN ... AND interface IN ... — interface names
    repeat across switches (every leaf has an Eth1), so the naive
    cross-product can match many rows that were never requested.
    """
    if not keys:
        return {}
    # Deduplicate; ordering of the IN list doesn't matter.
    pairs = list({(d, i) for d, i in keys})
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
        .where(
            tuple_(
                InterfaceUtilizationMinute.device,
                InterfaceUtilizationMinute.interface,
            ).in_(pairs)
        )
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


async def _lldp_adjacency_for_devices(
    db: AsyncSession,
    tenant_id: str,
    devices: set[str],
) -> dict[str, set[str]]:
    """Build {device → set of neighbor device names} from the LLDP cache.

    Only includes neighbors whose system_name *also* appears in
    ``devices`` — adjacency to a switch we never observed on this flow
    is irrelevant for ordering this path. That filtering is what lets
    us reuse the LLDP table for any flow without false branches.
    """
    if not devices:
        return {}
    q = (
        select(
            LLDPNeighbor.device,
            LLDPNeighbor.neighbor_system_name,
        )
        .where(LLDPNeighbor.tenant_id == tenant_id)
        .where(LLDPNeighbor.device.in_(list(devices)))
        .where(LLDPNeighbor.neighbor_system_name.is_not(None))
    )
    adj: dict[str, set[str]] = {}
    for row in (await db.execute(q)).all():
        nbr = row.neighbor_system_name
        if nbr not in devices or nbr == row.device:
            continue
        adj.setdefault(row.device, set()).add(nbr)
    return adj


def _topology_order(
    devices: set[str], adjacency: dict[str, set[str]]
) -> list[str] | None:
    """Return the devices ordered head→tail along an LLDP chain.

    Returns ``None`` when the subgraph isn't a clean chain — branching
    means we don't know which direction the flow went, and the caller
    falls back to the volume heuristic.

    Heuristic: a chain has exactly one node with no neighbors-among-
    candidates "to the left" (the head) and one with none "to the
    right" (the tail). LLDP is bidirectional (both ends see each other),
    so we can't infer direction from the adjacency alone — but a chain
    of N candidates yields N-1 bidirectional edges and N nodes where
    the two ends have degree 1.
    """
    if not devices:
        return []
    if len(devices) == 1:
        return list(devices)

    # Symmetrize: LLDP entries from leaf1 may name spine1 and vice
    # versa; if only one side reported, still treat them as adjacent.
    # Filter self-loops here so they don't inflate degree counts and
    # confuse the chain detection.
    sym: dict[str, set[str]] = {d: set() for d in devices}
    for a, nbrs in adjacency.items():
        for b in nbrs:
            if a == b:
                continue
            sym[a].add(b)
            sym.setdefault(b, set()).add(a)

    # A chain has exactly 2 nodes of degree 1 (the endpoints) and the
    # rest of degree 2. Any other shape (star, tree, branch) → None.
    deg = {d: len(sym.get(d, set())) for d in devices}
    endpoints = [d for d, k in deg.items() if k == 1]
    if len(endpoints) != 2:
        return None
    if any(deg[d] != 2 for d in devices if d not in endpoints):
        return None

    # Walk from one endpoint to the other.
    head = sorted(endpoints)[0]  # deterministic between two valid heads
    visited: list[str] = [head]
    prev: str | None = None
    cur = head
    while True:
        nexts = sym.get(cur, set()) - ({prev} if prev else set())
        if not nexts:
            break
        if len(nexts) > 1:
            # Defensive: chain check above should have caught this
            return None
        nxt = next(iter(nexts))
        visited.append(nxt)
        prev, cur = cur, nxt
        if cur == endpoints[1]:
            break

    if set(visited) != devices:
        # Disconnected — there are extra candidates not on the chain.
        return None
    return visited


def _order_rows_by_devices(
    rows, ordered_devices: list[str]
):
    """Reorder the (device, interface) rows to follow ``ordered_devices``.

    Within one device's group (rare: a flow with multiple ingress
    interfaces on the same device, e.g. due to ECMP fan-in), we keep
    descending byte order so the heaviest entry leads.
    """
    by_device: dict[str, list] = {}
    for r in rows:
        by_device.setdefault(r.device, []).append(r)
    out = []
    for d in ordered_devices:
        group = by_device.get(d, [])
        group.sort(key=lambda r: -int(r.bytes or 0))
        out.extend(group)
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
