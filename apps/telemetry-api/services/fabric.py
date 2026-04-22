"""ECMP fabric imbalance detection (PR 24).

For each ECMP group on a device — operator-configured or speed-inferred —
compute the per-member utilization across the window and flag groups
where the coefficient of variation exceeds CV_THRESHOLD or the
max/mean ratio exceeds MAX_MEAN_RATIO_THRESHOLD.

Imbalance hurts AI fabrics most: hash polarization on a leaf-to-spine
group can stall a training step even when total link capacity is far
from saturated.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    DeviceStateMinute,
    ECMPGroup,
    InterfaceUtilizationMinute,
)

CV_THRESHOLD = 0.30
MAX_MEAN_RATIO_THRESHOLD = 1.5
MIN_MEAN_UTIL_PCT = 5.0  # below this, imbalance is just noise on idle links


async def detect_fabric_imbalance(
    db: AsyncSession,
    tenant_id: str,
    device: str | None = None,
    window_minutes: int = 15,
) -> dict:
    """Compute imbalance metrics per ECMP group across all devices in tenant."""
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)

    devices = (
        [device]
        if device
        else await _list_devices_with_util(db, tenant_id, since)
    )

    groups_out: list[dict] = []
    imbalanced: list[dict] = []

    for dev in devices:
        ecmp_groups = await _resolve_ecmp_groups(db, tenant_id, dev)
        member_utils = await _per_member_avg_util(db, tenant_id, dev, since)
        if not member_utils:
            continue

        for group in ecmp_groups:
            members = [
                {
                    "interface": iface,
                    "avg_util_pct": member_utils[iface],
                }
                for iface in group["members"]
                if iface in member_utils
            ]
            if len(members) < 2:
                continue
            metrics = _imbalance_metrics([m["avg_util_pct"] for m in members])
            entry = {
                "device": dev,
                "group_name": group["group_name"],
                "source": group["source"],   # configured | inferred_by_speed
                "members": members,
                **metrics,
            }
            groups_out.append(entry)
            if metrics["is_imbalanced"]:
                imbalanced.append(entry)

    severity = _overall_severity(imbalanced)

    return {
        "device": device,
        "window_minutes": window_minutes,
        "groups": groups_out,
        "imbalanced_groups": imbalanced,
        "severity": severity,
        "confidence_note": (
            "Imbalance computed from sFlow-derived utilization (sampled) "
            "averaged across the window. Use ECMP groups configured via "
            "scripts/seed.py for accuracy; otherwise members are inferred "
            "from gNMI link speeds (PR 21)."
        ),
    }


async def _list_devices_with_util(
    db: AsyncSession, tenant_id: str, since: datetime
) -> list[str]:
    q = (
        select(InterfaceUtilizationMinute.device)
        .where(InterfaceUtilizationMinute.tenant_id == tenant_id)
        .where(InterfaceUtilizationMinute.ts_bucket >= since)
        .group_by(InterfaceUtilizationMinute.device)
    )
    return [row[0] for row in (await db.execute(q)).all()]


async def _per_member_avg_util(
    db: AsyncSession, tenant_id: str, device: str, since: datetime
) -> dict[str, float]:
    """Avg max(in,out) util_pct per interface across the window."""
    q = (
        select(
            InterfaceUtilizationMinute.interface,
            func.avg(
                func.greatest(
                    InterfaceUtilizationMinute.in_util_pct,
                    InterfaceUtilizationMinute.out_util_pct,
                )
            ).label("avg_util"),
        )
        .where(InterfaceUtilizationMinute.tenant_id == tenant_id)
        .where(InterfaceUtilizationMinute.device == device)
        .where(InterfaceUtilizationMinute.ts_bucket >= since)
        .group_by(InterfaceUtilizationMinute.interface)
    )
    return {r.interface: round(float(r.avg_util or 0), 2) for r in (await db.execute(q)).all()}


async def _resolve_ecmp_groups(
    db: AsyncSession, tenant_id: str, device: str
) -> list[dict]:
    """Operator-configured groups first, then speed-inferred fallback."""
    q = select(ECMPGroup).where(
        (ECMPGroup.tenant_id == tenant_id) & (ECMPGroup.device == device)
    )
    rows = (await db.execute(q)).scalars().all()
    if rows:
        return [
            {
                "group_name": r.group_name,
                "members": list(r.members or []),
                "source": "configured",
            }
            for r in rows
        ]
    return await _infer_groups_by_speed(db, tenant_id, device)


async def _infer_groups_by_speed(
    db: AsyncSession, tenant_id: str, device: str
) -> list[dict]:
    """Heuristic: every UP interface of the same link speed forms a group."""
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    q = (
        select(
            DeviceStateMinute.interface,
            DeviceStateMinute.speed_bps,
            func.max(DeviceStateMinute.ts_bucket).label("ts"),
        )
        .where(DeviceStateMinute.tenant_id == tenant_id)
        .where(DeviceStateMinute.device == device)
        .where(DeviceStateMinute.ts_bucket >= since)
        .where(DeviceStateMinute.oper_status == "UP")
        .group_by(DeviceStateMinute.interface, DeviceStateMinute.speed_bps)
    )
    rows = (await db.execute(q)).all()
    by_speed: dict[int, list[str]] = {}
    for r in rows:
        speed = int(r.speed_bps or 0)
        by_speed.setdefault(speed, []).append(r.interface)

    groups = []
    for speed, members in by_speed.items():
        if len(members) < 2:
            continue
        groups.append(
            {
                "group_name": f"auto-{_humanize_speed(speed)}",
                "members": sorted(members),
                "source": "inferred_by_speed",
            }
        )
    return groups


def _humanize_speed(bps: int) -> str:
    if bps >= 400_000_000_000:
        return "400G"
    if bps >= 100_000_000_000:
        return "100G"
    if bps >= 25_000_000_000:
        return "25G"
    if bps >= 10_000_000_000:
        return "10G"
    if bps >= 1_000_000_000:
        return "1G"
    return f"{bps}bps"


def _imbalance_metrics(utils: list[float]) -> dict:
    """Compute coefficient of variation + max/mean ratio."""
    n = len(utils)
    mean = sum(utils) / n if n else 0.0
    if n < 2 or mean < MIN_MEAN_UTIL_PCT:
        return {
            "mean_util_pct": round(mean, 2),
            "max_util_pct": round(max(utils) if utils else 0.0, 2),
            "cv": 0.0,
            "max_mean_ratio": 1.0,
            "is_imbalanced": False,
            "reason": (
                "below_min_util" if mean < MIN_MEAN_UTIL_PCT else "insufficient_members"
            ),
        }
    variance = sum((u - mean) ** 2 for u in utils) / n
    stddev = math.sqrt(variance)
    cv = stddev / mean if mean else 0.0
    max_mean_ratio = (max(utils) / mean) if mean else 1.0
    is_imbalanced = cv > CV_THRESHOLD or max_mean_ratio > MAX_MEAN_RATIO_THRESHOLD
    return {
        "mean_util_pct": round(mean, 2),
        "max_util_pct": round(max(utils), 2),
        "cv": round(cv, 3),
        "max_mean_ratio": round(max_mean_ratio, 2),
        "is_imbalanced": is_imbalanced,
        "reason": "imbalanced" if is_imbalanced else "balanced",
    }


def _overall_severity(imbalanced: list[dict]) -> str:
    if not imbalanced:
        return "low"
    worst_ratio = max(g["max_mean_ratio"] for g in imbalanced)
    if worst_ratio >= 3.0:
        return "high"
    if worst_ratio >= 2.0:
        return "medium"
    return "low"
