"""Topology inventory — devices and interfaces seen in recent telemetry.

v1 derives inventory from observed flows and counter samples. A future
version will read from a device-management source of truth (NetBox, etc.).
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import FlowSummaryMinute, InterfaceUtilizationMinute

INVENTORY_LOOKBACK_HOURS = 24


async def list_devices(db: AsyncSession) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=INVENTORY_LOOKBACK_HOURS)
    q = (
        select(
            InterfaceUtilizationMinute.device,
            func.max(InterfaceUtilizationMinute.ts_bucket).label("last_seen"),
            func.count(
                func.distinct(InterfaceUtilizationMinute.interface)
            ).label("interface_count"),
        )
        .where(InterfaceUtilizationMinute.ts_bucket >= since)
        .group_by(InterfaceUtilizationMinute.device)
        .order_by(InterfaceUtilizationMinute.device)
    )
    rows = (await db.execute(q)).all()
    return {
        "devices": [
            {
                "hostname": r.device,
                "interface_count": int(r.interface_count),
                "last_seen": r.last_seen.isoformat(),
            }
            for r in rows
        ],
        "total": len(rows),
        "lookback_hours": INVENTORY_LOOKBACK_HOURS,
    }


async def list_interfaces(db: AsyncSession) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=INVENTORY_LOOKBACK_HOURS)
    q = (
        select(
            InterfaceUtilizationMinute.device,
            InterfaceUtilizationMinute.interface,
            func.max(InterfaceUtilizationMinute.ts_bucket).label("last_seen"),
            func.max(InterfaceUtilizationMinute.in_util_pct).label("peak_in"),
            func.max(InterfaceUtilizationMinute.out_util_pct).label("peak_out"),
        )
        .where(InterfaceUtilizationMinute.ts_bucket >= since)
        .group_by(
            InterfaceUtilizationMinute.device,
            InterfaceUtilizationMinute.interface,
        )
        .order_by(
            InterfaceUtilizationMinute.device,
            InterfaceUtilizationMinute.interface,
        )
    )
    rows = (await db.execute(q)).all()

    # Also surface interfaces seen only in flow data
    flow_q = (
        select(
            FlowSummaryMinute.device,
            FlowSummaryMinute.interface,
        )
        .where(FlowSummaryMinute.ts_bucket >= since)
        .group_by(FlowSummaryMinute.device, FlowSummaryMinute.interface)
    )
    flow_pairs = {(r.device, r.interface) for r in (await db.execute(flow_q)).all()}
    util_pairs = {(r.device, r.interface) for r in rows}
    flow_only = flow_pairs - util_pairs

    return {
        "interfaces": [
            {
                "device": r.device,
                "interface": r.interface,
                "peak_in_util_pct": round(float(r.peak_in), 2),
                "peak_out_util_pct": round(float(r.peak_out), 2),
                "last_seen": r.last_seen.isoformat(),
                "source": "counters",
            }
            for r in rows
        ]
        + [
            {
                "device": device,
                "interface": interface,
                "peak_in_util_pct": None,
                "peak_out_util_pct": None,
                "last_seen": None,
                "source": "flows_only",
            }
            for device, interface in sorted(flow_only)
        ],
        "total": len(rows) + len(flow_only),
        "lookback_hours": INVENTORY_LOOKBACK_HOURS,
    }
