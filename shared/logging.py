"""Structured JSON logging with trace/tenant correlation (PR 30).

Both services — ``telemetry-api`` and ``mcp-server`` — call
``configure_logging(service_name)`` once at startup. After that every
``logging`` call emits one-line JSON carrying:

* ``ts`` (ISO 8601 UTC), ``level``, ``logger``, ``msg``, ``service``
* request-scoped fields when bound: ``tenant_id``, ``api_key_id``,
  ``request_id``, ``tool_name``
* OTel-derived ``trace_id`` / ``span_id`` when a span is current

Request-scoped fields are held in ``ContextVar``s bound by the
``log_context()`` context manager, typically from HTTP middleware. Tasks
spawned with ``asyncio.create_task`` inherit the current context, so
fire-and-forget audit writes log under the same tenant/request.

This module has no third-party dependencies. OTel span lookup is
opportunistic — missing OTel silently omits the trace fields rather
than failing the formatter.
"""
from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterator

_tenant_id_var: ContextVar[str | None] = ContextVar("flowmind_tenant_id", default=None)
_api_key_id_var: ContextVar[str | None] = ContextVar("flowmind_api_key_id", default=None)
_request_id_var: ContextVar[str | None] = ContextVar("flowmind_request_id", default=None)
_tool_name_var: ContextVar[str | None] = ContextVar("flowmind_tool_name", default=None)

_CONTEXT_VARS: dict[str, ContextVar[str | None]] = {
    "tenant_id": _tenant_id_var,
    "api_key_id": _api_key_id_var,
    "request_id": _request_id_var,
    "tool_name": _tool_name_var,
}

# Attribute names set by stdlib ``logging.LogRecord`` that we do not
# want to echo back into the JSON payload. ``extra={...}`` adds fields
# as attributes on the record; we emit anything not in this set.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


@contextmanager
def log_context(**fields: str | None) -> Iterator[None]:
    """Bind request-scoped fields for the duration of the block.

    Unknown keys are silently ignored — the set of recognized fields is
    defined by ``_CONTEXT_VARS`` so a typo here cannot invent a new log
    attribute. Binding ``None`` is a no-op so callers can pass optional
    values through without branching.
    """
    tokens = []
    for key, value in fields.items():
        var = _CONTEXT_VARS.get(key)
        if var is None or value is None:
            continue
        tokens.append((var, var.set(value)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def _span_fields() -> dict[str, str]:
    """Pull trace_id / span_id from the current OTel span, if any."""
    try:
        from opentelemetry import trace
    except ImportError:
        return {}
    span = trace.get_current_span()
    ctx = span.get_span_context() if span is not None else None
    if ctx is None or not ctx.is_valid:
        return {}
    return {
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
    }


class JsonFormatter(logging.Formatter):
    """One-line JSON per record. Stable field order for greppability."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service = service_name

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service,
            "msg": record.getMessage(),
        }
        for key, var in _CONTEXT_VARS.items():
            value = var.get()
            if value is not None:
                payload[key] = value
        payload.update(_span_fields())
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for attr, value in record.__dict__.items():
            if attr in _RESERVED_RECORD_ATTRS or attr.startswith("_"):
                continue
            if attr in payload:
                continue
            try:
                json.dumps(value, default=str)
            except (TypeError, ValueError):
                value = repr(value)
            payload[attr] = value
        return json.dumps(payload, default=str)


def configure_logging(
    service_name: str,
    *,
    level: str | int = "INFO",
    stream: Any = None,
) -> None:
    """Install the JSON formatter on the root logger.

    Idempotent: safe to call multiple times in tests. Replaces any
    existing handlers so a prior ``logging.basicConfig`` call cannot
    leak plain-text lines alongside JSON ones.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter(service_name))
    root.addHandler(handler)
    root.setLevel(level)
    # Quiet overly chatty libraries that otherwise double our line count.
    logging.getLogger("httpx").setLevel(max(root.level, logging.WARNING))


def current_context() -> dict[str, str]:
    """Snapshot of bound context fields — handy for tests and assertions."""
    return {
        key: value
        for key, value in ((key, var.get()) for key, var in _CONTEXT_VARS.items())
        if value is not None
    }
