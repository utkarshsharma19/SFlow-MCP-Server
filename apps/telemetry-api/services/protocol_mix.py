"""Protocol-mix summary — what protocols are running in the window."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import FlowSummaryMinute
from services.flows import protocol_name


async def summarize_protocol_mix(
    db: AsyncSession,
    tenant_id: str,
    scope: str = "global",
    window_minutes: int = 15,
) -> dict:
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)

    q = (
        select(
            FlowSummaryMinute.protocol,
            func.sum(FlowSummaryMinute.bytes_estimated).label("total_bytes"),
            func.sum(FlowSummaryMinute.packets_estimated).label("total_packets"),
        )
        .where(FlowSummaryMinute.tenant_id == tenant_id)
        .where(FlowSummaryMinute.ts_bucket >= since)
        .group_by(FlowSummaryMinute.protocol)
    )
    if scope != "global" and scope.startswith("device:"):
        q = q.where(FlowSummaryMinute.device == scope.split(":", 1)[1])

    rows = (await db.execute(q)).all()
    if not rows:
        return {
            "scope": scope,
            "window_minutes": window_minutes,
            "protocols": [],
            "plain_summary": "No flow data in requested window.",
        }

    total = sum(r.total_bytes for r in rows) or 1
    protocols = sorted(
        [
            {
                "protocol": protocol_name(r.protocol),
                "bytes_estimated": int(r.total_bytes),
                "packets_estimated": int(r.total_packets),
                "pct_of_bytes": round(r.total_bytes / total * 100, 1),
            }
            for r in rows
        ],
        key=lambda p: p["bytes_estimated"],
        reverse=True,
    )

    top = protocols[0]
    plain = (
        f"{top['protocol']} dominates at {top['pct_of_bytes']:.1f}% of "
        f"traffic over the last {window_minutes} min across {len(protocols)} "
        f"protocols."
    )

    return {
        "scope": scope,
        "window_minutes": window_minutes,
        "protocols": protocols,
        "plain_summary": plain,
        "confidence_note": "Byte totals are sampling-rate-corrected estimates.",
    }
