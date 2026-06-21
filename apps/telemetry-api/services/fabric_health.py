"""Fabric-wide rolled-up health score (PR 29).

A single decision-ready answer to the operator's first question — "is
the fabric healthy?" Combines four independent signals and reports a
weighted score plus the per-component drivers so an LLM can explain
*why* the score is what it is without re-querying.

Signals (each scored 0..1 where 1 = healthy):

* **link**     — fraction of interfaces whose max(in,out) util_pct stayed
  below ``LINK_UTIL_WARN``. Saturated links degrade the score even if
  every other plane is clean.
* **bgp**      — fraction of observed BGP sessions in ESTABLISHED.
  Flapping or stuck sessions are a leading indicator of routing churn.
* **queue**    — penalty applied for any queue with drops, sustained PFC
  pauses, or ECN marking in the window. Exact (gNMI) telemetry.
* **freshness**— fraction of registered collectors whose last heartbeat
  is still in the ``fresh`` band. A silent collector dark-boxes the
  fabric; the score must reflect that we *don't know*, not that things
  are fine.

Overall score is a weighted average; severity is mapped off the score
band so an LLM can talk about it without doing math.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    AnomalyEvent,
    BGPSessionMinute,
    InterfaceUtilizationMinute,
    QueueStatsMinute,
    SourceFreshness,
)

# Per-component thresholds. These are intentionally conservative — the
# tool is meant to be the first thing an operator chatbot looks at, so a
# yellow score should surface real concerns rather than fire on jitter.
LINK_UTIL_WARN = 80.0       # max util_pct above which a link is "hot"
PFC_FRAMES_WARN = 1000      # window-summed PFC frames flagged as "sustained"
ECN_PACKETS_WARN = 100      # window-summed ECN marks flagged as "signaling"

WEIGHTS = {
    "link": 0.30,
    "bgp": 0.25,
    "queue": 0.25,
    "freshness": 0.20,
}


async def get_fabric_health(
    db: AsyncSession,
    tenant_id: str,
    window_minutes: int = 15,
) -> dict:
    """Compute the rolled-up fabric health for a tenant.

    The result is shaped for an LLM tool consumer: every component
    carries its own score and a one-line driver string, the overall
    score is a weighted average, and ``severity`` is the band the
    operator chatbot should quote.
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)

    link = await _score_links(db, tenant_id, since)
    bgp = await _score_bgp(db, tenant_id, since)
    queue = await _score_queues(db, tenant_id, since)
    freshness = await _score_freshness(db, tenant_id)
    open_anoms = await _open_anomaly_breakdown(db, tenant_id, since)

    overall_score = (
        link["score"] * WEIGHTS["link"]
        + bgp["score"] * WEIGHTS["bgp"]
        + queue["score"] * WEIGHTS["queue"]
        + freshness["score"] * WEIGHTS["freshness"]
    )
    severity = severity_from_score(overall_score)

    drivers = [c["driver"] for c in (link, bgp, queue, freshness) if c["driver"]]
    if open_anoms["critical"] or open_anoms["high"]:
        drivers.insert(
            0,
            f"{open_anoms['critical']} critical + {open_anoms['high']} high "
            "anomaly events are currently open.",
        )

    return {
        "window_minutes": window_minutes,
        "overall_score": round(overall_score, 3),
        "severity": severity,
        "components": {
            "link": link,
            "bgp": bgp,
            "queue": queue,
            "freshness": freshness,
        },
        "open_anomaly_counts": open_anoms,
        "drivers": drivers,
        "confidence_note": _confidence_note(link, bgp, queue, freshness),
    }


def severity_from_score(score: float) -> str:
    """Map [0, 1] score → severity band."""
    if score >= 0.90:
        return "low"
    if score >= 0.75:
        return "medium"
    if score >= 0.50:
        return "high"
    return "critical"


# ---------------------------------------------------------------------------
# Component scorers — each returns {score, driver, ...evidence}
# ---------------------------------------------------------------------------

async def _score_links(
    db: AsyncSession, tenant_id: str, since: datetime
) -> dict:
    q = (
        select(
            InterfaceUtilizationMinute.device,
            InterfaceUtilizationMinute.interface,
            func.max(
                func.greatest(
                    InterfaceUtilizationMinute.in_util_pct,
                    InterfaceUtilizationMinute.out_util_pct,
                )
            ).label("peak_util"),
        )
        .where(InterfaceUtilizationMinute.tenant_id == tenant_id)
        .where(InterfaceUtilizationMinute.ts_bucket >= since)
        .group_by(
            InterfaceUtilizationMinute.device,
            InterfaceUtilizationMinute.interface,
        )
    )
    rows = (await db.execute(q)).all()
    total = len(rows)
    if not total:
        return {
            "score": 1.0,
            "interfaces_observed": 0,
            "hot_interfaces": 0,
            "driver": None,
            "no_data": True,
        }
    hot = [
        {
            "device": r.device,
            "interface": r.interface,
            "peak_util_pct": round(float(r.peak_util or 0), 2),
        }
        for r in rows
        if (r.peak_util or 0) >= LINK_UTIL_WARN
    ]
    score = 1.0 - (len(hot) / total)
    driver = None
    if hot:
        driver = (
            f"{len(hot)} of {total} interfaces exceeded "
            f"{LINK_UTIL_WARN:.0f}% peak utilization in the window."
        )
    return {
        "score": round(score, 3),
        "interfaces_observed": total,
        "hot_interfaces": len(hot),
        "top_hot": sorted(hot, key=lambda h: -h["peak_util_pct"])[:5],
        "driver": driver,
        "no_data": False,
    }


async def _score_bgp(
    db: AsyncSession, tenant_id: str, since: datetime
) -> dict:
    """Fraction of (device, peer) pairs whose latest state is ESTABLISHED."""
    # Pick the latest sample per peer in window — a single CONNECT mid-window
    # shouldn't count as down if the session recovered.
    latest_ts_q = (
        select(
            BGPSessionMinute.device,
            BGPSessionMinute.peer_address,
            func.max(BGPSessionMinute.ts_bucket).label("ts"),
        )
        .where(BGPSessionMinute.tenant_id == tenant_id)
        .where(BGPSessionMinute.ts_bucket >= since)
        .group_by(BGPSessionMinute.device, BGPSessionMinute.peer_address)
    ).subquery()

    q = (
        select(
            BGPSessionMinute.device,
            BGPSessionMinute.peer_address,
            BGPSessionMinute.session_state,
            BGPSessionMinute.last_error,
        )
        .join(
            latest_ts_q,
            (BGPSessionMinute.device == latest_ts_q.c.device)
            & (BGPSessionMinute.peer_address == latest_ts_q.c.peer_address)
            & (BGPSessionMinute.ts_bucket == latest_ts_q.c.ts),
        )
        .where(BGPSessionMinute.tenant_id == tenant_id)
    )
    rows = (await db.execute(q)).all()
    total = len(rows)
    if not total:
        return {
            "score": 1.0,
            "peers_observed": 0,
            "peers_down": 0,
            "driver": None,
            "no_data": True,
        }
    down = [
        {
            "device": r.device,
            "peer": r.peer_address,
            "state": r.session_state,
            "last_error": r.last_error,
        }
        for r in rows
        if r.session_state != "ESTABLISHED"
    ]
    score = 1.0 - (len(down) / total)
    driver = None
    if down:
        driver = f"{len(down)} of {total} BGP peers are not ESTABLISHED."
    return {
        "score": round(score, 3),
        "peers_observed": total,
        "peers_down": len(down),
        "down_peers": down[:10],
        "driver": driver,
        "no_data": False,
    }


async def _score_queues(
    db: AsyncSession, tenant_id: str, since: datetime
) -> dict:
    """Penalize drops > sustained PFC > ECN. Exact (gNMI) telemetry."""
    q = (
        select(
            QueueStatsMinute.device,
            QueueStatsMinute.interface,
            QueueStatsMinute.queue_id,
            func.sum(QueueStatsMinute.pfc_pause_rx).label("pfc_rx"),
            func.sum(QueueStatsMinute.pfc_pause_tx).label("pfc_tx"),
            func.sum(QueueStatsMinute.ecn_marked_packets).label("ecn"),
            func.sum(QueueStatsMinute.dropped_packets).label("drops"),
        )
        .where(QueueStatsMinute.tenant_id == tenant_id)
        .where(QueueStatsMinute.ts_bucket >= since)
        .group_by(
            QueueStatsMinute.device,
            QueueStatsMinute.interface,
            QueueStatsMinute.queue_id,
        )
    )
    rows = (await db.execute(q)).all()
    if not rows:
        return {
            "score": 1.0,
            "queues_observed": 0,
            "queues_with_drops": 0,
            "queues_with_pfc": 0,
            "queues_with_ecn": 0,
            "driver": None,
            "no_data": True,
        }
    drops_n = sum(1 for r in rows if (r.drops or 0) > 0)
    pfc_n = sum(
        1 for r in rows if ((r.pfc_rx or 0) + (r.pfc_tx or 0)) >= PFC_FRAMES_WARN
    )
    ecn_n = sum(1 for r in rows if (r.ecn or 0) >= ECN_PACKETS_WARN)
    total = len(rows)
    # Drops are the worst signal — full penalty per offending queue.
    # PFC is half-weighted, ECN quarter-weighted: the fabric is *talking*,
    # not failing.
    penalty = (drops_n + pfc_n * 0.5 + ecn_n * 0.25) / total
    score = max(0.0, 1.0 - penalty)
    driver_parts = []
    if drops_n:
        driver_parts.append(f"{drops_n} queue(s) dropped packets")
    if pfc_n:
        driver_parts.append(f"{pfc_n} queue(s) saw sustained PFC")
    if ecn_n:
        driver_parts.append(f"{ecn_n} queue(s) ECN-marked")
    driver = "; ".join(driver_parts) + "." if driver_parts else None
    return {
        "score": round(score, 3),
        "queues_observed": total,
        "queues_with_drops": drops_n,
        "queues_with_pfc": pfc_n,
        "queues_with_ecn": ecn_n,
        "driver": driver,
        "no_data": False,
    }


async def _score_freshness(db: AsyncSession, tenant_id: str) -> dict:
    """A silent collector is a hole in the picture — score reflects that."""
    q = select(SourceFreshness).where(SourceFreshness.tenant_id == tenant_id)
    rows = (await db.execute(q)).scalars().all()
    if not rows:
        return {
            "score": 1.0,
            "sources_observed": 0,
            "sources_stale": 0,
            "sources_silent": 0,
            "driver": None,
            "no_data": True,
        }
    silent = [r for r in rows if r.status == "silent"]
    stale = [r for r in rows if r.status == "stale"]
    total = len(rows)
    # Silent = full penalty, stale = half penalty.
    score = 1.0 - ((len(silent) + 0.5 * len(stale)) / total)
    score = max(0.0, score)
    driver = None
    if silent:
        driver = (
            f"{len(silent)} collector(s) silent — fabric state is partially "
            "unknown."
        )
    elif stale:
        driver = f"{len(stale)} collector(s) stale (recovering)."
    return {
        "score": round(score, 3),
        "sources_observed": total,
        "sources_stale": len(stale),
        "sources_silent": len(silent),
        "silent_sources": [
            {"source_kind": r.source_kind, "device": r.device}
            for r in silent[:10]
        ],
        "driver": driver,
        "no_data": False,
    }


async def _open_anomaly_breakdown(
    db: AsyncSession, tenant_id: str, since: datetime
) -> dict:
    q = (
        select(AnomalyEvent.severity, func.count())
        .where(AnomalyEvent.tenant_id == tenant_id)
        .where(AnomalyEvent.resolved_at.is_(None))
        .where(AnomalyEvent.last_seen_at >= since)
        .group_by(AnomalyEvent.severity)
    )
    counts = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    for sev, n in (await db.execute(q)).all():
        if sev in counts:
            counts[sev] = int(n)
    return counts


def _confidence_note(link, bgp, queue, freshness) -> str:
    """Honest about which planes we couldn't observe.

    The chatbot should never quote a green score when half the inputs
    are missing — degrade the language explicitly so the model passes
    that on.
    """
    missing = []
    if link.get("no_data"):
        missing.append("interface counters")
    if bgp.get("no_data"):
        missing.append("BGP state")
    if queue.get("no_data"):
        missing.append("queue/PFC/ECN")
    if freshness.get("no_data"):
        missing.append("collector heartbeats")
    base = (
        "Score blends sFlow-derived link utilization (sampled, ±10%) with "
        "exact gNMI BGP and queue telemetry."
    )
    if missing:
        return base + (
            f" Note: no data observed for {', '.join(missing)} — the "
            "corresponding component score defaults to 1.0 and the overall "
            "score should be treated as partial."
        )
    return base
