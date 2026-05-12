"""gNMI / OpenConfig device-state schemas.

These are exact (not sampled) telemetry — no sampling_rate field. They
correspond to OpenConfig YANG paths under interfaces/, network-instances/,
and qos/.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class InterfaceState(BaseModel):
    """openconfig-interfaces:interfaces/interface/state snapshot."""

    device: str
    interface: str
    admin_status: str           # UP|DOWN|TESTING
    oper_status: str            # UP|DOWN|LOWER_LAYER_DOWN|UNKNOWN|...
    last_change: Optional[datetime] = None
    speed_bps: Optional[int] = None
    mtu: Optional[int] = None
    description: Optional[str] = None
    timestamp: datetime


class BGPNeighborState(BaseModel):
    """openconfig-bgp:bgp/neighbors/neighbor/state snapshot."""

    device: str
    peer_address: str
    peer_as: Optional[int] = None
    session_state: str          # IDLE|CONNECT|ACTIVE|OPENSENT|OPENCONFIRM|ESTABLISHED
    uptime_seconds: Optional[int] = None
    prefixes_received: Optional[int] = None
    prefixes_sent: Optional[int] = None
    last_error: Optional[str] = None
    timestamp: datetime


class LLDPNeighborState(BaseModel):
    """openconfig-lldp:lldp/interfaces/interface/neighbors/neighbor/state snapshot.

    LLDP advertises L2 adjacency. We persist one row per (local
    interface, remote chassis_id) — that's the granularity at which a
    chatbot answers "what's plugged into Eth0/3?" and "which devices
    are adjacent to leaf1?" without rebuilding the topology graph from
    flow data.
    """

    device: str
    interface: str
    neighbor_chassis_id: str
    neighbor_system_name: Optional[str] = None
    neighbor_port_id: Optional[str] = None
    neighbor_port_description: Optional[str] = None
    neighbor_management_address: Optional[str] = None
    timestamp: datetime


class QueueState(BaseModel):
    """openconfig-qos:qos/interfaces/.../queues/queue/state snapshot.

    Captures buffer utilization and the PFC / ECN counters that surface
    congestion in RoCE / RDMA fabrics.
    """

    device: str
    interface: str
    queue_id: int
    traffic_class: Optional[int] = None
    max_depth_bytes: int
    avg_depth_bytes: int
    pfc_pause_rx: int = 0
    pfc_pause_tx: int = 0
    ecn_marked_packets: int = 0
    dropped_packets: int = 0
    timestamp: datetime
