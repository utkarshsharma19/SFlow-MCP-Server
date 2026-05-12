"""LLDP neighbors query service (PR 30).

Two reads:

* ``get_device_neighbors(device)`` — every adjacency on one device. The
  chatbot's "what's plugged in?" answer.
* ``upsert_neighbor_observation`` — call site for the gNMI ingest path
  (and the seed CLI) to refresh ``last_seen_at`` without churning
  ``first_seen_at``.

Both run under the same RLS policy as the rest of the device-state
tables, so a viewer key in tenant A can never see tenant B's
adjacencies even by guessing chassis IDs.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import LLDPNeighbor

STALE_NEIGHBOR_HOURS = 24


async def get_device_neighbors(
    db: AsyncSession,
    tenant_id: str,
    device: str,
) -> dict:
    q = (
        select(LLDPNeighbor)
        .where(LLDPNeighbor.tenant_id == tenant_id)
        .where(LLDPNeighbor.device == device)
        .order_by(LLDPNeighbor.interface)
    )
    rows = (await db.execute(q)).scalars().all()
    now = datetime.now(timezone.utc)
    neighbors = [
        {
            "interface": r.interface,
            "neighbor_chassis_id": r.neighbor_chassis_id,
            "neighbor_system_name": r.neighbor_system_name,
            "neighbor_port_id": r.neighbor_port_id,
            "neighbor_port_description": r.neighbor_port_description,
            "neighbor_management_address": r.neighbor_management_address,
            "first_seen_at": r.first_seen_at.isoformat(),
            "last_seen_at": r.last_seen_at.isoformat(),
            "is_stale": _is_stale(now, r.last_seen_at),
        }
        for r in rows
    ]
    return {
        "device": device,
        "neighbors": neighbors,
        "neighbor_count": len(neighbors),
        "confidence_note": _confidence_note(neighbors),
    }


def _is_stale(now: datetime, last_seen: datetime) -> bool:
    """A neighbor that hasn't refreshed inside the stale window may be gone.

    We don't *delete* stale rows because cable moves should appear in
    the response as a transition ("X was plugged here yesterday, gone
    today") rather than vanish silently. The chatbot can quote that.
    """
    return (now - last_seen).total_seconds() >= STALE_NEIGHBOR_HOURS * 3600


def _confidence_note(neighbors: list[dict]) -> str:
    if not neighbors:
        return (
            "No LLDP neighbors recorded for this device. Either LLDP isn't "
            "enabled on the device, gNMI isn't subscribed to the LLDP path, "
            "or the device has no L2 adjacencies."
        )
    stale = sum(1 for n in neighbors if n["is_stale"])
    note = f"{len(neighbors)} LLDP adjacencies reported from gNMI."
    if stale:
        note += (
            f" {stale} entry/entries have not refreshed in "
            f"{STALE_NEIGHBOR_HOURS}h — the cable may have been pulled."
        )
    return note


async def upsert_neighbor_observation(
    db: AsyncSession,
    *,
    tenant_id: str,
    device: str,
    interface: str,
    neighbor_chassis_id: str,
    neighbor_system_name: str | None,
    neighbor_port_id: str | None,
    neighbor_port_description: str | None,
    neighbor_management_address: str | None,
    now: datetime | None = None,
) -> None:
    """Refresh last_seen_at without bumping first_seen_at."""
    now = now or datetime.now(timezone.utc)
    stmt = (
        pg_insert(LLDPNeighbor)
        .values(
            tenant_id=tenant_id,
            device=device,
            interface=interface,
            neighbor_chassis_id=neighbor_chassis_id,
            neighbor_system_name=neighbor_system_name,
            neighbor_port_id=neighbor_port_id,
            neighbor_port_description=neighbor_port_description,
            neighbor_management_address=neighbor_management_address,
            first_seen_at=now,
            last_seen_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_lldp_neighbor",
            set_={
                "neighbor_system_name": neighbor_system_name,
                "neighbor_port_id": neighbor_port_id,
                "neighbor_port_description": neighbor_port_description,
                "neighbor_management_address": neighbor_management_address,
                "last_seen_at": now,
            },
        )
    )
    await db.execute(stmt)
    await db.commit()
