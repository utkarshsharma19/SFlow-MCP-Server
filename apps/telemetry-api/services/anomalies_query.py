"""Read-side queries for anomaly events."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AnomalyEvent

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


async def get_recent_anomalies(
    db: AsyncSession,
    tenant_id: str,
    scope: str = "global",
    severity_min: str = "medium",
    since_minutes: int = 30,
) -> dict:
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    min_rank = SEVERITY_RANK.get(severity_min, 2)
    allowed = [s for s, r in SEVERITY_RANK.items() if r >= min_rank]

    q = (
        select(AnomalyEvent)
        .where(AnomalyEvent.tenant_id == tenant_id)
        .where(AnomalyEvent.ts >= since)
        .where(AnomalyEvent.severity.in_(allowed))
        .order_by(AnomalyEvent.ts.desc())
        .limit(50)
    )
    if scope != "global":
        q = q.where(AnomalyEvent.scope.contains(scope))

    events = (await db.execute(q)).scalars().all()

    most_severe = max(
        events, key=lambda e: SEVERITY_RANK[e.severity], default=None
    )

    return {
        "anomalies": [
            {
                "id": str(e.id),
                "ts": e.ts.isoformat(),
                "type": e.anomaly_type,
                "severity": e.severity,
                "summary": e.summary,
                "scope": e.scope,
            }
            for e in events
        ],
        "total_count": len(events),
        "most_severe": most_severe.severity if most_severe else None,
        "plain_summary": _build_plain_summary(events),
        "window_minutes": since_minutes,
        "scope": scope,
    }


def _build_plain_summary(events) -> str:
    if not events:
        return "No anomalies in the requested window."
    critical = sum(1 for e in events if e.severity == "critical")
    high = sum(1 for e in events if e.severity == "high")
    parts = []
    if critical:
        parts.append(f"{critical} critical")
    if high:
        parts.append(f"{high} high-severity")
    if not parts:
        parts.append("all medium-severity")
    noun = "event" if len(events) == 1 else "events"
    return (
        f"{len(events)} anomaly {noun} detected "
        f"({', '.join(parts)} requiring attention)."
    )
