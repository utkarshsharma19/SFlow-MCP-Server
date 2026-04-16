"""initial tables — flow_summary_minute, interface_utilization_minute, anomaly_events

Revision ID: 0001
Revises:
Create Date: 2026-04-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # gen_random_uuid() lives in pgcrypto on older PGs; 16+ ships it in core.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "flow_summary_minute",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("ts_bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device", sa.String(length=255), nullable=False),
        sa.Column("interface", sa.String(length=255), nullable=False),
        sa.Column("src_ip", sa.String(length=45), nullable=False),
        sa.Column("dst_ip", sa.String(length=45), nullable=False),
        sa.Column("protocol", sa.Integer(), nullable=False),
        sa.Column("bytes_estimated", sa.BigInteger(), nullable=False),
        sa.Column("packets_estimated", sa.BigInteger(), nullable=False),
        sa.Column("sampling_rate", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_flow_summary_minute_ts_bucket", "flow_summary_minute", ["ts_bucket"]
    )
    op.create_index(
        "ix_flow_device_ts", "flow_summary_minute", ["device", "ts_bucket"]
    )
    op.create_index(
        "ix_flow_src_dst_ts",
        "flow_summary_minute",
        ["src_ip", "dst_ip", "ts_bucket"],
    )

    op.create_table(
        "interface_utilization_minute",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("ts_bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device", sa.String(length=255), nullable=False),
        sa.Column("interface", sa.String(length=255), nullable=False),
        sa.Column("in_bps", sa.BigInteger(), nullable=False),
        sa.Column("out_bps", sa.BigInteger(), nullable=False),
        sa.Column("in_util_pct", sa.Float(), nullable=False),
        sa.Column("out_util_pct", sa.Float(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_interface_utilization_minute_ts_bucket",
        "interface_utilization_minute",
        ["ts_bucket"],
    )
    op.create_index(
        "ix_util_device_if_ts",
        "interface_utilization_minute",
        ["device", "interface", "ts_bucket"],
    )

    op.create_table(
        "anomaly_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scope", sa.String(length=512), nullable=False),
        sa.Column("anomaly_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index("ix_anomaly_events_ts", "anomaly_events", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_anomaly_events_ts", table_name="anomaly_events")
    op.drop_table("anomaly_events")
    op.drop_index(
        "ix_util_device_if_ts", table_name="interface_utilization_minute"
    )
    op.drop_index(
        "ix_interface_utilization_minute_ts_bucket",
        table_name="interface_utilization_minute",
    )
    op.drop_table("interface_utilization_minute")
    op.drop_index("ix_flow_src_dst_ts", table_name="flow_summary_minute")
    op.drop_index("ix_flow_device_ts", table_name="flow_summary_minute")
    op.drop_index(
        "ix_flow_summary_minute_ts_bucket", table_name="flow_summary_minute"
    )
    op.drop_table("flow_summary_minute")
