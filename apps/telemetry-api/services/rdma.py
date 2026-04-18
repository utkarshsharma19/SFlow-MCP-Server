"""RoCEv2 / RDMA fabric health analysis (PR 23).

Combines two independent signals to assess GPU-cluster fabric stress:

1. **Flow-side**: RoCEv2 traffic rides UDP/4791. We sum bytes on that
   port within a window to estimate the RoCE share of traffic. The
   sFlow path is sampling-corrected so this is an estimate.

2. **Queue-side** (gNMI / OpenConfig): PFC pause frames signal
   receiver-side congestion (lossless backpressure), ECN marks signal
   DCQCN congestion notification, and any drop counts on a lossless
   queue indicate the fabric has lost data — the failure mode RDMA is
   designed to avoid. These come from queue_stats_minute (PR 21) which
   carries exact (non-sampled) telemetry.

Severity is assigned conservatively:
  * critical — drops on a queue that also shows PFC pauses (lossless drop)
  * high     — sustained PFC pauses or ECN marking on RoCE-carrying iface
  * medium   — PFC or ECN observed but no drops; fabric is signaling
  * low      — RoCE present, no congestion signals
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    FlowSummaryMinute,
    QueueStatsMinute,
)

# RoCEv2 transport rides UDP/4791 (IANA). UDP is protocol 17.
ROCE_UDP_PORT = 4791
UDP_PROTO = 17


async def get_rdma_health(
    db: AsyncSession,
    tenant_id: str,
    device: str | None = None,
    window_minutes: int = 15,
) -> dict:
    """Combined flow + queue assessment of RoCE/RDMA fabric health.

    If `device` is provided the analysis is scoped to that device's
    queues; the flow side is global (we don't carry the egress device
    on FlowSummaryMinute, only ingress).
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)

    # ---- Flow side: RoCE share of total bytes -------------------------
    # NOTE: sFlow doesn't reliably surface UDP src/dst port at the
    # RESTflow-API surface we ingest, so we estimate via the protocol
    # column. RoCEv2 always uses UDP, so a high UDP share on links that
    # also show PFC/ECN is the heuristic the LLM should reason on.
    total_q = (
        select(func.sum(FlowSummaryMinute.bytes_estimated))
        .where(FlowSummaryMinute.tenant_id == tenant_id)
        .where(FlowSummaryMinute.ts_bucket >= since)
    )
    udp_q = (
        select(func.sum(FlowSummaryMinute.bytes_estimated))
        .where(FlowSummaryMinute.tenant_id == tenant_id)
        .where(FlowSummaryMinute.ts_bucket >= since)
        .where(FlowSummaryMinute.protocol == UDP_PROTO)
    )
    total_bytes = (await db.execute(total_q)).scalar() or 0
    udp_bytes = (await db.execute(udp_q)).scalar() or 0
    roce_share = (udp_bytes / total_bytes) if total_bytes else 0.0

    top_talkers_q = (
        select(
            FlowSummaryMinute.src_ip,
            FlowSummaryMinute.dst_ip,
            func.sum(FlowSummaryMinute.bytes_estimated).label("bytes"),
        )
        .where(FlowSummaryMinute.tenant_id == tenant_id)
        .where(FlowSummaryMinute.ts_bucket >= since)
        .where(FlowSummaryMinute.protocol == UDP_PROTO)
        .group_by(FlowSummaryMinute.src_ip, FlowSummaryMinute.dst_ip)
        .order_by(desc("bytes"))
        .limit(10)
    )
    top_talkers = [
        {
            "src_ip": r.src_ip,
            "dst_ip": r.dst_ip,
            "bytes_estimated": int(r.bytes),
        }
        for r in (await db.execute(top_talkers_q)).all()
    ]

    # ---- Queue side: PFC + ECN + drops --------------------------------
    queue_q = (
        select(
            QueueStatsMinute.device,
            QueueStatsMinute.interface,
            QueueStatsMinute.queue_id,
            func.sum(QueueStatsMinute.pfc_pause_rx).label("pfc_rx"),
            func.sum(QueueStatsMinute.pfc_pause_tx).label("pfc_tx"),
            func.sum(QueueStatsMinute.ecn_marked_packets).label("ecn"),
            func.sum(QueueStatsMinute.dropped_packets).label("drops"),
            func.max(QueueStatsMinute.max_depth_bytes).label("peak_depth"),
        )
        .where(QueueStatsMinute.tenant_id == tenant_id)
        .where(QueueStatsMinute.ts_bucket >= since)
        .group_by(
            QueueStatsMinute.device,
            QueueStatsMinute.interface,
            QueueStatsMinute.queue_id,
        )
    )
    if device is not None:
        queue_q = queue_q.where(QueueStatsMinute.device == device)
    queue_rows = (await db.execute(queue_q)).all()

    queues_with_signals = []
    for r in queue_rows:
        if not (r.pfc_rx or r.pfc_tx or r.ecn or r.drops):
            continue
        queues_with_signals.append(
            {
                "device": r.device,
                "interface": r.interface,
                "queue_id": r.queue_id,
                "pfc_pause_rx": int(r.pfc_rx or 0),
                "pfc_pause_tx": int(r.pfc_tx or 0),
                "ecn_marked_packets": int(r.ecn or 0),
                "dropped_packets": int(r.drops or 0),
                "peak_depth_bytes": int(r.peak_depth or 0),
            }
        )

    severity, drivers = _assess(roce_share, queues_with_signals)

    confidence_note = (
        "Queue counters are exact (gNMI/OpenConfig). RoCE share is "
        "estimated from UDP bytes via sFlow sampling — high UDP share "
        "alone is suggestive, not definitive, of RoCEv2."
    )
    if not queue_rows:
        confidence_note += (
            " No gNMI queue samples in window — install gNMI targets "
            "to enable the queue-side signals."
        )

    return {
        "device": device,
        "window_minutes": window_minutes,
        "roce_share_estimated": round(roce_share, 3),
        "udp_bytes_estimated": int(udp_bytes),
        "total_bytes_estimated": int(total_bytes),
        "top_udp_talkers": top_talkers,
        "queues_with_congestion_signals": queues_with_signals,
        "severity": severity,
        "drivers": drivers,
        "confidence_note": confidence_note,
    }


def _assess(roce_share: float, queues: list[dict]) -> tuple[str, list[str]]:
    """Map (roce_share, queue_signals) to a severity + plain-language drivers."""
    drivers: list[str] = []

    drops_with_pfc = [
        q for q in queues if q["dropped_packets"] > 0 and (q["pfc_pause_rx"] or q["pfc_pause_tx"])
    ]
    sustained_pfc = [
        q for q in queues if (q["pfc_pause_rx"] + q["pfc_pause_tx"]) > 1000
    ]
    ecn_signaling = [q for q in queues if q["ecn_marked_packets"] > 0]
    any_drops = [q for q in queues if q["dropped_packets"] > 0]

    if drops_with_pfc:
        drivers.append(
            f"Lossless drop detected on {len(drops_with_pfc)} queue(s): "
            "PFC pauses present AND drops > 0 on the same queue. RDMA "
            "transport will retransmit and stall."
        )
        return "critical", drivers

    if any_drops:
        drivers.append(
            f"Drops observed on {len(any_drops)} queue(s) without PFC — "
            "lossy classes are dropping but the lossless plane appears "
            "intact. Verify QoS class mapping for RoCE traffic."
        )

    if sustained_pfc:
        drivers.append(
            f"Sustained PFC pause activity on {len(sustained_pfc)} queue(s) "
            "(>1000 frames in window). Receiver-side congestion is "
            "applying backpressure — investigate ECMP imbalance or "
            "incast at the sink."
        )
        return "high", drivers

    if ecn_signaling:
        drivers.append(
            f"ECN marking active on {len(ecn_signaling)} queue(s). DCQCN "
            "is throttling senders — fabric is healthy but congested."
        )
        return "medium", drivers

    if queues:
        drivers.append(
            "PFC/ECN counters present but below thresholds; fabric is "
            "signaling normally."
        )
        return "low", drivers

    if roce_share > 0.5:
        drivers.append(
            f"High UDP share ({roce_share:.0%}) consistent with RoCE traffic, "
            "but no gNMI queue samples to confirm fabric health."
        )
        return "low", drivers

    drivers.append("No RoCE/RDMA stress signals in window.")
    return "low", drivers
