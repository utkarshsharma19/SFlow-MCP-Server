"""Token-quota dataclass + decision shape tests (PR 30)."""
from __future__ import annotations

from services.tool_audit import QuotaDecision


def test_quota_decision_to_response_includes_token_fields():
    """Chat gateway depends on these keys — pin them in a test."""
    d = QuotaDecision(
        allowed=True,
        calls_this_period=5,
        call_limit=100,
        bytes_out_this_period=1_000,
        byte_limit=None,
        llm_tokens_this_period=4_200,
        token_limit=100_000,
        reason="ok",
    )
    resp = d.to_response()
    assert resp["llm_tokens_this_period"] == 4_200
    assert resp["token_limit"] == 100_000
    assert resp["allowed"] is True


def test_quota_decision_disallows_over_token_limit_with_clear_reason():
    d = QuotaDecision(
        allowed=False,
        calls_this_period=5,
        call_limit=100,
        bytes_out_this_period=1_000,
        byte_limit=None,
        llm_tokens_this_period=200_000,
        token_limit=100_000,
        reason="token_limit_exceeded",
    )
    assert d.allowed is False
    assert d.reason == "token_limit_exceeded"


def test_quota_decision_unlimited_token_limit_is_none():
    """NULL in the DB → None in Python — must not get coerced to 0."""
    d = QuotaDecision(
        allowed=True,
        calls_this_period=0,
        call_limit=None,
        bytes_out_this_period=0,
        byte_limit=None,
        llm_tokens_this_period=10_000,
        token_limit=None,
        reason="ok",
    )
    assert d.token_limit is None
    assert d.to_response()["token_limit"] is None
