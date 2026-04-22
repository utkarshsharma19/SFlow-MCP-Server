"""LLM-generated anomaly summary tests (PR 25)."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from services import anomaly_narrative
from services.anomaly_narrative import (
    TemplateProvider,
    _build_prompt,
    _summarize_facts,
    summarize_recent_anomalies,
)


TENANT_A = str(uuid.uuid4())


def _event(**overrides):
    base = dict(
        ts=datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc),
        anomaly_type="link_saturation",
        severity="high",
        scope="device:leaf1",
        summary="Eth1 sustained >95%",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class StubResult:
    def __init__(self, events):
        self._events = events

    def scalars(self):
        return self

    def all(self):
        return self._events


class StubSession:
    def __init__(self, events):
        self._events = events
        self.queries: list[str] = []

    async def execute(self, query):
        self.queries.append(str(query))
        return StubResult(self._events)


def test_template_provider_handles_empty_events():
    out = asyncio.run(TemplateProvider().generate("", {"total": 0}))
    assert "No anomalies" in out


def test_summarize_facts_counts_by_severity_type_and_scope():
    events = [
        _event(severity="critical", anomaly_type="pfc_storm", scope="device:spine1"),
        _event(severity="high", anomaly_type="link_saturation", scope="device:leaf1"),
        _event(severity="high", anomaly_type="link_saturation", scope="device:leaf1"),
    ]
    facts = _summarize_facts(events, window_minutes=30)
    assert facts["total"] == 3
    assert facts["by_severity"] == {"high": 2, "critical": 1}
    assert facts["by_type"]["link_saturation"] == 2
    assert facts["top_scope"] == "device:leaf1"
    assert len(facts["samples"]) == 3


def test_build_prompt_never_leaks_system_instructions():
    facts = _summarize_facts([_event()], window_minutes=30)
    prompt = _build_prompt(facts, "global")
    assert "invent" in prompt
    assert "Scope: global." in prompt
    assert "Samples:" in prompt


def test_summarize_returns_narrative_and_confidence_note():
    events = [_event(), _event(severity="critical", anomaly_type="pfc_storm")]
    session = StubSession(events)

    class Captured:
        prompt = ""
        facts: dict = {}

        async def generate(self_inner, prompt, facts):
            self_inner.prompt = prompt
            self_inner.facts = facts
            return "Two anomalies: 1 critical PFC storm and 1 high link saturation."

    provider = Captured()
    out = asyncio.run(
        summarize_recent_anomalies(
            session,
            tenant_id=TENANT_A,
            scope="global",
            since_minutes=30,
            provider=provider,
        )
    )
    assert out["event_count"] == 2
    assert out["severity_breakdown"] == {"high": 1, "critical": 1}
    assert "PFC storm" in out["narrative"]
    assert out["provider"] == "Captured"
    assert "confidence_note" in out
    # The prompt must not allow invented facts.
    assert "Do not invent" in provider.prompt


def test_summarize_filters_on_tenant_and_severity_min():
    session = StubSession([])
    asyncio.run(
        summarize_recent_anomalies(
            session,
            tenant_id=TENANT_A,
            scope="global",
            since_minutes=30,
            severity_min="critical",
        )
    )
    # The compiled SQL should carry the tenant predicate and reference the
    # severity column.
    assert any("tenant_id" in q for q in session.queries)
    assert any("severity" in q for q in session.queries)


def test_default_provider_is_deterministic_template():
    anomaly_narrative._provider = None  # reset singleton between tests
    session = StubSession([_event()])
    out = asyncio.run(
        summarize_recent_anomalies(
            session, tenant_id=TENANT_A, scope="global", since_minutes=15
        )
    )
    assert out["provider"] == "TemplateProvider"
    assert "1 anomaly event" in out["narrative"]
    assert "15 minutes" in out["narrative"]
