import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from collectors.sflow_rt_client import SFlowRTClient
from routers import flows as flows_router
from routers import interfaces as interfaces_router
from services.anomalies import anomaly_loop
from services.baselines import baseline_loop
from services.ingest import ingestion_loop

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = SFlowRTClient(os.getenv("SFLOW_RT_URL", "http://localhost:8008"))
    ingest_task = asyncio.create_task(ingestion_loop(client))
    baseline_task = asyncio.create_task(baseline_loop())
    anomaly_task = asyncio.create_task(anomaly_loop())
    log.info("Ingestion, baseline, and anomaly loops started")
    try:
        yield
    finally:
        ingest_task.cancel()
        baseline_task.cancel()
        anomaly_task.cancel()
        await client.close()


app = FastAPI(title="FlowMind Telemetry API", lifespan=lifespan)

app.include_router(flows_router.router)
app.include_router(interfaces_router.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
