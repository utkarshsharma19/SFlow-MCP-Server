FROM python:3.11-slim

WORKDIR /app

COPY apps/mcp-server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/mcp-server/ /app/apps/mcp-server/
COPY shared /app/shared/

ENV PYTHONPATH=/app
WORKDIR /app/apps/mcp-server

ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8090

EXPOSE 8090
CMD ["python", "server.py", "--transport", "streamable-http"]
