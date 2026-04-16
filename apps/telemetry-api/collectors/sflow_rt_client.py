"""sFlow-RT RESTflow polling client.

The only component that talks to sFlow-RT. Everything downstream reads
from the database populated by the ingestion loop. Keep this client
free of DB coupling so it can be unit-tested against a live sFlow-RT
instance in isolation.
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional

import httpx

from shared.schemas.flow import FlowRecord
from shared.schemas.interface import InterfaceCounter

log = logging.getLogger(__name__)


class SFlowRTClient:
    def __init__(self, base_url: str, timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=timeout)

    async def health_check(self) -> bool:
        try:
            resp = await self.client.get(f"{self.base_url}/version")
            return resp.status_code == 200
        except Exception as e:
            log.warning(f"sFlow-RT health check failed: {e}")
            return False

    async def get_top_flows(
        self,
        max_flows: int = 100,
        min_bytes: int = 0,
    ) -> List[FlowRecord]:
        """Fetch active flows from sFlow-RT RESTflow API.

        Returns empty list on error — caller should log and continue.
        """
        url = f"{self.base_url}/activeflows/json"
        params = {"maxFlows": max_flows, "minValue": min_bytes}
        try:
            resp = await self.client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            now = datetime.now(timezone.utc)
            records: List[FlowRecord] = []
            for row in data.get("flows", []):
                try:
                    records.append(
                        FlowRecord(
                            agent=row["agent"],
                            input_if_index=row.get("inputifindex", 0),
                            output_if_index=row.get("outputifindex", 0),
                            src_ip=row.get("srcip", "0.0.0.0"),
                            dst_ip=row.get("dstip", "0.0.0.0"),
                            protocol=int(row.get("protocol", 0)),
                            bytes=int(row.get("bytes", 0)),
                            packets=int(row.get("packets", 0)),
                            sampling_rate=int(row.get("samplingrate", 1)),
                            timestamp=now,
                        )
                    )
                except Exception as parse_err:
                    log.debug(f"Skipping malformed flow row: {parse_err}")
            return records
        except httpx.HTTPError as e:
            log.error(f"Failed to fetch flows from sFlow-RT: {e}")
            return []

    async def get_interface_counters(
        self,
        agent: Optional[str] = None,
    ) -> List[InterfaceCounter]:
        """Fetch interface counters. If agent is None, fetches all known agents."""
        agent_path = agent if agent else "ALL"
        url = f"{self.base_url}/ifcounters/{agent_path}/json"
        try:
            resp = await self.client.get(url)
            resp.raise_for_status()
            data = resp.json()
            now = datetime.now(timezone.utc)
            counters: List[InterfaceCounter] = []
            for item in data:
                counters.append(
                    InterfaceCounter(
                        agent=item["agent"],
                        if_index=int(item["ifindex"]),
                        if_name=item.get("ifname", f"if{item['ifindex']}"),
                        if_speed=int(item.get("ifspeed", 1_000_000_000)),
                        if_in_octets=int(item.get("ifinoctets", 0)),
                        if_out_octets=int(item.get("ifoutoctets", 0)),
                        if_in_errors=int(item.get("ifinerrors", 0)),
                        if_out_errors=int(item.get("ifouterrors", 0)),
                        timestamp=now,
                    )
                )
            return counters
        except httpx.HTTPError as e:
            log.error(f"Failed to fetch interface counters: {e}")
            return []

    async def close(self):
        await self.client.aclose()
