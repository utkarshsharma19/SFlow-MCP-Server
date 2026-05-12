"""Verity orchestrator API client (PR 30).

Verity (Hedgehog) exposes a REST API describing declared fabric intent:
each Connection / Switch / VPC carries a JSON spec the orchestrator
considers truth. We poll that surface and project it into
``device_intent`` / ``bgp_intent`` so the diff service can compare
against the live gNMI observation without taking a direct dependency
on Verity at query time.

The client is deliberately a thin HTTP wrapper around an *adapter*
interface: any orchestrator that emits the same canonical
``DeclaredInterface`` / ``DeclaredBGPPeer`` shape can slot in by writing
a sibling client (Apstra, EOS-CLI dumps, NetBox, etc.).

Auth: bearer token from env. If the token is unset the client returns
empty results — the ingest loop then idles, which matches the gNMI
client's "no targets configured" behavior.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeclaredInterface:
    """Canonical intent shape — what every orchestrator must produce."""

    device: str
    interface: str
    admin_status: Optional[str] = None     # UP|DOWN|None
    oper_status: Optional[str] = None      # UP|DOWN|None — usually inferred
    speed_bps: Optional[int] = None
    mtu: Optional[int] = None
    description: Optional[str] = None


@dataclass(frozen=True)
class DeclaredBGPPeer:
    device: str
    peer_address: str
    peer_as: Optional[int] = None
    session_state: Optional[str] = None    # usually ESTABLISHED


class VerityClient:
    """REST client for a Verity-style fabric controller.

    Real Verity endpoints are versioned; this implementation targets the
    public ``/api/v1`` surface as observed in the open-source Hedgehog
    Fabric documentation. Operators can override the base URL + path
    prefix via env vars to point at an internal proxy or test double.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("VERITY_BASE_URL", "")).rstrip("/")
        self.token = token or os.getenv("VERITY_TOKEN", "")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url) and bool(self.token)

    async def _get(self, path: str) -> dict | list:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.token}"},
            )
        resp = await self._client.get(path)
        resp.raise_for_status()
        return resp.json()

    async def list_declared_interfaces(self) -> list[DeclaredInterface]:
        """Pull every interface intent declaration from the controller.

        We hit ``/api/v1/switches`` and then walk each switch's
        ``ports`` array; the alternative (one call per switch) is
        chatty and rate-limits in larger fabrics.
        """
        if not self.is_configured:
            return []
        try:
            payload = await self._get("/api/v1/switches")
        except httpx.HTTPError as e:
            log.warning("verity list_declared_interfaces failed: %s", e)
            return []
        return list(_parse_interfaces(payload))

    async def list_declared_bgp_peers(self) -> list[DeclaredBGPPeer]:
        if not self.is_configured:
            return []
        try:
            payload = await self._get("/api/v1/bgp-peerings")
        except httpx.HTTPError as e:
            log.warning("verity list_declared_bgp_peers failed: %s", e)
            return []
        return list(_parse_bgp(payload))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ---------------------------------------------------------------------------
# Parsers — separated so a unit test can feed canned payloads
# ---------------------------------------------------------------------------

def _parse_interfaces(payload):
    """Yield DeclaredInterface from a Verity-shaped switches response.

    Resilient to two shapes the controller is known to return:
      * top-level ``{"items": [...]}``
      * bare list ``[...]``
    Missing keys yield ``None`` rather than raise, so a partial
    declaration still produces a row (the diff service treats NULL as
    'no opinion').
    """
    switches = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(switches, list):
        return
    for sw in switches:
        if not isinstance(sw, dict):
            continue
        device = sw.get("name") or sw.get("hostname")
        if not device:
            continue
        for port in sw.get("ports", []) or []:
            iface = port.get("name") or port.get("interface")
            if not iface:
                continue
            yield DeclaredInterface(
                device=device,
                interface=iface,
                admin_status=_canonical_status(port.get("admin")),
                oper_status=_canonical_status(port.get("oper")),
                speed_bps=_to_int(port.get("speed_bps") or port.get("speedBps")),
                mtu=_to_int(port.get("mtu")),
                description=port.get("description"),
            )


def _parse_bgp(payload):
    peerings = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(peerings, list):
        return
    for p in peerings:
        if not isinstance(p, dict):
            continue
        device = p.get("device") or p.get("switch")
        peer = p.get("peer") or p.get("peer_address") or p.get("peerAddress")
        if not device or not peer:
            continue
        yield DeclaredBGPPeer(
            device=device,
            peer_address=peer,
            peer_as=_to_int(p.get("peer_as") or p.get("peerAs")),
            session_state=_canonical_bgp_state(
                p.get("expected_state") or p.get("expectedState")
            ),
        )


def _canonical_status(raw) -> str | None:
    if raw is None:
        return None
    s = str(raw).upper()
    if s in {"UP", "DOWN", "TESTING"}:
        return s
    return None


def _canonical_bgp_state(raw) -> str | None:
    if raw is None:
        return None
    s = str(raw).upper()
    if s in {
        "IDLE",
        "CONNECT",
        "ACTIVE",
        "OPENSENT",
        "OPENCONFIRM",
        "ESTABLISHED",
    }:
        return s
    return None


def _to_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
