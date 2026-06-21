"""Per-path circuit breaker tests (PR 30)."""
from __future__ import annotations

import importlib
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


def _import_client(monkeypatch):
    _clear_mcp_modules()
    monkeypatch.syspath_prepend(str(MCP_SERVER_DIR))
    return importlib.import_module("client")


def test_breaker_starts_closed(monkeypatch):
    c = _import_client(monkeypatch)
    c._reset_breakers_for_tests()
    state = c._breaker_for("/foo")
    assert c._circuit_is_open(state, now=0.0) is False


def test_breaker_opens_at_threshold(monkeypatch):
    c = _import_client(monkeypatch)
    c._reset_breakers_for_tests()
    state = c._breaker_for("/foo")
    now = 100.0
    for _ in range(c.CB_FAILURE_THRESHOLD):
        c._record_failure(state, now)
    assert c._circuit_is_open(state, now) is True


def test_breaker_half_opens_after_cooldown(monkeypatch):
    c = _import_client(monkeypatch)
    c._reset_breakers_for_tests()
    state = c._breaker_for("/foo")
    now = 0.0
    for _ in range(c.CB_FAILURE_THRESHOLD):
        c._record_failure(state, now)
    assert c._circuit_is_open(state, now) is True
    # After cooldown elapses, breaker is half-open (i.e. is_open=False)
    future = now + c.CB_COOLDOWN_SECONDS + 0.1
    assert c._circuit_is_open(state, future) is False


def test_breaker_closes_on_recorded_success(monkeypatch):
    c = _import_client(monkeypatch)
    c._reset_breakers_for_tests()
    state = c._breaker_for("/foo")
    for _ in range(c.CB_FAILURE_THRESHOLD):
        c._record_failure(state, now=0.0)
    c._record_success(state)
    assert state.failures == 0
    assert state.opened_at == 0.0
    assert c._circuit_is_open(state, now=1.0) is False


def test_open_response_shape(monkeypatch):
    c = _import_client(monkeypatch)
    resp = c._open_response("/foo")
    assert resp["error"] == "telemetry_unavailable"
    assert resp["reason"] == "circuit_breaker_open"
    assert resp["path"] == "/foo"
    assert resp["retry_after_seconds"] == int(c.CB_COOLDOWN_SECONDS)


def test_half_open_admits_one_probe_then_re_arms(monkeypatch):
    """After cooldown elapses, exactly ONE caller passes; concurrent
    callers in the same instant see the breaker still open. Prevents
    a thundering herd hitting a recovering telemetry-API."""
    c = _import_client(monkeypatch)
    c._reset_breakers_for_tests()
    state = c._breaker_for("/foo")
    for _ in range(c.CB_FAILURE_THRESHOLD):
        c._record_failure(state, now=0.0)

    cooldown_end = c.CB_COOLDOWN_SECONDS + 0.1
    # First caller after cooldown: probe slot, sees breaker as not-open
    assert c._circuit_is_open(state, now=cooldown_end) is False
    # Immediate second caller: cooldown timer was reset by the probe,
    # so the breaker is open again
    assert c._circuit_is_open(state, now=cooldown_end + 0.001) is True
    # Far in the future: another probe slot opens
    assert c._circuit_is_open(state, now=cooldown_end + c.CB_COOLDOWN_SECONDS + 0.1) is False


def test_breakers_are_per_path(monkeypatch):
    """A failing /foo must not trip /bar."""
    c = _import_client(monkeypatch)
    c._reset_breakers_for_tests()
    foo = c._breaker_for("/foo")
    bar = c._breaker_for("/bar")
    for _ in range(c.CB_FAILURE_THRESHOLD):
        c._record_failure(foo, now=0.0)
    assert c._circuit_is_open(foo, now=0.0) is True
    assert c._circuit_is_open(bar, now=0.0) is False
