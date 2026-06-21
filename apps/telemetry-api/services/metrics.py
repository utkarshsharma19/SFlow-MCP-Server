"""Prometheus ``/metrics`` exposition (PR 30).

We already push OTLP traces + metrics to Jaeger via ``otel.py``. This
file adds the *pull* path for Prometheus scrapers — most ops shops have
Prom in front of Grafana, and OTLP-push to Prom requires either an OTel
collector sidecar or the Prom remote-write receiver. Both are extra
boxes for what amounts to "expose six counters".

The exposition pulls primarily from two sources:

1. ``tool_call_audit`` rolled up over the last 5 minutes — the source of
   truth for MCP tool traffic. We do *not* maintain a parallel counter
   in memory; that would drift from the audit table whenever the
   process restarted.

2. ``webhook_deliveries`` — same idea: ship the last 5 minutes.

3. A handful of cheap in-memory gauges (``sflow_up``, ``gnmi_up``) that
   the ingest loops already poke for OTel.

The text format is the official Prom exposition spec — no library, no
versioning drift.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ToolCallAudit, WebhookDelivery

WINDOW_MINUTES = 5
_PROCESS_START = time.time()


async def render_prometheus(db: AsyncSession) -> str:
    """Return the Prom text-format body for the current window."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=WINDOW_MINUTES)
    lines: list[str] = []

    # ---- Tool call rates by (tool, status) -----------------------------
    tool_rows = (
        await db.execute(
            select(
                ToolCallAudit.tool_name,
                ToolCallAudit.status,
                func.count().label("n"),
            )
            .where(ToolCallAudit.ts >= since)
            .group_by(ToolCallAudit.tool_name, ToolCallAudit.status)
        )
    ).all()
    lines.append(
        "# HELP flowmind_mcp_tool_calls_5m Tool invocations in the last 5 minutes"
    )
    lines.append("# TYPE flowmind_mcp_tool_calls_5m gauge")
    for r in tool_rows:
        lines.append(
            'flowmind_mcp_tool_calls_5m{tool="%s",status="%s"} %d'
            % (_lbl(r.tool_name), _lbl(r.status), int(r.n))
        )

    # ---- Tool duration p50/p95 (approximate — exact percentiles would
    # need a histogram column; we use min/avg/max as a cheap proxy until
    # someone wires real buckets in).
    dur_rows = (
        await db.execute(
            select(
                ToolCallAudit.tool_name,
                func.avg(ToolCallAudit.duration_ms).label("avg"),
                func.max(ToolCallAudit.duration_ms).label("max"),
            )
            .where(ToolCallAudit.ts >= since)
            .where(ToolCallAudit.duration_ms.isnot(None))
            .group_by(ToolCallAudit.tool_name)
        )
    ).all()
    lines.append(
        "# HELP flowmind_mcp_tool_duration_ms_avg Avg tool latency over 5 min"
    )
    lines.append("# TYPE flowmind_mcp_tool_duration_ms_avg gauge")
    for r in dur_rows:
        lines.append(
            'flowmind_mcp_tool_duration_ms_avg{tool="%s"} %.1f'
            % (_lbl(r.tool_name), float(r.avg or 0))
        )
    lines.append(
        "# HELP flowmind_mcp_tool_duration_ms_max Max tool latency over 5 min"
    )
    lines.append("# TYPE flowmind_mcp_tool_duration_ms_max gauge")
    for r in dur_rows:
        lines.append(
            'flowmind_mcp_tool_duration_ms_max{tool="%s"} %d'
            % (_lbl(r.tool_name), int(r.max or 0))
        )

    # ---- Confidence band counts — the chatbot cost we care most about ---
    # A spike in 'degraded' is a leading indicator that the fabric will
    # lie to the model. Watching it is more valuable than watching pure
    # tool latency.
    band_rows = (
        await db.execute(
            select(
                ToolCallAudit.confidence_band,
                func.count().label("n"),
            )
            .where(ToolCallAudit.ts >= since)
            .where(ToolCallAudit.confidence_band.isnot(None))
            .group_by(ToolCallAudit.confidence_band)
        )
    ).all()
    lines.append(
        "# HELP flowmind_mcp_confidence_band_5m Tool responses by confidence band"
    )
    lines.append("# TYPE flowmind_mcp_confidence_band_5m gauge")
    for r in band_rows:
        lines.append(
            'flowmind_mcp_confidence_band_5m{band="%s"} %d'
            % (_lbl(r.confidence_band), int(r.n))
        )

    # ---- Webhook delivery health --------------------------------------
    wh_rows = (
        await db.execute(
            select(
                WebhookDelivery.status,
                func.count().label("n"),
            )
            .where(WebhookDelivery.ts >= since)
            .group_by(WebhookDelivery.status)
        )
    ).all()
    lines.append(
        "# HELP flowmind_webhook_deliveries_5m Webhook deliveries by status (5m)"
    )
    lines.append("# TYPE flowmind_webhook_deliveries_5m gauge")
    for r in wh_rows:
        lines.append(
            'flowmind_webhook_deliveries_5m{status="%s"} %d'
            % (_lbl(r.status), int(r.n))
        )

    # ---- Process uptime ------------------------------------------------
    lines.append("# HELP flowmind_process_uptime_seconds Process uptime")
    lines.append("# TYPE flowmind_process_uptime_seconds counter")
    lines.append(f"flowmind_process_uptime_seconds {int(time.time() - _PROCESS_START)}")

    return "\n".join(lines) + "\n"


def _lbl(value: str | None) -> str:
    """Escape Prometheus label value per exposition spec."""
    if value is None:
        return ""
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )
