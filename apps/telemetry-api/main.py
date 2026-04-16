from fastapi import FastAPI

app = FastAPI(title="FlowMind Telemetry API")


@app.get("/health")
async def health():
    return {"status": "ok"}
