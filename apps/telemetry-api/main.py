import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from collectors.gnmi_client import GNMIClient
from collectors.sflow_rt_client import SFlowRTClient
from db import AsyncSessionLocal
from services.metrics import render_prometheus
from services.rls_session import bypass_rls
from middleware.auth import APIKeyMiddleware
from otel import setup_telemetry
from shared.logging import configure_logging
from routers import admin as admin_router
from routers import anomalies as anomalies_router
from routers import tool_audit as tool_audit_router
from routers import devices as devices_router
from routers import fabric as fabric_router
from routers import flows as flows_router
from routers import intent as intent_router
from routers import interfaces as interfaces_router
from routers import rdma as rdma_router
from routers import topology as topology_router
from routers import traffic as traffic_router
from services.anomalies import anomaly_loop
from services.baselines import baseline_loop
from services.gnmi_ingest import gnmi_ingestion_loop
from services.ingest import ingestion_loop
from services.partition_maintenance import partition_maintenance_loop
from services.source_freshness_loop import source_freshness_loop
from services.webhook_dispatcher import webhook_dispatcher_loop

configure_logging("flowmind-telemetry-api", level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    sflow = SFlowRTClient(os.getenv("SFLOW_RT_URL", "http://localhost:8008"))
    gnmi = GNMIClient()
    ingest_task = asyncio.create_task(ingestion_loop(sflow))
    gnmi_task = asyncio.create_task(gnmi_ingestion_loop(gnmi))
    baseline_task = asyncio.create_task(baseline_loop())
    anomaly_task = asyncio.create_task(anomaly_loop())
    partition_task = asyncio.create_task(partition_maintenance_loop())
    freshness_task = asyncio.create_task(source_freshness_loop())
    webhook_task = asyncio.create_task(webhook_dispatcher_loop())
    log.info(
        "ingestion, baseline, anomaly, partition-maintenance, "
        "source-freshness, and webhook-dispatcher loops started"
    )
    try:
        yield
    finally:
        for t in (
            ingest_task,
            gnmi_task,
            baseline_task,
            anomaly_task,
            partition_task,
            freshness_task,
            webhook_task,
        ):
            t.cancel()
        await sflow.close()
        await gnmi.close()


app = FastAPI(title="FlowMind Telemetry API", lifespan=lifespan)
setup_telemetry(app)
app.add_middleware(APIKeyMiddleware)

app.include_router(flows_router.router)
app.include_router(interfaces_router.router)
app.include_router(anomalies_router.router)
app.include_router(traffic_router.router)
app.include_router(topology_router.router)
app.include_router(devices_router.router)
app.include_router(rdma_router.router)
app.include_router(fabric_router.router)
app.include_router(intent_router.router)
app.include_router(admin_router.router)
app.include_router(tool_audit_router.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    """Prometheus exposition. Reads cross-tenant by design (operator view).

    Mounted directly on the app (not behind APIKeyMiddleware) because
    Prom scrapes via in-cluster scrape configs that won't carry an
    API key. The middleware's EXEMPT_PATHS list includes ``/metrics``.
    Body is text/plain per Prometheus exposition spec.
    """
    async with AsyncSessionLocal() as session:
        async with bypass_rls(session):
            body = await render_prometheus(session)
    return Response(content=body, media_type="text/plain; version=0.0.4")
