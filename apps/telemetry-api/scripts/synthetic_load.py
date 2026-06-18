"""Synthetic flow + counter generator for testbed demos (PR 31).

Writes plausible rows directly into ``flow_summary_minute`` and
``interface_utilization_minute`` so the chatbot has something to look
at without a live sFlow stream. Also seeds a few LLDP neighbors so
``find_path`` can demonstrate ordered output.

This is *not* sFlow-protocol-compatible. It bypasses sFlow-RT and
writes pre-bucketed rows. Production deployments must use the real
sFlow agent → sFlow-RT → telemetry-api path; this script exists for
demos, integration tests, and chatbot-side development when no fabric
is available.

Usage:
    docker compose exec telemetry-api \\
        python -m scripts.synthetic_load \\
            --tenant-slug default \\
            --minutes 30 \\
            --switches leaf1 spine1 leaf2

The defaults model a small spine-leaf fabric with one hot link so
``get_fabric_health`` returns a non-trivial severity and
``get_link_history`` has a visible spike to plot.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db import AsyncSessionLocal
from db.models import (
    FlowSummaryMinute,
    InterfaceUtilizationMinute,
    LLDPNeighbor,
    Tenant,
)
from services.rls_session import set_tenant


# Fabric model: two leaves and one spine. Each leaf has one customer
# port (Eth0) and one uplink to the spine (Eth1). The spine has an
# uplink to each leaf (Eth0 → leaf1, Eth1 → leaf2). This is enough
# topology for find_path to detect a chain.
DEFAULT_FABRIC = {
    "leaf1": [
        {"interface": "Ethernet0", "speed_bps": 25_000_000_000, "role": "edge"},
        {"interface": "Ethernet1", "speed_bps": 100_000_000_000, "role": "uplink"},
    ],
    "spine1": [
        {"interface": "Ethernet0", "speed_bps": 100_000_000_000, "role": "downlink"},
        {"interface": "Ethernet1", "speed_bps": 100_000_000_000, "role": "downlink"},
    ],
    "leaf2": [
        {"interface": "Ethernet0", "speed_bps": 25_000_000_000, "role": "edge"},
        {"interface": "Ethernet1", "speed_bps": 100_000_000_000, "role": "uplink"},
    ],
}

# (a, a_iface) <→ (b, b_iface) edges, one row per side.
DEFAULT_LLDP = [
    ("leaf1", "Ethernet1", "spine1", "Ethernet0"),
    ("spine1", "Ethernet0", "leaf1", "Ethernet1"),
    ("leaf2", "Ethernet1", "spine1", "Ethernet1"),
    ("spine1", "Ethernet1", "leaf2", "Ethernet1"),
]

# A few synthetic talkers. The first pair drives the "hot link" so the
# chatbot has a clear story.
DEFAULT_TALKERS = [
    ("10.1.1.5", "10.2.2.7", 6, "high"),    # TCP, drives the hot link
    ("10.1.1.6", "10.2.2.8", 17, "medium"),  # UDP
    ("10.1.1.7", "10.3.3.9", 6, "low"),
]


def _bucket(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


async def _resolve_tenant(slug: str) -> str:
    async with AsyncSessionLocal() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == slug))
        ).scalar_one_or_none()
        if tenant is None:
            print(f"error: tenant '{slug}' not found", file=sys.stderr)
            sys.exit(2)
        return str(tenant.id)


async def _seed_lldp(tenant_id: str, edges) -> int:
    now = datetime.now(timezone.utc)
    n = 0
    async with AsyncSessionLocal() as session:
        await set_tenant(session, tenant_id)
        for a, a_iface, b, _b_iface in edges:
            chassis_id = f"chassis-{b}"
            stmt = (
                pg_insert(LLDPNeighbor)
                .values(
                    tenant_id=tenant_id,
                    device=a,
                    interface=a_iface,
                    neighbor_chassis_id=chassis_id,
                    neighbor_system_name=b,
                    neighbor_port_id=_b_iface,
                    neighbor_port_description=f"to-{a}",
                    neighbor_management_address=f"10.0.0.{n + 1}",
                    first_seen_at=now,
                    last_seen_at=now,
                )
                .on_conflict_do_update(
                    constraint="uq_lldp_neighbor",
                    set_={"last_seen_at": now},
                )
            )
            await session.execute(stmt)
            n += 1
        await session.commit()
    return n


async def _generate_flows_and_util(
    tenant_id: str,
    fabric: dict,
    talkers,
    minutes: int,
    sampling_rate: int,
) -> dict:
    """Write ``minutes`` of synthetic rows ending at the current bucket.

    Returns counters for the operator's status print.
    """
    now = _bucket(datetime.now(timezone.utc))
    summary = {"flow_rows": 0, "util_rows": 0}

    async with AsyncSessionLocal() as session:
        await set_tenant(session, tenant_id)

        for m in range(minutes):
            ts = now - timedelta(minutes=(minutes - 1 - m))

            # Build a per-(device, interface) byte total. Each talker
            # rides every uplink on its path, biased so the "high"
            # talker dominates a particular link.
            link_bytes: dict[tuple[str, str], int] = {}

            for src, dst, proto, intensity in talkers:
                base = {"high": 1_500_000_000, "medium": 400_000_000, "low": 80_000_000}[intensity]
                jitter = random.uniform(0.7, 1.3)
                bytes_total = int(base * jitter)
                packets_total = bytes_total // 1200

                for dev, ifaces in fabric.items():
                    for iface in ifaces:
                        # Hot link: leaf1 → spine1 uplink for the "high" talker
                        if intensity == "high" and not (
                            (dev == "leaf1" and iface["interface"] == "Ethernet1")
                            or (dev == "spine1" and iface["interface"] == "Ethernet0")
                            or (dev == "leaf2" and iface["interface"] == "Ethernet0")
                        ):
                            continue
                        # Every other talker rides every uplink, half-volume
                        # so the high talker visibly dominates.
                        share = 1.0 if intensity == "high" else 0.4
                        link_bytes[(dev, iface["interface"])] = (
                            link_bytes.get((dev, iface["interface"]), 0)
                            + int(bytes_total * share)
                        )

                # Flow rows are written per-(device, ingress_iface) so
                # the find_path GROUP BY produces real hops.
                for dev, ifaces in fabric.items():
                    for iface in ifaces:
                        if intensity == "high" and not (
                            (dev == "leaf1" and iface["interface"] == "Ethernet1")
                            or (dev == "spine1" and iface["interface"] == "Ethernet0")
                            or (dev == "leaf2" and iface["interface"] == "Ethernet0")
                        ):
                            continue
                        share = 1.0 if intensity == "high" else 0.4
                        session.add(
                            FlowSummaryMinute(
                                tenant_id=tenant_id,
                                ts_bucket=ts,
                                device=dev,
                                interface=iface["interface"],
                                src_ip=src,
                                dst_ip=dst,
                                protocol=proto,
                                bytes_estimated=int(bytes_total * share),
                                packets_estimated=int(packets_total * share),
                                sampling_rate=sampling_rate,
                            )
                        )
                        summary["flow_rows"] += 1

            # Convert per-link byte totals into utilization rows. The
            # spine downlinks see in+out symmetric; leaves see asymmetric
            # (in on Eth0 customer, out on Eth1 uplink).
            for dev, ifaces in fabric.items():
                for iface in ifaces:
                    key = (dev, iface["interface"])
                    bytes_in_minute = link_bytes.get(key, 0)
                    bps = (bytes_in_minute * 8) / 60
                    util = min(99.5, 100.0 * bps / iface["speed_bps"])
                    in_util = round(util * random.uniform(0.85, 1.0), 2)
                    out_util = round(util * random.uniform(0.85, 1.0), 2)
                    session.add(
                        InterfaceUtilizationMinute(
                            tenant_id=tenant_id,
                            ts_bucket=ts,
                            device=dev,
                            interface=iface["interface"],
                            in_bps=int(bps * 0.95),
                            out_bps=int(bps * 0.9),
                            in_util_pct=in_util,
                            out_util_pct=out_util,
                            error_count=(2 if intensity == "high" and util > 80 else 0),
                        )
                    )
                    summary["util_rows"] += 1

        await session.commit()
    return summary


async def main_async(args) -> None:
    tenant_id = await _resolve_tenant(args.tenant_slug)
    print(f"Synthetic load → tenant_id={tenant_id}, {args.minutes} min")

    fabric = DEFAULT_FABRIC if not args.switches else {
        s: DEFAULT_FABRIC.get(s, [
            {"interface": "Ethernet0", "speed_bps": 25_000_000_000, "role": "edge"},
            {"interface": "Ethernet1", "speed_bps": 100_000_000_000, "role": "uplink"},
        ])
        for s in args.switches
    }

    lldp_n = await _seed_lldp(tenant_id, DEFAULT_LLDP)
    print(f"  LLDP edges written: {lldp_n}")

    summary = await _generate_flows_and_util(
        tenant_id=tenant_id,
        fabric=fabric,
        talkers=DEFAULT_TALKERS,
        minutes=args.minutes,
        sampling_rate=args.sampling_rate,
    )
    print(
        f"  flow rows written: {summary['flow_rows']}, "
        f"util rows written: {summary['util_rows']}"
    )
    print(
        "Try: curl -H 'X-API-Key: <key>' "
        "'http://localhost:8080/fabric/health?window_minutes=15'"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="synthetic_load",
        description="Write plausible rows for demos when no live sFlow is available.",
    )
    parser.add_argument("--tenant-slug", default="default")
    parser.add_argument(
        "--minutes", type=int, default=30,
        help="Backfill this many minute-buckets ending now.",
    )
    parser.add_argument(
        "--sampling-rate", type=int, default=1000,
        help="Synthesized sampling_rate carried on flow rows.",
    )
    parser.add_argument(
        "--switches", nargs="*",
        help="Override the default fabric model. Each name gets an "
             "edge + uplink interface.",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
