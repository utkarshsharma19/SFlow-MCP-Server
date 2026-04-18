"""Query layer for gNMI-derived device state.

Surfaces the most recent interface/BGP/queue snapshot per (device, ...)
key within a window. Tenant-scoped per PR 20 — every query carries
WHERE tenant_id = :tid.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    BGPSessionMinute,
    DeviceStateMinute,
    QueueStatsMinute,
)

DEFAULT_LOOKBACK_MINUTES = 15


async def get_device_state(
    db: AsyncSession,
    tenant_id: str,
    device: str,
    window_minutes: int = DEFAULT_LOOKBACK_MINUTES,
) -> dict:
    """Latest interface state + BGP + queue summary for a device.

    Returns the most recent snapshot per (interface) and per (peer); any
    interface that flapped down inside the window is highlighted in
    `interfaces_down` for the model to react to.
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)

    iface_subq = (
        select(
            DeviceStateMinute.interface,
            func.max(DeviceStateMinute.ts_bucket).label("latest_ts"),
        )
        .where(DeviceStateMinute.tenant_id == tenant_id)
        .where(DeviceStateMinute.device == device)
        .where(DeviceStateMinute.ts_bucket >= since)
        .group_by(DeviceStateMinute.interface)
        .subquery()
    )
    iface_q = (
        select(DeviceStateMinute)
        .join(
            iface_subq,
            (DeviceStateMinute.interface == iface_subq.c.interface)
            & (DeviceStateMinute.ts_bucket == iface_subq.c.latest_ts),
        )
        .where(DeviceStateMinute.tenant_id == tenant_id)
        .where(DeviceStateMinute.device == device)
    )
    iface_rows = (await db.execute(iface_q)).scalars().all()

    bgp_subq = (
        select(
            BGPSessionMinute.peer_address,
            func.max(BGPSessionMinute.ts_bucket).label("latest_ts"),
        )
        .where(BGPSessionMinute.tenant_id == tenant_id)
        .where(BGPSessionMinute.device == device)
        .where(BGPSessionMinute.ts_bucket >= since)
        .group_by(BGPSessionMinute.peer_address)
        .subquery()
    )
    bgp_q = (
        select(BGPSessionMinute)
        .join(
            bgp_subq,
            (BGPSessionMinute.peer_address == bgp_subq.c.peer_address)
            & (BGPSessionMinute.ts_bucket == bgp_subq.c.latest_ts),
        )
        .where(BGPSessionMinute.tenant_id == tenant_id)
        .where(BGPSessionMinute.device == device)
    )
    bgp_rows = (await db.execute(bgp_q)).scalars().all()

    queue_q = (
        select(
            QueueStatsMinute.interface,
            QueueStatsMinute.queue_id,
            func.max(QueueStatsMinute.max_depth_bytes).label("peak_depth"),
            func.sum(QueueStatsMinute.pfc_pause_rx).label("pfc_rx"),
            func.sum(QueueStatsMinute.pfc_pause_tx).label("pfc_tx"),
            func.sum(QueueStatsMinute.ecn_marked_packets).label("ecn"),
            func.sum(QueueStatsMinute.dropped_packets).label("drops"),
        )
        .where(QueueStatsMinute.tenant_id == tenant_id)
        .where(QueueStatsMinute.device == device)
        .where(QueueStatsMinute.ts_bucket >= since)
        .group_by(QueueStatsMinute.interface, QueueStatsMinute.queue_id)
        .order_by(desc("peak_depth"))
        .limit(20)
    )
    queue_rows = (await db.execute(queue_q)).all()

    interfaces = [
        {
            "interface": r.interface,
            "admin_status": r.admin_status,
            "oper_status": r.oper_status,
            "last_change": r.last_change.isoformat() if r.last_change else None,
            "speed_bps": r.speed_bps,
            "mtu": r.mtu,
            "description": r.description,
        }
        for r in iface_rows
    ]
    interfaces_down = [
        i["interface"]
        for i in interfaces
        if i["admin_status"] == "UP" and i["oper_status"] != "UP"
    ]

    bgp_peers = [
        {
            "peer_address": r.peer_address,
            "peer_as": r.peer_as,
            "session_state": r.session_state,
            "uptime_seconds": r.uptime_seconds,
            "prefixes_received": r.prefixes_received,
            "prefixes_sent": r.prefixes_sent,
            "last_error": r.last_error,
        }
        for r in bgp_rows
    ]
    bgp_down = [p["peer_address"] for p in bgp_peers if p["session_state"] != "ESTABLISHED"]

    queues = [
        {
            "interface": r.interface,
            "queue_id": r.queue_id,
            "peak_depth_bytes": int(r.peak_depth or 0),
            "pfc_pause_rx": int(r.pfc_rx or 0),
            "pfc_pause_tx": int(r.pfc_tx or 0),
            "ecn_marked_packets": int(r.ecn or 0),
            "dropped_packets": int(r.drops or 0),
        }
        for r in queue_rows
    ]

    sources_present = bool(iface_rows or bgp_rows or queue_rows)

    return {
        "device": device,
        "window_minutes": window_minutes,
        "interfaces": interfaces,
        "interfaces_down": interfaces_down,
        "bgp_peers": bgp_peers,
        "bgp_down": bgp_down,
        "queues": queues,
        "confidence_note": (
            "Exact (non-sampled) gNMI/OpenConfig snapshots."
            if sources_present
            else f"No gNMI samples for {device} in last {window_minutes} min — "
            "either the target is unreachable or GNMI_TARGETS is unset."
        ),
    }


async def list_devices_with_gnmi(db: AsyncSession, tenant_id: str) -> list[str]:
    """Devices that have produced gNMI samples in the last 24 hours."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    q = (
        select(DeviceStateMinute.device)
        .where(DeviceStateMinute.tenant_id == tenant_id)
        .where(DeviceStateMinute.ts_bucket >= since)
        .group_by(DeviceStateMinute.device)
        .order_by(DeviceStateMinute.device)
    )
    return [row[0] for row in (await db.execute(q)).all()]
