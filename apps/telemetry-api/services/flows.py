"""Top-talker query logic.

All query logic lives in the service layer. Routers stay thin. Every
query is tenant-scoped from PR 20 onward — no caller can read flows
belonging to another tenant.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import FlowSummaryMinute

PROTOCOL_NAMES = {6: "TCP", 17: "UDP", 1: "ICMP", 112: "VRRP", 89: "OSPF"}


def protocol_name(proto: int) -> str:
    return PROTOCOL_NAMES.get(proto, f"PROTO_{proto}")


async def get_top_talkers(
    db: AsyncSession,
    tenant_id: str,
    window_minutes: int,
    scope: str = "global",
    limit: int = 10,
) -> dict:
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)

    q = (
        select(
            FlowSummaryMinute.src_ip,
            FlowSummaryMinute.dst_ip,
            FlowSummaryMinute.protocol,
            func.sum(FlowSummaryMinute.bytes_estimated).label("total_bytes"),
            func.avg(FlowSummaryMinute.sampling_rate).label("avg_sampling_rate"),
        )
        .where(FlowSummaryMinute.tenant_id == tenant_id)
        .where(FlowSummaryMinute.ts_bucket >= since)
        .group_by(
            FlowSummaryMinute.src_ip,
            FlowSummaryMinute.dst_ip,
            FlowSummaryMinute.protocol,
        )
        .order_by(desc("total_bytes"))
        .limit(limit)
    )

    if scope != "global" and scope.startswith("device:"):
        device = scope.split(":", 1)[1]
        q = q.where(FlowSummaryMinute.device == device)

    result = await db.execute(q)
    rows = result.all()

    if not rows:
        return _empty_top_talkers_response(window_minutes, scope)

    total_bytes = sum(r.total_bytes for r in rows)
    avg_sr = int(sum(r.avg_sampling_rate for r in rows) / len(rows))

    proto_bytes: dict = {}
    for r in rows:
        name = protocol_name(r.protocol)
        proto_bytes[name] = proto_bytes.get(name, 0) + r.total_bytes
    proto_total = sum(proto_bytes.values()) or 1
    proto_breakdown = {k: round(v / proto_total, 3) for k, v in proto_bytes.items()}

    return {
        "top_pairs": [
            {
                "src_ip": r.src_ip,
                "dst_ip": r.dst_ip,
                "protocol": protocol_name(r.protocol),
                "bytes_estimated": r.total_bytes,
                "pct_of_total": round(r.total_bytes / total_bytes * 100, 1)
                if total_bytes
                else 0,
            }
            for r in rows
        ],
        "protocol_breakdown": proto_breakdown,
        "confidence_note": (
            f"Avg sampling rate 1:{avg_sr}. "
            f"Volume estimates carry ~±{min(50, avg_sr // 20)}% uncertainty."
        ),
        "window_minutes": window_minutes,
        "scope": scope,
    }


def _empty_top_talkers_response(window_minutes, scope):
    return {
        "top_pairs": [],
        "protocol_breakdown": {},
        "confidence_note": "No flow data in requested window.",
        "window_minutes": window_minutes,
        "scope": scope,
    }
