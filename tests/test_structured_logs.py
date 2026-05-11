"""Unit tests for shared.logging (PR 30)."""
from __future__ import annotations

import io
import json
import logging

import pytest

from shared.logging import (
    JsonFormatter,
    configure_logging,
    current_context,
    log_context,
)


def _emit(service: str, level: int, msg: str, *, extra: dict | None = None) -> dict:
    """Run one log line through the formatter and return the decoded JSON."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter(service))
    logger = logging.getLogger(f"test.{service}.{id(buf)}")
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    logger.propagate = False
    logger.log(level, msg, extra=extra or {})
    return json.loads(buf.getvalue().strip())


def test_payload_has_core_fields():
    rec = _emit("telemetry-api", logging.INFO, "hello")
    assert rec["service"] == "telemetry-api"
    assert rec["level"] == "INFO"
    assert rec["msg"] == "hello"
    assert rec["logger"].startswith("test.telemetry-api")
    # ISO 8601 UTC: must end in +00:00 so downstream tooling trusts the zone.
    assert rec["ts"].endswith("+00:00")


def test_context_vars_are_emitted_only_while_bound():
    unbound = _emit("s", logging.INFO, "a")
    assert "tenant_id" not in unbound
    assert "request_id" not in unbound

    with log_context(tenant_id="t-1", request_id="r-1", api_key_id="k-1"):
        bound = _emit("s", logging.INFO, "b")

    assert bound["tenant_id"] == "t-1"
    assert bound["request_id"] == "r-1"
    assert bound["api_key_id"] == "k-1"

    after = _emit("s", logging.INFO, "c")
    assert "tenant_id" not in after


def test_log_context_ignores_none_and_unknown_fields():
    with log_context(tenant_id="t-1", request_id=None, bogus="ignored"):
        rec = _emit("s", logging.INFO, "x")
    assert rec["tenant_id"] == "t-1"
    assert "request_id" not in rec
    assert "bogus" not in rec


def test_nested_log_contexts_unwind_in_reverse_order():
    with log_context(tenant_id="outer"):
        assert current_context()["tenant_id"] == "outer"
        with log_context(tenant_id="inner"):
            assert current_context()["tenant_id"] == "inner"
        assert current_context()["tenant_id"] == "outer"
    assert current_context() == {}


def test_extra_fields_pass_through():
    # stdlib ``extra={...}`` attaches keys as LogRecord attributes; the
    # formatter should surface non-reserved ones in the JSON payload.
    rec = _emit("s", logging.INFO, "core", extra={"deploy_id": "abc", "worker": 7})
    assert rec["msg"] == "core"
    assert rec["deploy_id"] == "abc"
    assert rec["worker"] == 7


def test_payload_keys_are_not_shadowed_by_record_attributes():
    # If a log record carried an attribute colliding with one of our
    # payload keys (e.g. ``service``), the formatter must keep its own
    # value — otherwise extras could forge a service identity.
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter("expected-service"))
    logger = logging.getLogger(f"test.shadow.{id(buf)}")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # ``service`` is safe to pass via extra (not reserved by stdlib)
    logger.info("hi", extra={"service": "forged"})
    rec = json.loads(buf.getvalue().strip())
    assert rec["service"] == "expected-service"


def test_non_json_extra_values_are_coerced_not_dropped():
    class NotJsonable:
        def __repr__(self) -> str:
            return "NotJsonable-instance"

    rec = _emit("s", logging.INFO, "core", extra={"obj": NotJsonable()})
    assert "NotJsonable-instance" in rec["obj"]


def test_exception_is_captured_as_string():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter("s"))
    logger = logging.getLogger(f"test.exc.{id(buf)}")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("caught")
    rec = json.loads(buf.getvalue().strip())
    assert rec["msg"] == "caught"
    assert "ValueError: boom" in rec["exc"]


def test_configure_logging_is_idempotent():
    configure_logging("s", level="WARNING")
    root = logging.getLogger()
    first_handlers = list(root.handlers)
    configure_logging("s", level="INFO")
    second_handlers = list(root.handlers)
    assert len(second_handlers) == 1
    # Handlers are replaced, not stacked — otherwise tests + uvicorn double-log.
    assert second_handlers[0] is not first_handlers[0]


@pytest.fixture(autouse=True)
def _reset_logging():
    yield
    # Leave the root logger clean between tests.
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)
