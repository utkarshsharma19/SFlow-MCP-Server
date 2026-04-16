FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/apps/mcp-server/
COPY shared /app/shared/

ENV PYTHONPATH=/app
WORKDIR /app/apps/mcp-server

EXPOSE 8090
CMD ["python", "server.py"]
