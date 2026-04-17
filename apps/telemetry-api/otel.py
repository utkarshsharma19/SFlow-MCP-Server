"""OpenTelemetry wiring for the telemetry API.

Exports traces and metrics via OTLP/gRPC to the endpoint in
OTEL_EXPORTER_OTLP_ENDPOINT (default: Jaeger all-in-one at
http://jaeger:4317). If OTel packages are missing we log and
no-op — the API remains functional without observability in
environments where the optional deps aren't installed.

Custom metrics:
- flowmind.flows_ingested_total  (counter, attribute: ok|error)
- flowmind.anomalies_detected_total (counter, attribute: severity)
- flowmind.ingestion_duration_seconds (histogram, attribute: phase)
- flowmind.sflow_rt_up (observable gauge 1/0)
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4317")
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "flowmind-telemetry-api")

flows_ingested = None
anomalies_detected = None
ingestion_duration = None
_sflow_up = 1


def set_sflow_up(ok: bool) -> None:
    global _sflow_up
    _sflow_up = 1 if ok else 0


def setup_telemetry(app=None) -> None:
    global flows_ingested, anomalies_detected, ingestion_duration
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import SERVICE_NAME as RES_SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        log.warning(
            f"OpenTelemetry packages not installed ({e}); "
            f"install the 'otel' extra to enable tracing/metrics."
        )
        return

    resource = Resource.create({RES_SERVICE_NAME: SERVICE_NAME})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True))
    )
    trace.set_tracer_provider(tracer_provider)

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=OTEL_ENDPOINT, insecure=True)
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    if app is not None:
        FastAPIInstrumentor.instrument_app(app)
    SQLAlchemyInstrumentor().instrument()

    meter = metrics.get_meter("flowmind.telemetry-api")
    flows_ingested = meter.create_counter(
        "flowmind.flows_ingested_total",
        description="Total flow records ingested",
    )
    anomalies_detected = meter.create_counter(
        "flowmind.anomalies_detected_total",
        description="Total anomaly events detected",
    )
    ingestion_duration = meter.create_histogram(
        "flowmind.ingestion_duration_seconds",
        description="Time taken for one ingestion cycle",
    )
    meter.create_observable_gauge(
        "flowmind.sflow_rt_up",
        callbacks=[lambda _options: [metrics.Observation(_sflow_up)]],
        description="sFlow-RT reachability (1=up, 0=down)",
    )

    log.info(f"OpenTelemetry initialized (endpoint={OTEL_ENDPOINT})")


def get_tracer(name: str):
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    return trace.get_tracer(name)
