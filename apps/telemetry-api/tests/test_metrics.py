"""Prometheus exposition tests (PR 30)."""
from __future__ import annotations

import asyncio

from services import metrics
from services.metrics import _lbl, render_prometheus


def test_lbl_escapes_special_chars():
    """Backslash, newline, and double-quote are the three the spec requires escaped."""
    assert _lbl('a"b') == 'a\\"b'
    assert _lbl("a\\b") == "a\\\\b"
    assert _lbl("a\nb") == "a\\nb"
    assert _lbl(None) == ""


def test_render_prometheus_handles_empty_state():
    """Fresh DB → valid Prom text with HELP/TYPE lines and no datapoints."""

    class FakeResult:
        def all(self_inner):
            return []

    class FakeSession:
        async def execute(self_inner, q):
            return FakeResult()

    body = asyncio.run(render_prometheus(FakeSession()))
    # Always emit HELP/TYPE preambles so a Prom scraper sees the metric
    # exists even when count is zero.
    assert "# HELP flowmind_mcp_tool_calls_5m" in body
    assert "# TYPE flowmind_mcp_tool_calls_5m gauge" in body
    assert "# HELP flowmind_mcp_confidence_band_5m" in body
    assert "flowmind_process_uptime_seconds " in body
    # No payload lines — only headers + uptime.
    for line in body.splitlines():
        if line.startswith("#") or line.startswith("flowmind_process_uptime"):
            continue
        assert line == "", f"unexpected datapoint: {line!r}"


def test_render_prometheus_emits_labelled_datapoints():
    class _Row:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    tool_rows = [
        _Row(tool_name="get_top_talkers", status="ok", n=12),
        _Row(tool_name="get_top_talkers", status="error", n=1),
    ]
    dur_rows = [_Row(tool_name="get_top_talkers", avg=42.0, max=120)]
    band_rows = [_Row(confidence_band="sampled", n=10)]
    wh_rows = [_Row(status="ok", n=3)]

    class FakeResult:
        def __init__(self_inner, rows):
            self_inner._rows = rows

        def all(self_inner):
            return self_inner._rows

    class FakeSession:
        def __init__(self_inner):
            self_inner.calls = 0

        async def execute(self_inner, q):
            self_inner.calls += 1
            return [
                FakeResult(tool_rows),
                FakeResult(dur_rows),
                FakeResult(band_rows),
                FakeResult(wh_rows),
            ][self_inner.calls - 1]

    body = asyncio.run(render_prometheus(FakeSession()))
    assert (
        'flowmind_mcp_tool_calls_5m{tool="get_top_talkers",status="ok"} 12'
        in body
    )
    assert (
        'flowmind_mcp_tool_calls_5m{tool="get_top_talkers",status="error"} 1'
        in body
    )
    assert 'flowmind_mcp_confidence_band_5m{band="sampled"} 10' in body
    assert 'flowmind_webhook_deliveries_5m{status="ok"} 3' in body


def test_window_minutes_documented_constant():
    """A docs/PR drift check — if we change this, dashboards need to follow."""
    assert metrics.WINDOW_MINUTES == 5
