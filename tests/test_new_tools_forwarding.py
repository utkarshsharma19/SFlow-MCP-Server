"""Smoke tests for the PR 29 MCP tools.

Each test stubs the telemetry-API call and asserts the tool forwards
the right path + params/body. Keeps the wiring honest without spinning
the full stack.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER_DIR = PROJECT_ROOT / "apps" / "mcp-server"


def _clear_mcp_modules() -> None:
    for name in list(sys.modules):
        if (
            name in {"app", "client", "server", "middleware", "resources", "tools"}
            or name.startswith("middleware.")
            or name.startswith("resources.")
            or name.startswith("tools.")
        ):
            sys.modules.pop(name)


def _import_server(monkeypatch):
    _clear_mcp_modules()
    monkeypatch.syspath_prepend(str(MCP_SERVER_DIR))
    return importlib.import_module("server")


def _run(coro):
    return asyncio.run(coro)


def _json_payload(result):
    assert len(result) == 1
    return json.loads(result[0].text)


def test_get_fabric_health_forwards_window(monkeypatch):
    server = _import_server(monkeypatch)
    from tools import fabric_health

    captured = {}

    async def fake_get(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return {"overall_score": 0.95, "severity": "low"}

    monkeypatch.setattr(fabric_health, "get_telemetry", fake_get)

    result = _run(server.mcp.call_tool("get_fabric_health", {"window_minutes": 30}))
    assert captured == {"path": "/fabric/health", "params": {"window_minutes": 30}}
    assert _json_payload(result) == {"overall_score": 0.95, "severity": "low"}


def test_get_fabric_health_rejects_bad_window(monkeypatch):
    server = _import_server(monkeypatch)
    result = _run(server.mcp.call_tool("get_fabric_health", {"window_minutes": 999}))
    assert _json_payload(result) == {
        "error": "window_minutes must be between 1 and 60"
    }


def test_intent_diff_forwards_device(monkeypatch):
    server = _import_server(monkeypatch)
    from tools import intent_diff

    captured = {}

    async def fake_get(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return {"total_drift_count": 0, "severity": "low"}

    monkeypatch.setattr(intent_diff, "get_telemetry", fake_get)

    result = _run(
        server.mcp.call_tool(
            "diff_config_intent_vs_state", {"device": "leaf1"}
        )
    )
    assert captured == {"path": "/intent/diff", "params": {"device": "leaf1"}}
    assert _json_payload(result) == {"total_drift_count": 0, "severity": "low"}


def test_intent_diff_omits_device_when_none(monkeypatch):
    server = _import_server(monkeypatch)
    from tools import intent_diff

    captured = {}

    async def fake_get(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return {"total_drift_count": 0}

    monkeypatch.setattr(intent_diff, "get_telemetry", fake_get)

    _run(server.mcp.call_tool("diff_config_intent_vs_state", {}))
    # Empty params dict — never send ?device= to the API.
    assert captured["params"] == {}


def test_acknowledge_anomaly_posts_with_action(monkeypatch):
    server = _import_server(monkeypatch)
    from tools import acknowledge_anomaly

    captured = {}

    async def fake_post(path, json=None):
        captured["path"] = path
        captured["json"] = json
        return {"anomaly_id": "abc", "status": "acknowledged"}

    monkeypatch.setattr(acknowledge_anomaly, "post_telemetry", fake_post)

    result = _run(
        server.mcp.call_tool(
            "acknowledge_anomaly",
            {"anomaly_id": "abc", "action": "acknowledge"},
        )
    )
    assert captured["path"] == "/anomalies/abc/acknowledge"
    assert _json_payload(result)["status"] == "acknowledged"


def test_acknowledge_anomaly_supports_resolve(monkeypatch):
    server = _import_server(monkeypatch)
    from tools import acknowledge_anomaly

    captured = {}

    async def fake_post(path, json=None):
        captured["path"] = path
        return {"anomaly_id": "abc", "status": "resolved"}

    monkeypatch.setattr(acknowledge_anomaly, "post_telemetry", fake_post)

    _run(
        server.mcp.call_tool(
            "acknowledge_anomaly",
            {"anomaly_id": "abc", "action": "resolve"},
        )
    )
    assert captured["path"] == "/anomalies/abc/resolve"


def test_acknowledge_anomaly_rejects_unknown_action(monkeypatch):
    server = _import_server(monkeypatch)
    result = _run(
        server.mcp.call_tool(
            "acknowledge_anomaly",
            {"anomaly_id": "abc", "action": "delete"},
        )
    )
    payload = _json_payload(result)
    assert "error" in payload
    assert "acknowledge" in payload["error"] and "resolve" in payload["error"]


def test_acknowledge_anomaly_rejects_missing_id(monkeypatch):
    server = _import_server(monkeypatch)
    result = _run(
        server.mcp.call_tool(
            "acknowledge_anomaly",
            {"anomaly_id": "", "action": "acknowledge"},
        )
    )
    assert _json_payload(result) == {"error": "anomaly_id is required"}
