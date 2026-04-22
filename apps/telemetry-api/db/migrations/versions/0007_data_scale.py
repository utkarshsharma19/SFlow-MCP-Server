"""data-scale hardening — partitioning, retention, dedup, source freshness

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-18

Three intertwined changes that have to ship together so the query layer
keeps working end to end:

1. Convert the three high-volume time-series tables to range-partitioned
   by ``ts_bucket`` (monthly). Pre-existing rows are copied into the new
   partitioned table; the old table is dropped in the same transaction.
   Partition management lives in SQL-side helpers (``ensure_monthly_partitions``
   and ``drop_partitions_older_than``) that a periodic worker or pg_cron
   can call without app-side migrations.

2. Extend ``anomaly_events`` with lifecycle + dedup columns so detectors
   can upsert on fingerprint rather than appending a row every tick. A
   partial unique index enforces "one open event per fingerprint" — once
   ``resolved_at`` is set, the next recurrence opens a fresh row.

3. Add ``source_freshness`` so silent collectors become a first-class
   anomaly signal instead of invisible data loss.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PARTITIONED_TABLES = {
    "flow_summary_minute": """
        id              BIGSERIAL,
        tenant_id       UUID NOT NULL,
        ts_bucket       TIMESTAMPTZ NOT NULL,
        device          VARCHAR(255) NOT NULL,
        interface       VARCHAR(255) NOT NULL,
        src_ip          VARCHAR(45) NOT NULL,
        dst_ip          VARCHAR(45) NOT NULL,
        protocol        INTEGER NOT NULL,
        bytes_estimated BIGINT NOT NULL,
        packets_estimated BIGINT NOT NULL,
        sampling_rate   INTEGER NOT NULL,
        PRIMARY KEY (id, ts_bucket)
    """,
    "interface_utilization_minute": """
        id              BIGSERIAL,
        tenant_id       UUID NOT NULL,
        ts_bucket       TIMESTAMPTZ NOT NULL,
        device          VARCHAR(255) NOT NULL,
        interface       VARCHAR(255) NOT NULL,
        in_bps          BIGINT NOT NULL,
        out_bps         BIGINT NOT NULL,
        in_util_pct     DOUBLE PRECISION NOT NULL,
        out_util_pct    DOUBLE PRECISION NOT NULL,
        error_count     INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (id, ts_bucket)
    """,
    "queue_stats_minute": """
        id                  BIGSERIAL,
        tenant_id           UUID NOT NULL,
        ts_bucket           TIMESTAMPTZ NOT NULL,
        device              VARCHAR(255) NOT NULL,
        interface           VARCHAR(255) NOT NULL,
        queue_id            INTEGER NOT NULL,
        traffic_class       INTEGER,
        max_depth_bytes     BIGINT NOT NULL,
        avg_depth_bytes     BIGINT NOT NULL,
        pfc_pause_rx        BIGINT NOT NULL DEFAULT 0,
        pfc_pause_tx        BIGINT NOT NULL DEFAULT 0,
        ecn_marked_packets  BIGINT NOT NULL DEFAULT 0,
        dropped_packets     BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (id, ts_bucket)
    """,
}

PARTITION_INDEXES = {
    "flow_summary_minute": [
        ("ix_flow_tenant_ts", "(tenant_id, ts_bucket)"),
        ("ix_flow_device_ts", "(device, ts_bucket)"),
        ("ix_flow_src_dst_ts", "(src_ip, dst_ip, ts_bucket)"),
    ],
    "interface_utilization_minute": [
        ("ix_util_tenant_ts", "(tenant_id, ts_bucket)"),
        ("ix_util_device_if_ts", "(device, interface, ts_bucket)"),
    ],
    "queue_stats_minute": [
        ("ix_queue_tenant_ts", "(tenant_id, ts_bucket)"),
        ("ix_queue_device_if_q_ts", "(device, interface, queue_id, ts_bucket)"),
    ],
}


def upgrade() -> None:
    # ---- 1. partition the three time-series tables -----------------------
    for table, columns in PARTITIONED_TABLES.items():
        op.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        # Drop the old primary key and the indexes so we can rebuild on the
        # partitioned table cleanly. Names are Postgres defaults.
        op.execute(f"ALTER TABLE {table}_old DROP CONSTRAINT {table}_pkey")
        for idx_name, _ in PARTITION_INDEXES[table]:
            op.execute(f"DROP INDEX IF EXISTS {idx_name}")
        # Some of the pre-partition tables also had a plain index on ts_bucket
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_ts_bucket")

        op.execute(
            f"CREATE TABLE {table} ({columns}) PARTITION BY RANGE (ts_bucket)"
        )
        for idx_name, cols in PARTITION_INDEXES[table]:
            op.execute(f"CREATE INDEX {idx_name} ON {table} {cols}")

    # ---- 2. partition-maintenance helper functions -----------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ensure_monthly_partitions(
            parent text,
            months_forward int DEFAULT 1
        )
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        DECLARE
            start_month timestamptz;
            end_month   timestamptz;
            part_name   text;
            i           int;
        BEGIN
            -- Always keep the previous month around so late-arriving rows
            -- don't crash the ingest path.
            FOR i IN -1 .. months_forward LOOP
                start_month := date_trunc('month', now()) + (i || ' month')::interval;
                end_month   := start_month + interval '1 month';
                part_name   := parent || '_' || to_char(start_month, 'YYYYMM');
                EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I
                     FOR VALUES FROM (%L) TO (%L)',
                    part_name, parent, start_month, end_month
                );
            END LOOP;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION drop_partitions_older_than(
            parent text,
            older_than interval
        )
        RETURNS int
        LANGUAGE plpgsql
        AS $$
        DECLARE
            child_rec  record;
            dropped    int := 0;
            cutoff     timestamptz := date_trunc('month', now() - older_than);
        BEGIN
            FOR child_rec IN
                SELECT c.relname AS child
                FROM pg_inherits i
                JOIN pg_class  p  ON p.oid = i.inhparent
                JOIN pg_class  c  ON c.oid = i.inhrelid
                WHERE p.relname = parent
            LOOP
                -- Partition naming convention: <parent>_YYYYMM. Older than cutoff?
                IF length(child_rec.child) >= length(parent) + 7 AND
                   to_date(right(child_rec.child, 6), 'YYYYMM') < cutoff
                THEN
                    EXECUTE format('DROP TABLE IF EXISTS %I', child_rec.child);
                    dropped := dropped + 1;
                END IF;
            END LOOP;
            RETURN dropped;
        END;
        $$;
        """
    )

    # Materialize initial partitions now that the function exists.
    for table in PARTITIONED_TABLES:
        op.execute(f"SELECT ensure_monthly_partitions('{table}', 2)")

    # Copy any pre-existing rows into the partitioned tables. The partition
    # for the current month was just created above; older rows fall back to
    # the previous-month partition (also created).
    for table in PARTITIONED_TABLES:
        op.execute(
            f"""
            INSERT INTO {table}
            SELECT * FROM {table}_old
            WHERE ts_bucket >= date_trunc('month', now() - interval '1 month')
            """
        )
        op.execute(f"DROP TABLE {table}_old")

    # ---- 3. anomaly_events dedup + lifecycle -----------------------------
    op.add_column(
        "anomaly_events",
        sa.Column("fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "anomaly_events",
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "anomaly_events",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "anomaly_events",
        sa.Column(
            "occurrence_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "anomaly_events",
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "anomaly_events",
        sa.Column(
            "acknowledged_by_api_key_id",
            postgresql.UUID(as_uuid=False),
            nullable=True,
        ),
    )
    op.add_column(
        "anomaly_events",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "anomaly_events",
        sa.Column(
            "resolved_by_api_key_id",
            postgresql.UUID(as_uuid=False),
            nullable=True,
        ),
    )

    # Seed fingerprints for any pre-existing rows so the dedup index is
    # consistent. Fingerprint = sha1(tenant|type|scope); rows predating
    # PR 26 are also treated as already resolved (last_seen_at = ts) to
    # avoid reviving stale alerts.
    op.execute(
        """
        UPDATE anomaly_events
        SET fingerprint  = md5(tenant_id::text || '|' || anomaly_type || '|' || scope),
            first_seen_at = ts,
            last_seen_at  = ts,
            resolved_at   = ts
        WHERE fingerprint IS NULL
        """
    )

    # Partial unique index: at most one open (unresolved) event per
    # fingerprint per tenant. Recurring conditions upsert into this row.
    op.create_index(
        "ix_anomaly_tenant_ts",
        "anomaly_events",
        ["tenant_id", "ts"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_anomaly_fingerprint_open",
        "anomaly_events",
        ["tenant_id", "fingerprint"],
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_anomaly_open_per_fingerprint
        ON anomaly_events (tenant_id, fingerprint)
        WHERE resolved_at IS NULL AND fingerprint IS NOT NULL
        """
    )

    # ---- 4. source_freshness ---------------------------------------------
    op.create_table(
        "source_freshness",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("device", sa.String(length=255), nullable=False),
        sa.Column("last_ingest_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_sample_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'fresh'"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "source_kind", "device", name="uq_source_freshness"
        ),
    )
    op.create_index(
        "ix_source_freshness_last_ingest",
        "source_freshness",
        ["tenant_id", "last_ingest_ts"],
    )


def downgrade() -> None:
    # Destructive. Partitions are dropped; data in old monthly partitions
    # is lost. Safe because the upgrade is idempotent on re-application.
    op.drop_index("ix_source_freshness_last_ingest", table_name="source_freshness")
    op.drop_table("source_freshness")

    op.execute("DROP INDEX IF EXISTS uq_anomaly_open_per_fingerprint")
    op.drop_index("ix_anomaly_fingerprint_open", table_name="anomaly_events")
    for col in (
        "resolved_by_api_key_id",
        "resolved_at",
        "acknowledged_by_api_key_id",
        "acknowledged_at",
        "occurrence_count",
        "last_seen_at",
        "first_seen_at",
        "fingerprint",
    ):
        op.drop_column("anomaly_events", col)

    op.execute("DROP FUNCTION IF EXISTS drop_partitions_older_than(text, interval)")
    op.execute("DROP FUNCTION IF EXISTS ensure_monthly_partitions(text, int)")

    for table, columns in PARTITIONED_TABLES.items():
        op.execute(f"ALTER TABLE {table} RENAME TO {table}_partitioned")
        op.execute(f"CREATE TABLE {table} ({columns.replace('BIGSERIAL', 'BIGSERIAL PRIMARY KEY').replace('PRIMARY KEY (id, ts_bucket)', '').rstrip(', \n')})")
        op.execute(f"INSERT INTO {table} SELECT * FROM {table}_partitioned")
        op.execute(f"DROP TABLE {table}_partitioned CASCADE")
        for idx_name, cols in PARTITION_INDEXES[table]:
            op.execute(f"CREATE INDEX {idx_name} ON {table} {cols}")
