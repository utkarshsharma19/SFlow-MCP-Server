"""Critical-anomaly webhook dispatcher (PR 30).

Background loop. Every tick:

1. For each active subscription, find anomaly_events whose
   ``last_seen_at`` is newer than the subscription's most recent
   delivery for that anomaly AND whose severity meets the subscription's
   ``severity_min``.
2. Sign the JSON payload with HMAC-SHA256 using the subscription's
   webhook secret (read out of ``encrypted_secrets``).
3. POST to ``target_url`` with the signature in ``X-FlowMind-Signature``.
4. Record the attempt in ``webhook_deliveries`` (one row per attempt,
   success or fail) — the unique key on (subscription_id, anomaly_id)
   means a recurrence won't double-page.

The dispatcher is best-effort: a subscription with too many consecutive
failures is suspended (``is_active=false``) so a black-hole URL doesn't
keep the loop churning indefinitely.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db import AsyncSessionLocal
from db.models import (
    AnomalyEvent,
    WebhookDelivery,
    WebhookSubscription,
)
from services.crypto import read_secret
from services.rls_session import bypass_rls

log = logging.getLogger(__name__)

DISPATCH_INTERVAL_SECONDS = int(os.getenv("WEBHOOK_DISPATCH_INTERVAL", "20"))
DISPATCH_TIMEOUT_SECONDS = float(os.getenv("WEBHOOK_DISPATCH_TIMEOUT", "5"))
MAX_CONSECUTIVE_FAILURES = int(os.getenv("WEBHOOK_MAX_FAILURES", "10"))
SIGNATURE_HEADER = "X-FlowMind-Signature"

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def sign_payload(secret: str, body: bytes) -> str:
    """Return ``sha256=<hex>`` HMAC matching GitHub's webhook convention."""
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def build_payload(anomaly: AnomalyEvent) -> dict:
    """Stable, public shape — receivers code against this."""
    return {
        "type": "flowmind.anomaly",
        "anomaly_id": str(anomaly.id),
        "tenant_id": str(anomaly.tenant_id),
        "anomaly_type": anomaly.anomaly_type,
        "severity": anomaly.severity,
        "scope": anomaly.scope,
        "summary": anomaly.summary,
        "first_seen_at": (
            anomaly.first_seen_at.isoformat() if anomaly.first_seen_at else None
        ),
        "last_seen_at": (
            anomaly.last_seen_at.isoformat() if anomaly.last_seen_at else None
        ),
        "occurrence_count": int(anomaly.occurrence_count),
        "metadata": anomaly.metadata_json,
    }


async def dispatch_once(
    db: AsyncSession,
    *,
    http_client: httpx.AsyncClient,
    now: datetime | None = None,
) -> list[dict]:
    """One pass: deliver every undelivered qualifying anomaly. Returns receipts."""
    now = now or datetime.now(timezone.utc)
    # Subscriptions are cross-tenant by definition — the loop runs above
    # tenancy. We re-bind tenant context for each delivery so any read
    # of tenant-scoped data inside the delivery (secrets, anomaly) still
    # passes RLS.
    receipts: list[dict] = []
    async with bypass_rls(db):
        subs = (
            await db.execute(
                select(WebhookSubscription).where(
                    WebhookSubscription.is_active.is_(True)
                )
            )
        ).scalars().all()

    for sub in subs:
        try:
            receipts.extend(
                await _deliver_for_subscription(db, http_client, sub, now)
            )
        except Exception as exc:  # noqa: BLE001 — one bad sub must not stall the loop
            log.exception("webhook subscription %s: dispatch error: %s", sub.id, exc)
    return receipts


async def _deliver_for_subscription(
    db: AsyncSession,
    http_client: httpx.AsyncClient,
    sub: WebhookSubscription,
    now: datetime,
) -> list[dict]:
    min_rank = SEVERITY_RANK.get(sub.severity_min, 4)
    allowed = [s for s, r in SEVERITY_RANK.items() if r >= min_rank]

    # Pull the secret in the subscription's own tenant context. If the
    # secret has been rotated out (None), suspend the subscription so an
    # operator notices instead of silently dropping pages.
    secret = await read_secret(
        db,
        tenant_id=str(sub.tenant_id),
        kind="webhook_secret",
        ref=sub.secret_ref,
    )
    if secret is None:
        log.warning(
            "subscription %s references missing secret %s — suspending",
            sub.id,
            sub.secret_ref,
        )
        await db.execute(
            update(WebhookSubscription)
            .where(WebhookSubscription.id == sub.id)
            .values(is_active=False, last_failure_at=now)
        )
        await db.commit()
        return []

    # Candidate anomalies: same tenant, severity meets bar, not yet
    # delivered. The not-exists clause is what makes the dispatcher
    # idempotent — re-running on the same data delivers nothing.
    candidates = (
        await db.execute(
            select(AnomalyEvent)
            .where(AnomalyEvent.tenant_id == sub.tenant_id)
            .where(AnomalyEvent.severity.in_(allowed))
            .where(AnomalyEvent.resolved_at.is_(None))
            .where(
                ~select(WebhookDelivery.id)
                .where(WebhookDelivery.subscription_id == sub.id)
                .where(WebhookDelivery.anomaly_id == AnomalyEvent.id)
                .exists()
            )
            .order_by(AnomalyEvent.last_seen_at.asc())
            .limit(50)
        )
    ).scalars().all()

    receipts: list[dict] = []
    for anomaly in candidates:
        receipt = await _post_one(
            db, http_client, sub, anomaly, secret.plaintext, now
        )
        receipts.append(receipt)
    return receipts


async def _post_one(
    db: AsyncSession,
    http_client: httpx.AsyncClient,
    sub: WebhookSubscription,
    anomaly: AnomalyEvent,
    secret_plaintext: str,
    now: datetime,
) -> dict:
    payload = build_payload(anomaly)
    body = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign_payload(secret_plaintext, body),
    }

    started = time.monotonic()
    status_code: int | None = None
    status = "failed"
    error: str | None = None
    try:
        resp = await http_client.post(
            sub.target_url,
            content=body,
            headers=headers,
            timeout=DISPATCH_TIMEOUT_SECONDS,
        )
        status_code = resp.status_code
        if 200 <= resp.status_code < 300:
            status = "ok"
        else:
            error = f"HTTP {resp.status_code}"
    except httpx.RequestError as exc:
        error = f"transport: {exc.__class__.__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001
        error = f"unexpected: {exc.__class__.__name__}: {exc}"
    duration_ms = int((time.monotonic() - started) * 1000)

    # Record the attempt — unique key on (sub, anomaly) makes the insert
    # idempotent. Use upsert-do-nothing so a race between two dispatcher
    # instances doesn't crash the loop.
    await db.execute(
        pg_insert(WebhookDelivery)
        .values(
            tenant_id=sub.tenant_id,
            subscription_id=sub.id,
            anomaly_id=anomaly.id,
            ts=now,
            status_code=status_code,
            status=status,
            error=(error[:512] if error else None),
            duration_ms=duration_ms,
        )
        .on_conflict_do_nothing(constraint="uq_webhook_delivery_per_anomaly")
    )
    # Update subscription health counters in the same transaction.
    if status == "ok":
        await db.execute(
            update(WebhookSubscription)
            .where(WebhookSubscription.id == sub.id)
            .values(last_success_at=now, consecutive_failures=0)
        )
    else:
        new_failures = sub.consecutive_failures + 1
        values: dict = {
            "last_failure_at": now,
            "consecutive_failures": new_failures,
        }
        if new_failures >= MAX_CONSECUTIVE_FAILURES:
            values["is_active"] = False
            log.warning(
                "subscription %s suspended after %d consecutive failures",
                sub.id,
                new_failures,
            )
        await db.execute(
            update(WebhookSubscription)
            .where(WebhookSubscription.id == sub.id)
            .values(**values)
        )
    await db.commit()

    return {
        "subscription_id": str(sub.id),
        "anomaly_id": str(anomaly.id),
        "status": status,
        "status_code": status_code,
        "duration_ms": duration_ms,
    }


async def webhook_dispatcher_loop() -> None:
    """Long-running loop entered from main.py's lifespan."""
    if os.getenv("FLOWMIND_DATA_KEY") is None:
        log.warning(
            "FLOWMIND_DATA_KEY unset — webhook dispatcher disabled. "
            "Set the data key to enable signed delivery."
        )
        return
    async with httpx.AsyncClient() as http_client:
        while True:
            try:
                async with AsyncSessionLocal() as db:
                    await dispatch_once(db, http_client=http_client)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("webhook dispatcher tick failed")
            await asyncio.sleep(DISPATCH_INTERVAL_SECONDS)
