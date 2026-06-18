"""Transport-auth middleware tests (PR 31)."""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER_DIR = PROJECT_ROOT / "apps" / "mcp-server"


def _import_module(monkeypatch):
    for name in list(sys.modules):
        if name.startswith("middleware") or name in {"middleware"}:
            sys.modules.pop(name)
    monkeypatch.syspath_prepend(str(MCP_SERVER_DIR))
    return importlib.import_module("middleware.transport_auth")


class _Recorder:
    """Captures ASGI ``send`` calls + tracks whether the wrapped app
    was reached. ``__call__`` is the receive coroutine."""

    def __init__(self):
        self.events = []
        self.received_messages = [{"type": "http.request", "body": b"", "more_body": False}]

    async def __call__(self):
        if self.received_messages:
            return self.received_messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(self, message):
        self.events.append(message)

    @property
    def status(self) -> int | None:
        for e in self.events:
            if e["type"] == "http.response.start":
                return e["status"]
        return None

    @property
    def body(self) -> bytes:
        return b"".join(
            e.get("body", b"") for e in self.events if e["type"] == "http.response.body"
        )


def _http_scope(headers=(), path="/mcp", query=b""):
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": list(headers),
    }


def _make_inner_app():
    """A passthrough downstream ASGI app: returns 200 with marker body."""
    inner_calls = {"n": 0}

    async def inner_app(scope, receive, send):
        inner_calls["n"] += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return inner_app, inner_calls


def test_disabled_when_no_key(monkeypatch):
    mod = _import_module(monkeypatch)
    inner, calls = _make_inner_app()
    mw = mod.TransportAuthMiddleware(inner, key=None)

    rec = _Recorder()
    asyncio.run(mw(_http_scope(), rec, rec.send))

    assert calls["n"] == 1
    assert rec.status == 200
    assert rec.body == b"ok"


def test_rejects_missing_key(monkeypatch):
    mod = _import_module(monkeypatch)
    inner, calls = _make_inner_app()
    mw = mod.TransportAuthMiddleware(inner, key="secret-1")

    rec = _Recorder()
    asyncio.run(mw(_http_scope(), rec, rec.send))

    assert calls["n"] == 0
    assert rec.status == 401
    assert b"unauthorized" in rec.body
    # WWW-Authenticate header hints clients which scheme to use
    start = next(e for e in rec.events if e["type"] == "http.response.start")
    assert any(h[0] == b"www-authenticate" for h in start["headers"])


def test_accepts_header_key(monkeypatch):
    mod = _import_module(monkeypatch)
    inner, calls = _make_inner_app()
    mw = mod.TransportAuthMiddleware(inner, key="secret-1")

    rec = _Recorder()
    asyncio.run(
        mw(
            _http_scope(headers=[(b"x-mcp-key", b"secret-1")]),
            rec,
            rec.send,
        )
    )
    assert calls["n"] == 1
    assert rec.status == 200


def test_header_lookup_is_case_insensitive(monkeypatch):
    mod = _import_module(monkeypatch)
    inner, calls = _make_inner_app()
    mw = mod.TransportAuthMiddleware(inner, key="secret-1")

    rec = _Recorder()
    asyncio.run(
        mw(
            _http_scope(headers=[(b"X-MCP-Key", b"secret-1")]),
            rec,
            rec.send,
        )
    )
    assert calls["n"] == 1
    assert rec.status == 200


def test_rejects_wrong_key(monkeypatch):
    mod = _import_module(monkeypatch)
    inner, calls = _make_inner_app()
    mw = mod.TransportAuthMiddleware(inner, key="secret-1")

    rec = _Recorder()
    asyncio.run(
        mw(
            _http_scope(headers=[(b"x-mcp-key", b"WRONG")]),
            rec,
            rec.send,
        )
    )
    assert calls["n"] == 0
    assert rec.status == 401


def test_accepts_query_string_fallback(monkeypatch):
    """Some MCP clients (Inspector, embedded browser tabs) can't set
    custom headers — let them pass the key in the URL instead."""
    mod = _import_module(monkeypatch)
    inner, calls = _make_inner_app()
    mw = mod.TransportAuthMiddleware(inner, key="secret-1")

    rec = _Recorder()
    asyncio.run(
        mw(
            _http_scope(query=b"mcp_key=secret-1&foo=bar"),
            rec,
            rec.send,
        )
    )
    assert calls["n"] == 1
    assert rec.status == 200


def test_lifespan_event_passes_through(monkeypatch):
    """ASGI lifespan must not be auth-gated — would deadlock startup."""
    mod = _import_module(monkeypatch)
    inner, calls = _make_inner_app()
    mw = mod.TransportAuthMiddleware(inner, key="secret-1")

    scope = {"type": "lifespan"}
    rec = _Recorder()
    asyncio.run(mw(scope, rec, rec.send))
    assert calls["n"] == 1


def test_websocket_reject_emits_close_not_http_response(monkeypatch):
    """An unauthorized websocket scope must NOT receive http.response.*
    messages — those crash Starlette's WS protocol layer. We expect a
    single websocket.close with code 1008 (policy violation)."""
    mod = _import_module(monkeypatch)
    inner, calls = _make_inner_app()
    mw = mod.TransportAuthMiddleware(inner, key="secret-1")

    ws_scope = {
        "type": "websocket",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "headers": [],
    }
    rec = _Recorder()
    asyncio.run(mw(ws_scope, rec, rec.send))

    assert calls["n"] == 0
    assert len(rec.events) == 1
    msg = rec.events[0]
    assert msg["type"] == "websocket.close"
    assert msg["code"] == 1008
    # No http.response anywhere
    for e in rec.events:
        assert not e["type"].startswith("http.response")


def test_websocket_passes_through_with_valid_key(monkeypatch):
    mod = _import_module(monkeypatch)
    inner, calls = _make_inner_app()
    mw = mod.TransportAuthMiddleware(inner, key="secret-1")

    ws_scope = {
        "type": "websocket",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "headers": [(b"x-mcp-key", b"secret-1")],
    }
    rec = _Recorder()
    asyncio.run(mw(ws_scope, rec, rec.send))
    assert calls["n"] == 1


def test_get_transport_key_reads_env(monkeypatch):
    mod = _import_module(monkeypatch)
    monkeypatch.delenv("MCP_TRANSPORT_KEY", raising=False)
    assert mod.get_transport_key() is None
    monkeypatch.setenv("MCP_TRANSPORT_KEY", "xyz")
    assert mod.get_transport_key() == "xyz"
    monkeypatch.setenv("MCP_TRANSPORT_KEY", "")
    assert mod.get_transport_key() is None
