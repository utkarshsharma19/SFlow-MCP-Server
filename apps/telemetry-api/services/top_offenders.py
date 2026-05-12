"""Rank the noisiest devices/interfaces over a window (PR 30).

Operator's morning standup question: "what should I look at first?"
The answer is the device or interface with the most events recently.
We compute a composite score per (device[, interface]) made of:

* ``anomaly_score``   — sum of open anomalies, weighted by severity
* ``flap_score``      — count of distinct ``last_change`` transitions
* ``error_score``     — sum of interface error_counts in the window
* ``hot_score``       — number of buckets with peak util ≥ 80%

Each component is normalized to its own max so one chatty signal
(thousands of errors) doesn't drown the others. The chatbot quotes the
composite plus the per-signal drivers — exactly what an operator would
write into a triage ticket.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    AnomalyEvent,
    DeviceStateMinute,
    InterfaceUtilizationMinute,
)

SEVERITY_WEIGHT = {"low": 1, "medium": 3, "high": 7, "critical": 15}
HOT_UTIL_PCT = 80.0
DEFAULT_LIMIT = 10


async def get_top_offenders(
    db: AsyncSession,
    tenant_id: str,
    scope: str = "device",
    window_minutes: int = 60,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """Return the top-N offenders ranked by composite score.

    Args:
      scope: "device" (default) groups by device hostname;
        "interface" groups by (device, interface).
    """
    if scope not in {"device", "interface"}:
        return {
            "error": "scope must be 'device' or 'interface'",
            "scope_received": scope,
        }
    limit = max(1, min(50, int(limit)))
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)

    by_key: dict[tuple, dict] = {}

    await _add_anomaly_scores(db, tenant_id, since, scope, by_key)
    await _add_flap_scores(db, tenant_id, since, scope, by_key)
    await _add_util_scores(db, tenant_id, since, scope, by_key)

    rows = list(by_key.values())
    _normalize_and_combine(rows)
    rows.sort(key=lambda r: -r["composite_score"])
    top = rows[:limit]

    return {
        "scope": scope,
        "window_minutes": window_minutes,
        "limit": limit,
        "offenders": top,
        "total_candidates": len(rows),
        "confidence_note": (
            "Composite score blends open anomalies (severity-weighted), "
            "flap count (gNMI ``last_change`` deltas inside the window), "
            "summed interface errors, and the number of minute buckets "
            "above 80% peak utilization. Each component is normalized to "
            "its own max so one loud signal doesn't dominate."
        ),
    }


def _key_for(row, scope: str):
    return (row.device,) if scope == "device" else (row.device, row.interface)


def _ensure(by_key: dict[tuple, dict], key: tuple, scope: str) -> dict:
    if key not in by_key:
        entry = {"device": key[0]}
        if scope == "interface":
            entry["interface"] = key[1]
        entry.update(
            anomaly_score=0,
            flap_count=0,
            error_count=0,
            hot_bucket_count=0,
            anomaly_breakdown={"low": 0, "medium": 0, "high": 0, "critical": 0},
        )
        by_key[key] = entry
    return by_key[key]


async def _add_anomaly_scores(
    db: AsyncSession,
    tenant_id: str,
    since: datetime,
    scope: str,
    by_key: dict,
) -> None:
    """Severity-weighted count of open anomalies scoped here."""
    q = (
        select(AnomalyEvent.scope, AnomalyEvent.severity, func.count().label("n"))
        .where(AnomalyEvent.tenant_id == tenant_id)
        .where(AnomalyEvent.last_seen_at >= since)
        .where(AnomalyEvent.resolved_at.is_(None))
        .group_by(AnomalyEvent.scope, AnomalyEvent.severity)
    )
    for r in (await db.execute(q)).all():
        parsed = _parse_scope(r.scope)
        if parsed is None:
            continue
        kind, device, interface = parsed
        if scope == "device":
            key = (device,)
        else:
            if interface is None:
                # Device-scoped anomaly doesn't pin an interface; skip in
                # interface-mode rather than spraying it across every
                # port on the device.
                continue
            key = (device, interface)
        entry = _ensure(by_key, key, scope)
        weight = SEVERITY_WEIGHT.get(r.severity, 1)
        entry["anomaly_score"] += weight * int(r.n)
        entry["anomaly_breakdown"][r.severity] = (
            entry["anomaly_breakdown"].get(r.severity, 0) + int(r.n)
        )


def _parse_scope(scope_str: str):
    """Return (kind, device, interface) for scopes the detectors write.

    Forms we know about:
      - ``device:leaf1`` → ("device", "leaf1", None)
      - ``interface:leaf1/Eth0`` → ("interface", "leaf1", "Eth0")
      - everything else returns None so global anomalies don't appear
        as a fake "offender".
    """
    if scope_str.startswith("device:"):
        return ("device", scope_str.split(":", 1)[1], None)
    if scope_str.startswith("interface:"):
        body = scope_str.split(":", 1)[1]
        if "/" in body:
            dev, iface = body.split("/", 1)
            return ("interface", dev, iface)
    return None


async def _add_flap_scores(
    db: AsyncSession,
    tenant_id: str,
    since: datetime,
    scope: str,
    by_key: dict,
) -> None:
    """Count distinct (device, interface, last_change) tuples in the window."""
    q = (
        select(
            DeviceStateMinute.device,
            DeviceStateMinute.interface,
            func.count(func.distinct(DeviceStateMinute.last_change)).label("n"),
        )
        .where(DeviceStateMinute.tenant_id == tenant_id)
        .where(DeviceStateMinute.ts_bucket >= since)
        .where(DeviceStateMinute.last_change >= since)
        .group_by(DeviceStateMinute.device, DeviceStateMinute.interface)
    )
    for r in (await db.execute(q)).all():
        key = (r.device,) if scope == "device" else (r.device, r.interface)
        entry = _ensure(by_key, key, scope)
        entry["flap_count"] += int(r.n)


async def _add_util_scores(
    db: AsyncSession,
    tenant_id: str,
    since: datetime,
    scope: str,
    by_key: dict,
) -> None:
    """Sum errors + count of hot buckets per (device[, interface])."""
    q = (
        select(
            InterfaceUtilizationMinute.device,
            InterfaceUtilizationMinute.interface,
            func.sum(InterfaceUtilizationMinute.error_count).label("errs"),
            func.sum(
                case(
                    (
                        func.greatest(
                            InterfaceUtilizationMinute.in_util_pct,
                            InterfaceUtilizationMinute.out_util_pct,
                        )
                        >= HOT_UTIL_PCT,
                        1,
                    ),
                    else_=0,
                )
            ).label("hot"),
        )
        .where(InterfaceUtilizationMinute.tenant_id == tenant_id)
        .where(InterfaceUtilizationMinute.ts_bucket >= since)
        .group_by(
            InterfaceUtilizationMinute.device,
            InterfaceUtilizationMinute.interface,
        )
    )
    for r in (await db.execute(q)).all():
        key = (r.device,) if scope == "device" else (r.device, r.interface)
        entry = _ensure(by_key, key, scope)
        entry["error_count"] += int(r.errs or 0)
        entry["hot_bucket_count"] += int(r.hot or 0)


def _normalize_and_combine(rows: list[dict]) -> None:
    """Composite = mean(normalized signals, weighted 0.4/0.3/0.15/0.15).

    Anomalies and flaps weighted higher because both are *events* (the
    operator missed something) rather than passive signals.
    """
    if not rows:
        return
    max_anomaly = max((r["anomaly_score"] for r in rows), default=0)
    max_flap = max((r["flap_count"] for r in rows), default=0)
    max_err = max((r["error_count"] for r in rows), default=0)
    max_hot = max((r["hot_bucket_count"] for r in rows), default=0)

    for r in rows:
        n_a = (r["anomaly_score"] / max_anomaly) if max_anomaly else 0.0
        n_f = (r["flap_count"] / max_flap) if max_flap else 0.0
        n_e = (r["error_count"] / max_err) if max_err else 0.0
        n_h = (r["hot_bucket_count"] / max_hot) if max_hot else 0.0
        composite = 0.4 * n_a + 0.3 * n_f + 0.15 * n_e + 0.15 * n_h
        r["composite_score"] = round(composite, 4)
        r["normalized"] = {
            "anomaly": round(n_a, 3),
            "flap": round(n_f, 3),
            "error": round(n_e, 3),
            "hot": round(n_h, 3),
        }
        drivers = []
        if n_a > 0:
            drivers.append(f"{r['anomaly_score']} weighted anomaly points")
        if n_f > 0:
            drivers.append(f"{r['flap_count']} flap(s)")
        if n_e > 0:
            drivers.append(f"{r['error_count']} errors")
        if n_h > 0:
            drivers.append(f"{r['hot_bucket_count']} hot bucket(s)")
        r["drivers"] = drivers
