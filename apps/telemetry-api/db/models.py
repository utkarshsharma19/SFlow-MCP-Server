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


class BaselineSnapshot(Base):
    """Rolling baseline per device/interface/hour for anomaly detection.

    Computed every 5 minutes over a 7-day rolling window. Diurnal
    bucketing (hour_of_day 0-23) captures weekday-morning vs
    weekend-midnight differences without over-fitting to a single day.
    """

    __tablename__ = "baseline_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    computed_at = Column(DateTime(timezone=True), nullable=False)
    device = Column(String(255), nullable=False)
    interface = Column(String(255), nullable=False)
    hour_of_day = Column(Integer, nullable=False)     # 0-23
    metric = Column(String(64), nullable=False)       # 'bytes' | 'util_pct' | ...
    mean_value = Column(Float, nullable=False)
    stddev_value = Column(Float, nullable=False)
    sample_count = Column(Integer, nullable=False)

    __table_args__ = (
        Index(
            "ix_baseline_device_if_hour",
            "device",
            "interface",
            "hour_of_day",
        ),
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
