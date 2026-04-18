from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Tenancy + auth (PR 20)
# ---------------------------------------------------------------------------

# The well-known default tenant used for single-tenant installs and for legacy
# ingestion writes until PR 14 lands per-source tenant mapping.
DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    slug = Column(String(64), nullable=False, unique=True)
    name = Column(String(255), nullable=False)
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class APIKey(Base):
    """Hashed API keys bound to a tenant and a role.

    The plaintext key is never stored — only sha256(key). Lookups use the
    hash so a DB compromise does not leak keys.
    """

    __tablename__ = "api_keys"

    id = Column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id = Column(
        UUID(as_uuid=False),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    key_hash = Column(String(64), nullable=False, unique=True)
    role = Column(String(32), nullable=False)   # viewer|analyst|operator|tenant_admin
    name = Column(String(128), nullable=False)
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_api_keys_tenant", "tenant_id"),
    )


class AuditLog(Base):
    """Append-only audit trail for API access.

    One row per request that carried a valid API key. Covers the actor
    (tenant + key + role), the target (method + path + status), and the
    effective scope seen by the query layer.
    """

    __tablename__ = "audit_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    tenant_id = Column(UUID(as_uuid=False), nullable=False)
    api_key_id = Column(UUID(as_uuid=False), nullable=True)
    role = Column(String(32), nullable=False)
    method = Column(String(16), nullable=False)
    path = Column(String(512), nullable=False)
    status_code = Column(Integer, nullable=False)
    scope = Column(String(255), nullable=True)
    duration_ms = Column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_audit_log_tenant_ts", "tenant_id", "ts"),
    )


# ---------------------------------------------------------------------------
# Per-source → tenant routing (PR 22)
# ---------------------------------------------------------------------------

class CollectorSource(Base):
    """Maps (source_kind, source_identifier) to the owning tenant.

    Replaces the DEFAULT_TENANT_ID hardcoding in the ingest loops. A row
    is keyed on a small set of canonical kinds (sflow|gnmi) plus the
    source-side identifier the collector sees on the wire — for sFlow
    that's the agent IP/hostname, for gNMI it's the target hostname.

    Lookups are cached in-process by services.tenant_routing; operators
    update mappings via scripts/seed.py and the cache picks them up on
    its next refresh tick.
    """

    __tablename__ = "collector_sources"

    id = Column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id = Column(
        UUID(as_uuid=False),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_kind = Column(String(32), nullable=False)         # sflow|gnmi
    source_identifier = Column(String(255), nullable=False)
    description = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "source_kind", "source_identifier", name="uq_collector_source_kind_id"
        ),
        Index("ix_collector_sources_tenant", "tenant_id"),
        Index(
            "ix_collector_sources_lookup", "source_kind", "source_identifier"
        ),
    )


# ---------------------------------------------------------------------------
# Telemetry tables (v1 — extended with tenant_id in PR 20)
# ---------------------------------------------------------------------------

class FlowSummaryMinute(Base):
    """Minute-bucketed flow summaries. Sampling-corrected estimates."""

    __tablename__ = "flow_summary_minute"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(UUID(as_uuid=False), nullable=False)
    ts_bucket = Column(DateTime(timezone=True), nullable=False, index=True)
    device = Column(String(255), nullable=False)
    interface = Column(String(255), nullable=False)
    src_ip = Column(String(45), nullable=False)
    dst_ip = Column(String(45), nullable=False)
    protocol = Column(Integer, nullable=False)
    bytes_estimated = Column(BigInteger, nullable=False)
    packets_estimated = Column(BigInteger, nullable=False)
    sampling_rate = Column(Integer, nullable=False)

    __table_args__ = (
        Index("ix_flow_tenant_ts", "tenant_id", "ts_bucket"),
        Index("ix_flow_device_ts", "device", "ts_bucket"),
        Index("ix_flow_src_dst_ts", "src_ip", "dst_ip", "ts_bucket"),
    )


class InterfaceUtilizationMinute(Base):
    __tablename__ = "interface_utilization_minute"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(UUID(as_uuid=False), nullable=False)
    ts_bucket = Column(DateTime(timezone=True), nullable=False, index=True)
    device = Column(String(255), nullable=False)
    interface = Column(String(255), nullable=False)
    in_bps = Column(BigInteger, nullable=False)
    out_bps = Column(BigInteger, nullable=False)
    in_util_pct = Column(Float, nullable=False)
    out_util_pct = Column(Float, nullable=False)
    error_count = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_util_tenant_ts", "tenant_id", "ts_bucket"),
        Index("ix_util_device_if_ts", "device", "interface", "ts_bucket"),
    )


class BaselineSnapshot(Base):
    __tablename__ = "baseline_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(UUID(as_uuid=False), nullable=False)
    computed_at = Column(DateTime(timezone=True), nullable=False)
    device = Column(String(255), nullable=False)
    interface = Column(String(255), nullable=False)
    hour_of_day = Column(Integer, nullable=False)
    metric = Column(String(64), nullable=False)
    mean_value = Column(Float, nullable=False)
    stddev_value = Column(Float, nullable=False)
    sample_count = Column(Integer, nullable=False)

    __table_args__ = (
        Index("ix_baseline_tenant", "tenant_id"),
        Index(
            "ix_baseline_device_if_hour",
            "device",
            "interface",
            "hour_of_day",
        ),
    )


class AnomalyEvent(Base):
    __tablename__ = "anomaly_events"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id = Column(UUID(as_uuid=False), nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    scope = Column(String(512), nullable=False)
    anomaly_type = Column(String(64), nullable=False)
    severity = Column(String(16), nullable=False)
    summary = Column(Text, nullable=False)
    metadata_json = Column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_anomaly_tenant_ts", "tenant_id", "ts"),
    )


# ---------------------------------------------------------------------------
# gNMI / OpenConfig device state (PR 21)
# ---------------------------------------------------------------------------
# These are exact (not sampled) telemetry. No sampling_rate is carried.

class DeviceStateMinute(Base):
    """OpenConfig interfaces/interface/state per minute snapshot."""

    __tablename__ = "device_state_minute"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(UUID(as_uuid=False), nullable=False)
    ts_bucket = Column(DateTime(timezone=True), nullable=False)
    device = Column(String(255), nullable=False)
    interface = Column(String(255), nullable=False)
    admin_status = Column(String(16), nullable=False)   # UP|DOWN|TESTING
    oper_status = Column(String(16), nullable=False)    # UP|DOWN|LOWER_LAYER_DOWN|...
    last_change = Column(DateTime(timezone=True), nullable=True)
    speed_bps = Column(BigInteger, nullable=True)
    mtu = Column(Integer, nullable=True)
    description = Column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_devstate_tenant_ts", "tenant_id", "ts_bucket"),
        Index("ix_devstate_device_if_ts", "device", "interface", "ts_bucket"),
    )


class BGPSessionMinute(Base):
    """OpenConfig network-instances/.../bgp/neighbors/neighbor/state snapshot."""

    __tablename__ = "bgp_session_minute"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(UUID(as_uuid=False), nullable=False)
    ts_bucket = Column(DateTime(timezone=True), nullable=False)
    device = Column(String(255), nullable=False)
    peer_address = Column(String(64), nullable=False)
    peer_as = Column(Integer, nullable=True)
    session_state = Column(String(32), nullable=False)  # IDLE|CONNECT|ACTIVE|OPENSENT|OPENCONFIRM|ESTABLISHED
    uptime_seconds = Column(BigInteger, nullable=True)
    prefixes_received = Column(BigInteger, nullable=True)
    prefixes_sent = Column(BigInteger, nullable=True)
    last_error = Column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_bgp_tenant_ts", "tenant_id", "ts_bucket"),
        Index("ix_bgp_device_peer_ts", "device", "peer_address", "ts_bucket"),
    )


class QueueStatsMinute(Base):
    """qos/interfaces/.../queues/queue/state — buffer + PFC + ECN telemetry.

    Critical for RDMA / RoCE fabrics (PR 23): pfc_pause_rx > 0 indicates
    receiver-side congestion; ecn_marked_packets > 0 indicates DCQCN signaling.
    """

    __tablename__ = "queue_stats_minute"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(UUID(as_uuid=False), nullable=False)
    ts_bucket = Column(DateTime(timezone=True), nullable=False)
    device = Column(String(255), nullable=False)
    interface = Column(String(255), nullable=False)
    queue_id = Column(Integer, nullable=False)
    traffic_class = Column(Integer, nullable=True)
    max_depth_bytes = Column(BigInteger, nullable=False)
    avg_depth_bytes = Column(BigInteger, nullable=False)
    pfc_pause_rx = Column(BigInteger, nullable=False, default=0)
    pfc_pause_tx = Column(BigInteger, nullable=False, default=0)
    ecn_marked_packets = Column(BigInteger, nullable=False, default=0)
    dropped_packets = Column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        Index("ix_queue_tenant_ts", "tenant_id", "ts_bucket"),
        Index(
            "ix_queue_device_if_q_ts",
            "device",
            "interface",
            "queue_id",
            "ts_bucket",
        ),
    )
