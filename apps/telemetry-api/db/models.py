from sqlalchemy import (
    Column,
    String,
    Integer,
    BigInteger,
    Float,
    DateTime,
    Text,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class FlowSummaryMinute(Base):
    """Minute-bucketed flow summaries. Sampling-corrected estimates."""

    __tablename__ = "flow_summary_minute"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_bucket = Column(DateTime(timezone=True), nullable=False, index=True)
    device = Column(String(255), nullable=False)
    interface = Column(String(255), nullable=False)
    src_ip = Column(String(45), nullable=False)   # supports IPv6
    dst_ip = Column(String(45), nullable=False)
    protocol = Column(Integer, nullable=False)
    bytes_estimated = Column(BigInteger, nullable=False)     # raw * sampling_rate
    packets_estimated = Column(BigInteger, nullable=False)
    sampling_rate = Column(Integer, nullable=False)          # always store this

    __table_args__ = (
        Index("ix_flow_device_ts", "device", "ts_bucket"),
        Index("ix_flow_src_dst_ts", "src_ip", "dst_ip", "ts_bucket"),
    )


class InterfaceUtilizationMinute(Base):
    """Minute-bucketed interface utilization metrics."""

    __tablename__ = "interface_utilization_minute"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_bucket = Column(DateTime(timezone=True), nullable=False, index=True)
    device = Column(String(255), nullable=False)
    interface = Column(String(255), nullable=False)
    in_bps = Column(BigInteger, nullable=False)
    out_bps = Column(BigInteger, nullable=False)
    in_util_pct = Column(Float, nullable=False)
    out_util_pct = Column(Float, nullable=False)
    error_count = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_util_device_if_ts", "device", "interface", "ts_bucket"),
    )


class AnomalyEvent(Base):
    """Detected anomaly events with severity and plain-language summary."""

    __tablename__ = "anomaly_events"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    scope = Column(String(512), nullable=False)
    anomaly_type = Column(String(64), nullable=False)
    severity = Column(String(16), nullable=False)
    summary = Column(Text, nullable=False)
    metadata_json = Column(JSONB, nullable=True)
