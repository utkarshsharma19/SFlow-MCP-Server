import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from collectors.sflow_rt_client import SFlowRTClient
from services.ingest import ingestion_loop

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = SFlowRTClient(os.getenv("SFLOW_RT_URL", "http://localhost:8008"))
    task = asyncio.create_task(ingestion_loop(client))
    log.info("Ingestion loop started")
    try:
        yield
    finally:
        task.cancel()
        await client.close()


app = FastAPI(title="FlowMind Telemetry API", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}
