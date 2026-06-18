import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Response

from collectors.gnmi_client import GNMIClient
from collectors.sflow_rt_client import SFlowRTClient
from db import AsyncSessionLocal
from services.metrics import render_prometheus
from services.rls_session import bypass_rls
from middleware.auth import APIKeyMiddleware
from otel import setup_telemetry
from shared.logging import configure_logging
from routers import admin as admin_router
from routers import admin_setup as admin_setup_router
from routers import anomalies as anomalies_router
from routers import chat as chat_router
from routers import chat_user_keys as chat_user_keys_router
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
from services.verity_ingest import verity_ingest_loop
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
    verity_task = asyncio.create_task(verity_ingest_loop())
    log.info(
        "ingestion, baseline, anomaly, partition-maintenance, "
        "source-freshness, webhook-dispatcher, and verity-ingest loops started"
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
            verity_task,
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
app.include_router(admin_setup_router.router)
app.include_router(tool_audit_router.router)
app.include_router(chat_router.router)
app.include_router(chat_user_keys_router.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics_endpoint(
    authorization: str | None = Header(default=None),
) -> Response:
    """Prometheus exposition. Reads cross-tenant by design (operator view).

    Mounted directly on the app (not behind the tenant-aware
    APIKeyMiddleware) because Prometheus scrape configs carry one
    static credential, not a per-tenant key. The path is in
    ``APIKeyMiddleware.EXEMPT_PATHS`` for the same reason.

    Auth: when ``FLOWMIND_METRICS_TOKEN`` is set in the environment,
    the request MUST carry ``Authorization: Bearer <token>``. Prometheus
    supports this out of the box via ``bearer_token_file`` in the
    scrape config. When the env is unset the endpoint is open — a
    development convenience that must not ship to prod.

    Output: ``text/plain; version=0.0.4`` per the Prometheus exposition
    spec, with cross-tenant rollups via ``bypass_rls``.
    """
    expected = os.getenv("FLOWMIND_METRICS_TOKEN")
    if expected:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail="metrics endpoint requires Authorization: Bearer <token>",
                headers={"WWW-Authenticate": 'Bearer realm="flowmind-metrics"'},
            )
        presented = authorization[len("Bearer "):].strip()
        if presented != expected:
            raise HTTPException(status_code=403, detail="invalid metrics token")

    async with AsyncSessionLocal() as session:
        async with bypass_rls(session):
            body = await render_prometheus(session)
    return Response(content=body, media_type="text/plain; version=0.0.4")
