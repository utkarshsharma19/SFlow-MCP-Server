"""gNMI / OpenConfig polling client.

Subscribes (poll-mode) to OpenConfig YANG paths on each configured target:
  * interfaces/interface/state
  * network-instances/.../bgp/neighbors/neighbor/state
  * qos/interfaces/.../queues/queue/state

Targets are configured via the GNMI_TARGETS env var as a comma-separated
list of hostnames or host:port pairs. The pygnmi import is deferred so the
service still boots — and tests still run — when pygnmi is not installed
or no targets are configured.

Like the sFlow-RT client, this module is decoupled from the DB. The
ingestion loop owns persistence; this client only fetches and parses.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

from shared.schemas.device_state import (
    BGPNeighborState,
    InterfaceState,
    QueueState,
)

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_targets(raw: str | None) -> list[tuple[str, int]]:
    """Parse 'host1,host2:50051' → [(host1, 6030), (host2, 50051)]."""
    if not raw:
        return []
    out: list[tuple[str, int]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            host, _, port = chunk.partition(":")
            try:
                out.append((host, int(port)))
            except ValueError:
                log.warning(f"ignoring malformed gNMI target: {chunk}")
        else:
            out.append((chunk, 6030))
    return out


# Map raw OpenConfig admin/oper string fragments to the canonical values
# we persist. gNMI implementations vary in casing, so we normalize here.
_ADMIN_MAP = {"UP": "UP", "DOWN": "DOWN", "TESTING": "TESTING"}
_OPER_MAP = {
    "UP": "UP",
    "DOWN": "DOWN",
    "TESTING": "TESTING",
    "UNKNOWN": "UNKNOWN",
    "DORMANT": "DORMANT",
    "NOT_PRESENT": "NOT_PRESENT",
    "LOWER_LAYER_DOWN": "LOWER_LAYER_DOWN",
}
_BGP_STATES = {
    "IDLE",
    "CONNECT",
    "ACTIVE",
    "OPENSENT",
    "OPENCONFIRM",
    "ESTABLISHED",
}


def _normalize_admin(raw) -> str:
    s = str(raw or "").upper().split(":")[-1]
    return _ADMIN_MAP.get(s, "UNKNOWN")


def _normalize_oper(raw) -> str:
    s = str(raw or "").upper().split(":")[-1]
    return _OPER_MAP.get(s, "UNKNOWN")


def _normalize_bgp_state(raw) -> str:
    s = str(raw or "").upper().split(":")[-1]
    return s if s in _BGP_STATES else "IDLE"


class GNMIClient:
    """Thin wrapper around pygnmi.client.gNMIclient.

    Falls back to an empty-result mode when pygnmi is missing or no
    targets are configured. This keeps the service operational in
    development environments without real SONiC/Arista devices.
    """

    def __init__(
        self,
        targets: list[tuple[str, int]] | None = None,
        username: str | None = None,
        password: str | None = None,
        insecure: bool = True,
    ) -> None:
        self.targets = targets or parse_targets(os.getenv("GNMI_TARGETS"))
        self.username = username or os.getenv("GNMI_USERNAME", "admin")
        self.password = password or os.getenv("GNMI_PASSWORD", "")
        self.insecure = insecure

        self._pygnmi_available = self._probe_pygnmi()
        if not self.targets:
            log.info("gNMI client disabled: no GNMI_TARGETS configured")
        elif not self._pygnmi_available:
            log.warning(
                "gNMI targets configured but pygnmi not installed; "
                "install the 'gnmi' extra to enable collection"
            )

    @staticmethod
    def _probe_pygnmi() -> bool:
        try:
            import pygnmi  # noqa: F401
            return True
        except ImportError:
            return False

    @property
    def enabled(self) -> bool:
        return self._pygnmi_available and bool(self.targets)

    async def health_check(self) -> bool:
        """Best-effort reachability probe across all targets.

        Returns True if at least one target answers a Capabilities call.
        """
        if not self.enabled:
            return False
        for host, port in self.targets:
            try:
                with self._open(host, port) as gc:
                    gc.capabilities()
                return True
            except Exception as e:
                log.debug(f"gNMI capabilities failed for {host}:{port}: {e}")
        return False

    def _open(self, host: str, port: int):
        from pygnmi.client import gNMIclient

        return gNMIclient(
            target=(host, port),
            username=self.username,
            password=self.password,
            insecure=self.insecure,
        )

    # -----------------------------------------------------------------
    # Per-path getters. Each handles its own exceptions so a single bad
    # target / path never aborts the whole ingestion cycle.
    # -----------------------------------------------------------------

    async def get_interface_state(self) -> List[InterfaceState]:
        if not self.enabled:
            return []
        out: List[InterfaceState] = []
        for host, port in self.targets:
            try:
                with self._open(host, port) as gc:
                    resp = gc.get(path=["openconfig-interfaces:interfaces"])
                out.extend(_parse_interface_state(host, resp))
            except Exception as e:
                log.warning(f"gNMI interface fetch failed for {host}: {e}")
        return out

    async def get_bgp_neighbors(self) -> List[BGPNeighborState]:
        if not self.enabled:
            return []
        out: List[BGPNeighborState] = []
        for host, port in self.targets:
            try:
                with self._open(host, port) as gc:
                    resp = gc.get(
                        path=["openconfig-network-instance:network-instances"]
                    )
                out.extend(_parse_bgp_neighbors(host, resp))
            except Exception as e:
                log.warning(f"gNMI BGP fetch failed for {host}: {e}")
        return out

    async def get_queue_stats(self) -> List[QueueState]:
        if not self.enabled:
            return []
        out: List[QueueState] = []
        for host, port in self.targets:
            try:
                with self._open(host, port) as gc:
                    resp = gc.get(path=["openconfig-qos:qos/interfaces"])
                out.extend(_parse_queue_stats(host, resp))
            except Exception as e:
                log.warning(f"gNMI queue fetch failed for {host}: {e}")
        return out

    async def close(self) -> None:
        # pygnmi context-manages each call, nothing persistent to close.
        return None


# ---------------------------------------------------------------------------
# Response parsers — split out of the client to keep them unit-testable
# without a live target.
# ---------------------------------------------------------------------------

def _walk_notifications(resp: dict | None):
    """Yield (path_str, value) for every leaf in a gNMI Get response."""
    if not resp:
        return
    for note in resp.get("notification", []):
        for upd in note.get("update", []):
            yield upd.get("path", ""), upd.get("val")


def _parse_interface_state(device: str, resp: dict | None) -> List[InterfaceState]:
    """Pull interface state leaves out of a gNMI Get response.

    pygnmi flattens the path into something like:
      interfaces/interface[name=Ethernet0]/state/oper-status

    We group by the [name=...] key and emit one InterfaceState per interface.
    """
    by_iface: dict[str, dict] = {}
    for path, val in _walk_notifications(resp):
        if not path or "interface[name=" not in path:
            continue
        name = path.split("interface[name=", 1)[1].split("]", 1)[0]
        slot = by_iface.setdefault(name, {})
        leaf = path.rsplit("/", 1)[-1]
        slot[leaf] = val

    ts = _now()
    out: List[InterfaceState] = []
    for iface, leaves in by_iface.items():
        if "oper-status" not in leaves and "admin-status" not in leaves:
            continue
        out.append(
            InterfaceState(
                device=device,
                interface=iface,
                admin_status=_normalize_admin(leaves.get("admin-status")),
                oper_status=_normalize_oper(leaves.get("oper-status")),
                last_change=_parse_iso(leaves.get("last-change")),
                speed_bps=_safe_int(leaves.get("speed")),
                mtu=_safe_int(leaves.get("mtu")),
                description=_safe_str(leaves.get("description")),
                timestamp=ts,
            )
        )
    return out


def _parse_bgp_neighbors(device: str, resp: dict | None) -> List[BGPNeighborState]:
    by_peer: dict[str, dict] = {}
    for path, val in _walk_notifications(resp):
        if "neighbor[neighbor-address=" not in path:
            continue
        addr = path.split("neighbor[neighbor-address=", 1)[1].split("]", 1)[0]
        slot = by_peer.setdefault(addr, {})
        leaf = path.rsplit("/", 1)[-1]
        slot[leaf] = val

    ts = _now()
    out: List[BGPNeighborState] = []
    for peer, leaves in by_peer.items():
        if "session-state" not in leaves:
            continue
        out.append(
            BGPNeighborState(
                device=device,
                peer_address=peer,
                peer_as=_safe_int(leaves.get("peer-as")),
                session_state=_normalize_bgp_state(leaves.get("session-state")),
                uptime_seconds=_safe_int(leaves.get("uptime")),
                prefixes_received=_safe_int(leaves.get("received-prefixes")),
                prefixes_sent=_safe_int(leaves.get("sent-prefixes")),
                last_error=_safe_str(leaves.get("last-error")),
                timestamp=ts,
            )
        )
    return out


def _parse_queue_stats(device: str, resp: dict | None) -> List[QueueState]:
    by_key: dict[tuple[str, int], dict] = {}
    for path, val in _walk_notifications(resp):
        if "interface[interface-id=" not in path or "queue[name=" not in path:
            continue
        iface = path.split("interface[interface-id=", 1)[1].split("]", 1)[0]
        qname = path.split("queue[name=", 1)[1].split("]", 1)[0]
        try:
            qid = int(qname)
        except ValueError:
            qid = abs(hash(qname)) % 1024
        slot = by_key.setdefault((iface, qid), {})
        leaf = path.rsplit("/", 1)[-1]
        slot[leaf] = val

    ts = _now()
    out: List[QueueState] = []
    for (iface, qid), leaves in by_key.items():
        out.append(
            QueueState(
                device=device,
                interface=iface,
                queue_id=qid,
                traffic_class=_safe_int(leaves.get("traffic-class")),
                max_depth_bytes=_safe_int(leaves.get("max-queue-len")) or 0,
                avg_depth_bytes=_safe_int(leaves.get("avg-queue-len")) or 0,
                pfc_pause_rx=_safe_int(leaves.get("pfc-pause-rx")) or 0,
                pfc_pause_tx=_safe_int(leaves.get("pfc-pause-tx")) or 0,
                ecn_marked_packets=_safe_int(leaves.get("ecn-marked-pkts")) or 0,
                dropped_packets=_safe_int(leaves.get("dropped-pkts")) or 0,
                timestamp=ts,
            )
        )
    return out


def _safe_int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _safe_str(val) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _parse_iso(val) -> Optional[datetime]:
    if not val:
        return None
    try:
        # gNMI commonly emits last-change as nanoseconds since epoch.
        if isinstance(val, (int, float)) or (isinstance(val, str) and val.isdigit()):
            ns = int(val)
            return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return None
