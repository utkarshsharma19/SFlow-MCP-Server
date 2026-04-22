"""LLM-generated anomaly summaries (PR 25).

Takes the last N anomaly events for a tenant/scope and produces a short
operator-facing narrative. The LLM call is pluggable: the default provider
is a deterministic template so tests and air-gapped deployments work
without network access; a real provider can be wired in via
``FLOWMIND_LLM_PROVIDER``.

The narrative is *advisory*. It never invents counts, severities, or
device names — it is built from the same event rows the viewer would see
via ``/anomalies/recent``. That's what keeps it safe to expose as an MCP
tool.
"""
from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Iterable, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AnomalyEvent

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
MAX_EVENTS_IN_PROMPT = 25


class NarrativeProvider(Protocol):
    async def generate(self, prompt: str, facts: dict) -> str: ...


class TemplateProvider:
    """Deterministic fallback: builds a narrative from structured facts.

    Used in tests and when no LLM is configured. Never calls the network.
    """

    async def generate(self, prompt: str, facts: dict) -> str:
        total = facts["total"]
        if total == 0:
            return "No anomalies detected in the requested window."

        by_severity = facts["by_severity"]
        by_type = facts["by_type"]
        top_scope = facts["top_scope"]

        sev_phrase = ", ".join(
            f"{n} {sev}"
            for sev, n in sorted(
                by_severity.items(),
                key=lambda kv: -SEVERITY_RANK.get(kv[0], 0),
            )
            if n
        )
        type_phrase = ", ".join(f"{n}× {t}" for t, n in by_type.most_common(3))
        lead = (
            f"{total} anomaly {'event' if total == 1 else 'events'} "
            f"in the last {facts['window_minutes']} minutes"
        )
        focus = f" Most activity on {top_scope}." if top_scope else ""
        return f"{lead} — {sev_phrase}. Dominant types: {type_phrase}.{focus}"


_provider: NarrativeProvider | None = None


def set_provider(provider: NarrativeProvider) -> None:
    """Swap the active provider (used by tests and wiring)."""
    global _provider
    _provider = provider


def _default_provider() -> NarrativeProvider:
    global _provider
    if _provider is None:
        # Future: branch on FLOWMIND_LLM_PROVIDER to select a real LLM client.
        # The template provider is intentionally the default so we never
        # silently emit fabricated narratives in production.
        _provider = TemplateProvider()
    return _provider


async def summarize_recent_anomalies(
    db: AsyncSession,
    tenant_id: str,
    scope: str = "global",
    since_minutes: int = 30,
    severity_min: str = "medium",
    provider: NarrativeProvider | None = None,
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
        .limit(MAX_EVENTS_IN_PROMPT)
    )
    if scope != "global":
        q = q.where(AnomalyEvent.scope.contains(scope))

    events = (await db.execute(q)).scalars().all()
    facts = _summarize_facts(events, since_minutes)
    prompt = _build_prompt(facts, scope)

    prov = provider or _default_provider()
    narrative = await prov.generate(prompt, facts)

    return {
        "scope": scope,
        "window_minutes": since_minutes,
        "severity_min": severity_min,
        "event_count": facts["total"],
        "severity_breakdown": facts["by_severity"],
        "narrative": narrative,
        "provider": type(prov).__name__,
        "confidence_note": (
            "Narrative is derived from stored anomaly events only; it does "
            "not introduce new facts. When the LLM provider is the built-in "
            "template, wording is deterministic and identical runs yield "
            "identical text."
        ),
    }


def _summarize_facts(events: Iterable, window_minutes: int) -> dict:
    events = list(events)
    by_severity: dict[str, int] = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    by_type: Counter[str] = Counter()
    by_scope: Counter[str] = Counter()
    for e in events:
        by_severity[e.severity] = by_severity.get(e.severity, 0) + 1
        by_type[e.anomaly_type] += 1
        by_scope[e.scope] += 1
    top_scope = by_scope.most_common(1)[0][0] if by_scope else ""
    return {
        "total": len(events),
        "by_severity": {k: v for k, v in by_severity.items() if v},
        "by_type": by_type,
        "top_scope": top_scope,
        "window_minutes": window_minutes,
        "samples": [
            {
                "ts": e.ts.isoformat(),
                "type": e.anomaly_type,
                "severity": e.severity,
                "scope": e.scope,
                "summary": e.summary,
            }
            for e in events[:5]
        ],
    }


def _build_prompt(facts: dict, scope: str) -> str:
    lines = [
        "You are a senior network reliability engineer.",
        "Summarize the following anomalies in <=3 sentences for an ops shift handoff.",
        "Do not invent counts, devices, or severities beyond the facts provided.",
        f"Scope: {scope}.",
        f"Window: {facts['window_minutes']} minutes.",
        f"Severity counts: {facts['by_severity']}.",
        f"Top types: {dict(facts['by_type'].most_common(5))}.",
        f"Top scope: {facts['top_scope']}.",
        "Samples:",
    ]
    for s in facts["samples"]:
        lines.append(
            f"- {s['ts']} [{s['severity']}] {s['type']} @ {s['scope']}: {s['summary']}"
        )
    return "\n".join(lines)
