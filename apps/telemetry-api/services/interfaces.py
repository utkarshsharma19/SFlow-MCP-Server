"""Interface utilization query logic (tenant-scoped from PR 20)."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import InterfaceUtilizationMinute

UTIL_BREACH_THRESHOLD_PCT = 80.0


async def get_interface_utilization(
    db: AsyncSession,
    tenant_id: str,
    device: str,
    interface: str,
    window_minutes: int,
) -> dict:
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)

    q = (
        select(InterfaceUtilizationMinute)
        .where(InterfaceUtilizationMinute.tenant_id == tenant_id)
        .where(InterfaceUtilizationMinute.device == device)
        .where(InterfaceUtilizationMinute.interface == interface)
        .where(InterfaceUtilizationMinute.ts_bucket >= since)
        .order_by(InterfaceUtilizationMinute.ts_bucket)
    )
    result = await db.execute(q)
    rows = result.scalars().all()

    if not rows:
        return {
            "avg_in_util_pct": 0.0,
            "max_in_util_pct": 0.0,
            "avg_out_util_pct": 0.0,
            "max_out_util_pct": 0.0,
            "trend": "stable",
            "threshold_breaches": [],
            "confidence_note": (
                f"No utilization samples for {device}:{interface} "
                f"in last {window_minutes} min."
            ),
        }

    avg_in = sum(r.in_util_pct for r in rows) / len(rows)
    avg_out = sum(r.out_util_pct for r in rows) / len(rows)
    max_in = max(r.in_util_pct for r in rows)
    max_out = max(r.out_util_pct for r in rows)

    trend = _compute_trend(rows)

    breaches = [
        {
            "ts": r.ts_bucket.isoformat(),
            "direction": "inbound" if r.in_util_pct > r.out_util_pct else "outbound",
            "util_pct": round(max(r.in_util_pct, r.out_util_pct), 1),
        }
        for r in rows
        if max(r.in_util_pct, r.out_util_pct) >= UTIL_BREACH_THRESHOLD_PCT
    ]

    return {
        "avg_in_util_pct": round(avg_in, 2),
        "max_in_util_pct": round(max_in, 2),
        "avg_out_util_pct": round(avg_out, 2),
        "max_out_util_pct": round(max_out, 2),
        "trend": trend,
        "threshold_breaches": breaches,
        "confidence_note": (
            f"Derived from {len(rows)} minute-bucketed samples of "
            f"{device}:{interface}."
        ),
    }


def _compute_trend(rows) -> str:
    if len(rows) < 2:
        return "stable"
    half = len(rows) // 2
    first_half = rows[:half] or rows[:1]
    second_half = rows[half:]
    first_avg = sum(max(r.in_util_pct, r.out_util_pct) for r in first_half) / len(first_half)
    second_avg = sum(max(r.in_util_pct, r.out_util_pct) for r in second_half) / len(second_half)
    delta = second_avg - first_avg
    if delta > 5:
        return "increasing"
    if delta < -5:
        return "decreasing"
    return "stable"
