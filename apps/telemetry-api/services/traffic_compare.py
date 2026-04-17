"""Compare traffic volume between two time windows."""
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import FlowSummaryMinute


async def compare_windows(
    db: AsyncSession,
    tenant_id: str,
    scope: str,
    baseline_start: datetime,
    baseline_end: datetime,
    current_start: datetime,
    current_end: datetime,
) -> dict:
    async def window_bytes(start: datetime, end: datetime) -> int:
        q = (
            select(func.coalesce(func.sum(FlowSummaryMinute.bytes_estimated), 0))
            .where(FlowSummaryMinute.tenant_id == tenant_id)
            .where(FlowSummaryMinute.ts_bucket >= start)
            .where(FlowSummaryMinute.ts_bucket < end)
        )
        if scope != "global" and scope.startswith("device:"):
            device = scope.split(":", 1)[1]
            q = q.where(FlowSummaryMinute.device == device)
        return int((await db.execute(q)).scalar() or 0)

    baseline_bytes = await window_bytes(baseline_start, baseline_end)
    current_bytes = await window_bytes(current_start, current_end)

    delta_pct = 0.0
    if baseline_bytes > 0:
        delta_pct = round(
            ((current_bytes - baseline_bytes) / baseline_bytes) * 100, 1
        )

    direction = (
        "increase" if delta_pct > 5
        else "decrease" if delta_pct < -5
        else "stable"
    )

    return {
        "scope": scope,
        "baseline_window": {
            "start": baseline_start.isoformat(),
            "end": baseline_end.isoformat(),
            "bytes_estimated": baseline_bytes,
        },
        "current_window": {
            "start": current_start.isoformat(),
            "end": current_end.isoformat(),
            "bytes_estimated": current_bytes,
        },
        "delta_bytes_pct": delta_pct,
        "direction": direction,
        "confidence_note": (
            "Volumes are sampling-rate-corrected estimates. "
            "Treat deltas under 10% as noise."
        ),
    }
