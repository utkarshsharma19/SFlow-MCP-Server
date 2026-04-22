"""Tool schema contract test (PR 29 hardening).

The LLM-facing tool surface is the blast radius for this product: if a
tool's parameters or defaults change without the model being retrained
on the new schema, calls start failing silently or producing garbage.

This test snapshots every MCP tool's signature to
``apps/mcp-server/schemas/tool_contracts.json`` and fails the build on
drift. To intentionally change a tool's contract, bump that tool's
``version`` and update the committed ``signature`` in the same PR — the
version bump is the review signal that old clients/prompts need to
migrate.
"""
from __future__ import annotations

import importlib
import inspect
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER_DIR = PROJECT_ROOT / "apps" / "mcp-server"
CONTRACT_PATH = MCP_SERVER_DIR / "schemas" / "tool_contracts.json"

TOOL_MODULES = {
    "compare_traffic_windows": ("compare_windows", "compare_traffic_windows"),
    "detect_fabric_imbalance": ("fabric_imbalance", "detect_fabric_imbalance"),
    "explain_hot_link": ("explain_hot_link", "explain_hot_link"),
    "get_device_state": ("device_state", "get_device_state"),
    "get_interface_utilization": ("link_utilization", "get_interface_utilization"),
    "get_rdma_health": ("rdma_health", "get_rdma_health"),
    "get_recent_anomalies": ("recent_anomalies", "get_recent_anomalies"),
    "get_top_talkers": ("top_talkers", "get_top_talkers"),
    "summarize_anomalies": ("anomaly_summary", "summarize_anomalies"),
    "summarize_protocol_mix": ("protocol_mix", "summarize_protocol_mix"),
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


@pytest.fixture
def tool_fns(monkeypatch):
    _clear_mcp_modules()
    monkeypatch.syspath_prepend(str(MCP_SERVER_DIR))
    fns: dict[str, callable] = {}
    for tool_name, (module_name, attr) in TOOL_MODULES.items():
        module = importlib.import_module(f"tools.{module_name}")
        fns[tool_name] = getattr(module, attr)
    return fns


def _load_contract() -> dict:
    with open(CONTRACT_PATH) as f:
        return json.load(f)


def test_contract_covers_every_registered_tool(tool_fns):
    contract = _load_contract()
    contract_tools = set(contract["tools"])
    live_tools = set(tool_fns)
    missing = live_tools - contract_tools
    extra = contract_tools - live_tools
    assert not missing, f"tools missing from contract: {sorted(missing)}"
    assert not extra, f"contract references non-existent tools: {sorted(extra)}"


def test_every_tool_signature_matches_committed_contract(tool_fns):
    """Any drift here is a breaking change — bump version + update JSON."""
    contract = _load_contract()["tools"]
    drift: list[str] = []
    for tool_name, fn in tool_fns.items():
        # functools.wraps chain is followed by inspect.signature — this
        # returns the ORIGINAL (inner) function signature, not the
        # decorated wrapper. That's what we want: the contract is the
        # tool's input shape, not the middleware.
        actual = str(inspect.signature(fn))
        expected = contract[tool_name]["signature"]
        if actual != expected:
            drift.append(f"{tool_name}:\n  expected: {expected}\n  actual:   {actual}")
    assert not drift, (
        "Tool signatures drifted from committed contract. If intentional, "
        "bump the tool's version and update tool_contracts.json:\n\n"
        + "\n\n".join(drift)
    )


def test_every_tool_has_semver_version():
    contract = _load_contract()["tools"]
    for tool_name, spec in contract.items():
        v = spec.get("version", "")
        parts = v.split(".")
        assert len(parts) >= 2 and all(p.isdigit() for p in parts), (
            f"{tool_name} has invalid version {v!r}; use semver like '1.0' or '2.1.3'"
        )
