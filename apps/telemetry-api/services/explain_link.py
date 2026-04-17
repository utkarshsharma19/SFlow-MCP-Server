"""Explain-hot-link — who/what is driving utilization on a single interface."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import FlowSummaryMinute, InterfaceUtilizationMinute
from services.flows import protocol_name


async def explain_hot_link(
    db: AsyncSession,
    tenant_id: str,
    device: str,
    interface: str,
    window_minutes: int = 15,
) -> dict:
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)

    util_q = (
        select(InterfaceUtilizationMinute)
        .where(InterfaceUtilizationMinute.tenant_id == tenant_id)
        .where(InterfaceUtilizationMinute.device == device)
        .where(InterfaceUtilizationMinute.interface == interface)
        .where(InterfaceUtilizationMinute.ts_bucket >= since)
        .order_by(InterfaceUtilizationMinute.ts_bucket)
    )
    util_rows = (await db.execute(util_q)).scalars().all()

    peak_ts = None
    peak_util = 0.0
    if util_rows:
        peak_row = max(
            util_rows,
            key=lambda r: max(r.in_util_pct, r.out_util_pct),
        )
        peak_util = max(peak_row.in_util_pct, peak_row.out_util_pct)
        peak_ts = peak_row.ts_bucket

    flow_q = (
        select(
            FlowSummaryMinute.src_ip,
            FlowSummaryMinute.dst_ip,
            FlowSummaryMinute.protocol,
            func.sum(FlowSummaryMinute.bytes_estimated).label("total_bytes"),
        )
        .where(FlowSummaryMinute.tenant_id == tenant_id)
        .where(FlowSummaryMinute.device == device)
        .where(FlowSummaryMinute.interface == interface)
        .where(FlowSummaryMinute.ts_bucket >= since)
        .group_by(
            FlowSummaryMinute.src_ip,
            FlowSummaryMinute.dst_ip,
            FlowSummaryMinute.protocol,
        )
        .order_by(desc("total_bytes"))
        .limit(5)
    )
    flow_rows = (await db.execute(flow_q)).all()

    total_bytes = sum(r.total_bytes for r in flow_rows)
    contributors = [
        {
            "src_ip": r.src_ip,
            "dst_ip": r.dst_ip,
            "protocol": protocol_name(r.protocol),
            "bytes_estimated": int(r.total_bytes),
            "pct_of_link": (
                round(r.total_bytes / total_bytes * 100, 1)
                if total_bytes else 0.0
            ),
        }
        for r in flow_rows
    ]

    narrative = _narrative(device, interface, peak_util, contributors)

    return {
        "device": device,
        "interface": interface,
        "window_minutes": window_minutes,
        "peak_util_pct": round(peak_util, 1),
        "peak_ts": peak_ts.isoformat() if peak_ts else None,
        "top_contributors": contributors,
        "narrative": narrative,
        "confidence_note": (
            f"Derived from {len(util_rows)} util samples "
            f"and {len(flow_rows)} flow groupings."
        ),
    }


def _narrative(device, interface, peak_util, contributors) -> str:
    if not contributors:
        return (
            f"No flow activity visible on {device}:{interface} in the "
            f"requested window. This usually means no sampled traffic "
            f"reached the collector."
        )
    top = contributors[0]
    return (
        f"{interface} on {device} peaked at {peak_util:.1f}% utilization. "
        f"The dominant conversation is "
        f"{top['src_ip']} -> {top['dst_ip']} ({top['protocol']}), "
        f"accounting for {top['pct_of_link']:.1f}% of flow bytes."
    )
