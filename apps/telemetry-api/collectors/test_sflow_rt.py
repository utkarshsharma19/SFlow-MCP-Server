"""Manual integration test — run with: python -m collectors.test_sflow_rt

Milestone gate: this test must pass before any DB or MCP code is
touched. If it fails, fix sFlow-RT connectivity first — everything
downstream depends on this client returning real flow + counter data.
"""
import asyncio
import os

from collectors.sflow_rt_client import SFlowRTClient


async def main():
    url = os.getenv("SFLOW_RT_URL", "http://localhost:8008")
    client = SFlowRTClient(url)

    print("--- Health Check ---")
    ok = await client.health_check()
    print(f"  sFlow-RT reachable: {ok}")
    assert ok, "STOP HERE: fix sFlow-RT connectivity before proceeding"

    print("\n--- Top Flows (max 5) ---")
    flows = await client.get_top_flows(max_flows=5)
    print(f"  Received {len(flows)} flows")
    for f in flows[:3]:
        est = f.bytes * f.sampling_rate
        print(
            f"  {f.src_ip} -> {f.dst_ip}  proto={f.protocol}  "
            f"raw_bytes={f.bytes}  est_bytes={est}"
        )

    print("\n--- Interface Counters ---")
    counters = await client.get_interface_counters()
    print(f"  Received {len(counters)} interface counter records")
    for c in counters[:3]:
        print(f"  {c.agent} {c.if_name}  in={c.if_in_octets}  out={c.if_out_octets}")

    await client.close()
    print("\nMilestone gate PASSED: sFlow-RT client is working.")


if __name__ == "__main__":
    asyncio.run(main())
