# FlowMind — Network Intelligence MCP

Turn your network fabric into an AI-callable brain.

FlowMind is a network telemetry MCP (Model Context Protocol) server. It sits between a network fabric and any LLM-based agent, exposing decision-ready tools rather than raw telemetry.

## Status

v1 in active development. See `docs/FlowMind_PRGuide.docx` for the 12-PR build plan.

## Architecture (v1)

Four-layer pipeline:

1. **Ingestion** — sFlow-RT collector (Docker), polled over RESTflow
2. **Normalization** — FastAPI service that buckets, sampling-corrects, and stores flows + counters in Postgres
3. **Intelligence** — MCP server exposing bounded, read-only tools
4. **Consumption** — Any MCP-compatible client (Claude Desktop, Cursor, custom agents)

## MCP Tools (v1)

- `get_top_talkers` — Who is generating the most traffic?
- `get_interface_utilization` — How loaded is this link?
- `compare_traffic_windows` — What changed vs baseline?
- `get_recent_anomalies` — Is something wrong right now?
- `summarize_protocol_mix` — What protocols are running?
- `explain_hot_link` — Why is this interface saturated?

Every tool response includes a `confidence_note` describing sampling coverage.

## Local development

```
docker compose up
```

Starts sFlow-RT, Postgres, Redis, the telemetry API, and the MCP server.

## Docs

- `docs/FlowMind_Pitch.docx` — Problem, solution, target market
- `docs/FlowMind_TechSpec.docx` — Full architecture + tool contracts
- `docs/FlowMind_PRGuide.docx` — 12-PR build plan with code and acceptance criteria
