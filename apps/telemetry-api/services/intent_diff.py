"""Intent-vs-state drift detection (PR 29).

Compares the cached operator/Verity intent (``device_intent`` and
``bgp_intent``) against the latest observed state in
``device_state_minute`` / ``bgp_session_minute``. The output is shaped
so an LLM tool consumer can immediately tell the operator *what* drifted
and *which way* — intent says UP, reality says DOWN — without doing the
set arithmetic itself.

Three classes of finding:

* **mismatch**         — both sides know about the interface/peer but
  disagree on a specific field. Most common drift case.
* **missing_state**    — intent declares it; observed state has no row
  for it inside the freshness window. Common when a port was never
  brought up or gNMI lost the target.
* **unexpected_state** — observed state has it; intent does not. Either
  someone added config out-of-band or the intent cache hasn't been
  synced.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    BGPIntent,
    BGPSessionMinute,
    DeviceIntent,
    DeviceStateMinute,
)

# A state row older than this is treated as "no observation" for diff
# purposes. Otherwise a flapping gNMI feed would silently mask drift.
STATE_FRESHNESS_MINUTES = 10


@dataclass(frozen=True)
class DiffCounts:
    mismatches: int
    missing_state: int
    unexpected_state: int

    @property
    def total(self) -> int:
        return self.mismatches + self.missing_state + self.unexpected_state


async def diff_intent_vs_state(
    db: AsyncSession,
    tenant_id: str,
    device: str | None = None,
) -> dict:
    """Return a per-tenant (or per-device) drift report.

    If ``device`` is omitted the diff runs across every device with
    either intent or recent state in the tenant. Results are bounded
    only by tenant — drift accumulates across days so a small window
    here would hide real misconfig.
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=STATE_FRESHNESS_MINUTES)

    iface_findings = await _diff_interfaces(db, tenant_id, since, device)
    bgp_findings = await _diff_bgp(db, tenant_id, since, device)

    iface_counts = _count(iface_findings)
    bgp_counts = _count(bgp_findings)
    total = iface_counts.total + bgp_counts.total
    severity = _severity(iface_findings, bgp_findings)

    return {
        "device": device,
        "interfaces": {
            "findings": iface_findings,
            "counts": iface_counts.__dict__,
        },
        "bgp": {
            "findings": bgp_findings,
            "counts": bgp_counts.__dict__,
        },
        "total_drift_count": total,
        "severity": severity,
        "confidence_note": (
            "Intent is taken from the cached ``device_intent`` / "
            "``bgp_intent`` tables (populated by Verity sync or the seed "
            "CLI). State is the latest gNMI/OpenConfig sample within the "
            f"last {STATE_FRESHNESS_MINUTES} minutes — older rows are "
            "treated as no observation. Diffs ignore NULL intent fields."
        ),
    }


# ---------------------------------------------------------------------------
# Interface diff
# ---------------------------------------------------------------------------

async def _diff_interfaces(
    db: AsyncSession,
    tenant_id: str,
    since: datetime,
    device: str | None,
) -> list[dict]:
    intent_q = select(DeviceIntent).where(DeviceIntent.tenant_id == tenant_id)
    if device is not None:
        intent_q = intent_q.where(DeviceIntent.device == device)
    intent_rows = (await db.execute(intent_q)).scalars().all()
    intent_by_key = {(r.device, r.interface): r for r in intent_rows}

    state_by_key = await _latest_interface_state(db, tenant_id, since, device)

    findings: list[dict] = []

    for key, intent in intent_by_key.items():
        state = state_by_key.get(key)
        if state is None:
            findings.append(
                {
                    "kind": "missing_state",
                    "device": intent.device,
                    "interface": intent.interface,
                    "intent": _intent_summary(intent),
                    "state": None,
                    "diffs": ["no recent state observation for declared interface"],
                }
            )
            continue
        diffs = _compare_interface(intent, state)
        if diffs:
            findings.append(
                {
                    "kind": "mismatch",
                    "device": intent.device,
                    "interface": intent.interface,
                    "intent": _intent_summary(intent),
                    "state": state,
                    "diffs": diffs,
                }
            )

    for key, state in state_by_key.items():
        if key in intent_by_key:
            continue
        findings.append(
            {
                "kind": "unexpected_state",
                "device": state["device"],
                "interface": state["interface"],
                "intent": None,
                "state": state,
                "diffs": ["interface present in state but not in intent"],
            }
        )

    return findings


async def _latest_interface_state(
    db: AsyncSession,
    tenant_id: str,
    since: datetime,
    device: str | None,
) -> dict[tuple[str, str], dict]:
    """One row per (device, interface) — the freshest in the window."""
    latest_ts_q = (
        select(
            DeviceStateMinute.device,
            DeviceStateMinute.interface,
            func.max(DeviceStateMinute.ts_bucket).label("ts"),
        )
        .where(DeviceStateMinute.tenant_id == tenant_id)
        .where(DeviceStateMinute.ts_bucket >= since)
        .group_by(DeviceStateMinute.device, DeviceStateMinute.interface)
    )
    if device is not None:
        latest_ts_q = latest_ts_q.where(DeviceStateMinute.device == device)
    latest_ts_q = latest_ts_q.subquery()

    q = (
        select(DeviceStateMinute)
        .join(
            latest_ts_q,
            (DeviceStateMinute.device == latest_ts_q.c.device)
            & (DeviceStateMinute.interface == latest_ts_q.c.interface)
            & (DeviceStateMinute.ts_bucket == latest_ts_q.c.ts),
        )
        .where(DeviceStateMinute.tenant_id == tenant_id)
    )
    rows = (await db.execute(q)).scalars().all()
    return {
        (r.device, r.interface): {
            "device": r.device,
            "interface": r.interface,
            "admin_status": r.admin_status,
            "oper_status": r.oper_status,
            "speed_bps": int(r.speed_bps) if r.speed_bps is not None else None,
            "mtu": int(r.mtu) if r.mtu is not None else None,
            "description": r.description,
            "ts_bucket": r.ts_bucket.isoformat(),
        }
        for r in rows
    }


def _compare_interface(intent: DeviceIntent, state: dict) -> list[str]:
    """Return one short string per drifted field; NULL intent is ignored."""
    diffs: list[str] = []
    pairs = [
        ("expected_admin_status", "admin_status", "admin"),
        ("expected_oper_status", "oper_status", "oper"),
        ("expected_speed_bps", "speed_bps", "speed_bps"),
        ("expected_mtu", "mtu", "mtu"),
        ("expected_description", "description", "description"),
    ]
    for intent_attr, state_key, label in pairs:
        expected = getattr(intent, intent_attr)
        if expected is None:
            continue
        observed = state.get(state_key)
        if observed != expected:
            diffs.append(f"{label}: intent={expected!r} state={observed!r}")
    return diffs


def _intent_summary(intent: DeviceIntent) -> dict:
    return {
        "expected_admin_status": intent.expected_admin_status,
        "expected_oper_status": intent.expected_oper_status,
        "expected_speed_bps": intent.expected_speed_bps,
        "expected_mtu": intent.expected_mtu,
        "expected_description": intent.expected_description,
        "source": intent.source,
    }


# ---------------------------------------------------------------------------
# BGP diff
# ---------------------------------------------------------------------------

async def _diff_bgp(
    db: AsyncSession,
    tenant_id: str,
    since: datetime,
    device: str | None,
) -> list[dict]:
    intent_q = select(BGPIntent).where(BGPIntent.tenant_id == tenant_id)
    if device is not None:
        intent_q = intent_q.where(BGPIntent.device == device)
    intent_rows = (await db.execute(intent_q)).scalars().all()
    intent_by_key = {(r.device, r.peer_address): r for r in intent_rows}

    state_by_key = await _latest_bgp_state(db, tenant_id, since, device)

    findings: list[dict] = []

    for key, intent in intent_by_key.items():
        state = state_by_key.get(key)
        if state is None:
            findings.append(
                {
                    "kind": "missing_state",
                    "device": intent.device,
                    "peer_address": intent.peer_address,
                    "intent": _bgp_intent_summary(intent),
                    "state": None,
                    "diffs": ["no recent BGP state for declared peer"],
                }
            )
            continue
        diffs = _compare_bgp(intent, state)
        if diffs:
            findings.append(
                {
                    "kind": "mismatch",
                    "device": intent.device,
                    "peer_address": intent.peer_address,
                    "intent": _bgp_intent_summary(intent),
                    "state": state,
                    "diffs": diffs,
                }
            )

    for key, state in state_by_key.items():
        if key in intent_by_key:
            continue
        findings.append(
            {
                "kind": "unexpected_state",
                "device": state["device"],
                "peer_address": state["peer_address"],
                "intent": None,
                "state": state,
                "diffs": ["BGP peer present in state but not in intent"],
            }
        )

    return findings


async def _latest_bgp_state(
    db: AsyncSession,
    tenant_id: str,
    since: datetime,
    device: str | None,
) -> dict[tuple[str, str], dict]:
    latest_ts_q = (
        select(
            BGPSessionMinute.device,
            BGPSessionMinute.peer_address,
            func.max(BGPSessionMinute.ts_bucket).label("ts"),
        )
        .where(BGPSessionMinute.tenant_id == tenant_id)
        .where(BGPSessionMinute.ts_bucket >= since)
        .group_by(BGPSessionMinute.device, BGPSessionMinute.peer_address)
    )
    if device is not None:
        latest_ts_q = latest_ts_q.where(BGPSessionMinute.device == device)
    latest_ts_q = latest_ts_q.subquery()

    q = (
        select(BGPSessionMinute)
        .join(
            latest_ts_q,
            (BGPSessionMinute.device == latest_ts_q.c.device)
            & (BGPSessionMinute.peer_address == latest_ts_q.c.peer_address)
            & (BGPSessionMinute.ts_bucket == latest_ts_q.c.ts),
        )
        .where(BGPSessionMinute.tenant_id == tenant_id)
    )
    rows = (await db.execute(q)).scalars().all()
    return {
        (r.device, r.peer_address): {
            "device": r.device,
            "peer_address": r.peer_address,
            "peer_as": int(r.peer_as) if r.peer_as is not None else None,
            "session_state": r.session_state,
            "uptime_seconds": (
                int(r.uptime_seconds) if r.uptime_seconds is not None else None
            ),
            "last_error": r.last_error,
            "ts_bucket": r.ts_bucket.isoformat(),
        }
        for r in rows
    }


def _compare_bgp(intent: BGPIntent, state: dict) -> list[str]:
    diffs: list[str] = []
    if (
        intent.expected_peer_as is not None
        and state.get("peer_as") != intent.expected_peer_as
    ):
        diffs.append(
            f"peer_as: intent={intent.expected_peer_as} "
            f"state={state.get('peer_as')}"
        )
    if (
        intent.expected_session_state is not None
        and state.get("session_state") != intent.expected_session_state
    ):
        diffs.append(
            f"session_state: intent={intent.expected_session_state!r} "
            f"state={state.get('session_state')!r}"
        )
    return diffs


def _bgp_intent_summary(intent: BGPIntent) -> dict:
    return {
        "expected_peer_as": intent.expected_peer_as,
        "expected_session_state": intent.expected_session_state,
        "source": intent.source,
    }


# ---------------------------------------------------------------------------
# Severity + counts
# ---------------------------------------------------------------------------

def _count(findings: list[dict]) -> DiffCounts:
    mismatch = sum(1 for f in findings if f["kind"] == "mismatch")
    missing = sum(1 for f in findings if f["kind"] == "missing_state")
    unexpected = sum(1 for f in findings if f["kind"] == "unexpected_state")
    return DiffCounts(mismatch, missing, unexpected)


def _severity(iface: list[dict], bgp: list[dict]) -> str:
    """Drift severity reflects how badly reality disagrees with intent.

    Any declared interface that's DOWN when intent says UP is critical
    — that's literally a broken link the operator declared as working.
    A BGP peer declared ESTABLISHED that isn't is also critical.
    Mismatched MTU/speed/description is medium drift.
    Unexpected_state alone is low — config exists that intent doesn't
    know about, but the fabric still works.
    """
    for f in iface:
        if f["kind"] != "mismatch":
            continue
        intent = f["intent"] or {}
        state = f["state"] or {}
        if (
            intent.get("expected_oper_status") == "UP"
            and state.get("oper_status") != "UP"
        ):
            return "critical"
        if (
            intent.get("expected_admin_status") == "UP"
            and state.get("admin_status") != "UP"
        ):
            return "critical"
    for f in bgp:
        if f["kind"] != "mismatch":
            continue
        intent = f["intent"] or {}
        state = f["state"] or {}
        if (
            intent.get("expected_session_state") == "ESTABLISHED"
            and state.get("session_state") != "ESTABLISHED"
        ):
            return "critical"

    any_missing = any(
        f["kind"] == "missing_state" for f in iface + bgp
    )
    any_mismatch = any(f["kind"] == "mismatch" for f in iface + bgp)
    if any_mismatch or any_missing:
        return "medium"
    if iface or bgp:
        return "low"
    return "low"
