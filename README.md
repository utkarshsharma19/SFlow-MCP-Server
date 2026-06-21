# FlowMind — Network Intelligence MCP

Turn your network fabric into an AI-callable brain.

FlowMind is a network telemetry MCP (Model Context Protocol) server. It sits between a network fabric and any LLM-based agent, exposing decision-ready tools rather than raw telemetry.

## Status

v1 in active development. See `docs/FlowMind_PRGuide.docx` for the original 12-PR build plan; PRs 23–30 layered on RDMA assessment, multi-tenancy, MCP audit/quotas, structured logs, and the enterprise wave 3 features in this README.

## Architecture (v1)

Four-layer pipeline:

1. **Ingestion** — sFlow-RT collector (Docker) polled over RESTflow, gNMI/OpenConfig poll-mode targets, and an optional Verity orchestrator pull
2. **Normalization** — FastAPI service that buckets, sampling-corrects, and stores flows + counters + device state in Postgres (partitioned monthly)
3. **Intelligence** — MCP server exposing bounded, read-only (and a few write) tools
4. **Consumption** — Any MCP-compatible client (Claude Desktop, Cursor, custom agents, chat gateways)

## MCP Tools

Read tools:

| Tool | Question it answers |
|---|---|
| `get_fabric_health` | How healthy is the fabric overall? |
| `get_top_offenders` | What should I look at first this morning? |
| `get_top_talkers` | Who is generating the most traffic? |
| `get_interface_utilization` | How loaded is this link right now? |
| `get_link_history` | What happened on this link over the last hour? |
| `compare_traffic_windows` | What changed vs. baseline? |
| `summarize_protocol_mix` | What protocols are running? |
| `explain_hot_link` | Why is this interface saturated? |
| `get_recent_anomalies` | Is something wrong right now? |
| `summarize_anomalies` | Narrative summary for shift handoff |
| `get_device_state` | Is the device healthy? (gNMI) |
| `get_device_neighbors` | What's plugged into this device? (LLDP) |
| `get_rdma_health` | Is my RoCE/RDMA training fabric healthy? |
| `detect_fabric_imbalance` | Is one ECMP member doing all the work? |
| `find_path` | What hops does a src→dst flow traverse? |
| `diff_config_intent_vs_state` | Where does live state drift from declared intent? |

Write tools (operator role):

| Tool | Action |
|---|---|
| `acknowledge_anomaly` | Acknowledge or resolve an anomaly_event by id |

Every tool response includes a `confidence_note` describing sampling coverage (or noting that gNMI snapshots are exact).

## Testbed quickstart (≈ 10 minutes)

1. **Clone + bring up the stack.**
   ```bash
   git clone <this-repo> && cd SFlow-MCP-Server
   cp .env.example .env                 # optional; defaults work for dev
   docker compose up -d --build
   ```
   First boot runs `alembic upgrade head` inside the telemetry API container, so the schema is ready before uvicorn starts.

2. **Confirm liveness.**
   ```bash
   curl -s http://localhost:8080/health
   curl -s http://localhost:8080/metrics | head -20
   curl -s http://localhost:8008/version  # sFlow-RT
   ```

3. **Point your testbed at the sFlow listener.** Configure your switches to send sFlow datagrams to `udp:<host>:6343`. Within ~30 seconds you should see flows in:
   ```bash
   curl -s "http://localhost:8080/flows/top-talkers?window_minutes=5" \
        -H "X-API-Key: dev-insecure-key" | jq
   ```

4. **(Optional) Issue a real tenant + API key for the testbed.**
   ```bash
   docker compose exec telemetry-api \
     python -m scripts.seed create-tenant --slug acme --name "Acme Testbed"
   docker compose exec telemetry-api \
     python -m scripts.seed create-key   --tenant-slug acme \
       --role operator --name "testbed-key"
   ```
   The key is printed once. Export it: `export FM_KEY=fm_…`.

5. **Map your sFlow source(s) to the tenant.** Otherwise every flow lands in the default tenant.
   ```bash
   docker compose exec telemetry-api \
     python -m scripts.seed map-source --kind sflow \
       --identifier 10.0.1.5 --tenant-slug acme
   ```

6. **Connect the MCP server.** With Claude Desktop or any MCP client, hit `http://localhost:8090/mcp` (streamable HTTP transport). For a quick ad-hoc check:
   ```bash
   npx @modelcontextprotocol/inspector \
     -e TELEMETRY_API_KEY="$FM_KEY" \
     -e TELEMETRY_API_URL=http://localhost:8080 \
     -- python apps/mcp-server/server.py
   ```

7. **(Optional) Wire push notifications.** Register a webhook so the chatbot/pager gets called on `critical` anomalies:
   ```bash
   docker compose exec telemetry-api \
     python -m scripts.seed create-webhook --tenant-slug acme \
       --target-url https://your.chatbot/anomalies --severity-min critical
   ```
   The secret is printed once. Receiver verifies via
   `X-FlowMind-Signature: sha256=HMAC-SHA256(secret, body)`.

8. **(Optional) Hook up Verity.** Set `VERITY_BASE_URL` + `VERITY_TOKEN` in `.env`, map the source, then `docker compose restart telemetry-api`. Intent diff lights up automatically.

## Endpoints reference

| URL | Purpose |
|---|---|
| `http://localhost:8080/health` | Liveness probe |
| `http://localhost:8080/metrics` | Prometheus exposition |
| `http://localhost:8080/docs` | OpenAPI Swagger UI |
| `http://localhost:8090/mcp` | MCP streamable-HTTP transport |
| `http://localhost:6343/udp` | sFlow agent destination |
| `http://localhost:16686` | Jaeger UI (traces) |

## Hardening switches (enterprise wave 3, PR 30)

- **Distributed rate limiter** — set `REDIS_URL` (already wired in compose). Without Redis, MCP rate limits are per-process only.
- **MCP circuit breaker** — tune via `MCP_CB_FAILURE_THRESHOLD` / `MCP_CB_COOLDOWN_SECONDS`. 4xx errors do NOT count as faults.
- **Token-budget quota** — chatbot gateways call `POST /tool-audit/charge-tokens` with the per-turn LLM token count. Set `token_limit` via `POST /tool-audit/quota`.
- **Webhook delivery** — register subscriptions via `seed.py create-webhook`. Dispatcher signs with HMAC-SHA256 and idempotency-keys on `(subscription_id, anomaly_id)` so restarts don't double-page.
- **Encryption at rest** — set `FLOWMIND_DATA_KEY`. All `encrypted_secrets` (gNMI passwords, webhook signing keys) go through pgcrypto.

## Local development

```
docker compose up
```

Starts sFlow-RT, Postgres, Redis, Jaeger, the telemetry API, and the MCP server.

Or just the deps and run Python locally:
```
docker compose up -d postgres redis sflow-rt jaeger
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cd apps/telemetry-api && DATABASE_URL=postgresql+asyncpg://flowmind:flowmind_dev@localhost/flowmind \
   ../../.venv/bin/python -m alembic upgrade head
```

## Testing

```
.venv/bin/python -m pytest -q
```

For an interactive MCP protocol test:
```
npx @modelcontextprotocol/inspector .venv/bin/python apps/mcp-server/server.py
```

## Docs

- `docs/FlowMind_Pitch.docx` — Problem, solution, target market
- `docs/FlowMind_TechSpec.docx` — Full architecture + tool contracts
- `docs/FlowMind_PRGuide.docx` — Original 12-PR build plan
