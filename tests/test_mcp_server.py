import asyncio
import importlib
import json
import runpy
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER_DIR = PROJECT_ROOT / "apps" / "mcp-server"

EXPECTED_TOOLS = {
    "compare_traffic_windows",
    "explain_hot_link",
    "get_interface_utilization",
    "get_recent_anomalies",
    "get_top_talkers",
    "summarize_protocol_mix",
}

EXPECTED_RESOURCES = {
    "inventory://devices",
    "inventory://interfaces",
}


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


def test_registers_expected_tools_and_resources(monkeypatch):
    server = _import_server(monkeypatch)

    tools = _run(server.mcp.list_tools())
    resources = _run(server.mcp.list_resources())

    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    assert {str(resource.uri) for resource in resources} == EXPECTED_RESOURCES


def test_script_entrypoint_runs_server_with_registered_tools(monkeypatch):
    from mcp.server.fastmcp import FastMCP

    captured = {}

    def fake_run(self, transport="stdio", mount_path=None):
        captured["transport"] = transport
        captured["tools"] = {tool.name for tool in _run(self.list_tools())}
        captured["resources"] = {str(resource.uri) for resource in _run(self.list_resources())}

    monkeypatch.setattr(FastMCP, "run", fake_run)
    monkeypatch.syspath_prepend(str(MCP_SERVER_DIR))
    monkeypatch.setattr(sys, "argv", ["server.py"])
    _clear_mcp_modules()

    runpy.run_path(str(MCP_SERVER_DIR / "server.py"), run_name="__main__")

    assert captured == {
        "transport": "stdio",
        "tools": EXPECTED_TOOLS,
        "resources": EXPECTED_RESOURCES,
    }


def test_validation_errors_are_structured(monkeypatch):
    server = _import_server(monkeypatch)

    top_talkers = _run(
        server.mcp.call_tool("get_top_talkers", {"window_minutes": 0, "limit": 10})
    )
    anomalies = _run(server.mcp.call_tool("get_recent_anomalies", {"severity_min": "invalid"}))

    assert _json_payload(top_talkers) == {
        "error": "window_minutes must be between 1 and 60",
    }
    assert _json_payload(anomalies) == {
        "error": "severity_min must be low|medium|high|critical",
    }


def test_tool_forwards_expected_request_to_telemetry_api(monkeypatch):
    server = _import_server(monkeypatch)
    from tools import top_talkers

    captured = {}

    async def fake_get_telemetry(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return {"ok": True, "items": []}

    monkeypatch.setattr(top_talkers, "get_telemetry", fake_get_telemetry)

    result = _run(
        server.mcp.call_tool(
            "get_top_talkers",
            {"window_minutes": 5, "scope": "device:core-sw-01", "limit": 3},
        )
    )

    assert captured == {
        "path": "/flows/top-talkers",
        "params": {
            "window_minutes": 5,
            "scope": "device:core-sw-01",
            "limit": 3,
        },
    }
    assert _json_payload(result) == {"ok": True, "items": []}
