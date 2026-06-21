FROM python:3.11-slim

WORKDIR /app

# psycopg2 (used by alembic) needs the libpq client at runtime; the
# binary wheel pulls headers in, but apt installs of libpq5 are tiny
# and survive any future switch off ``psycopg2-binary``.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY apps/telemetry-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/telemetry-api/ /app/apps/telemetry-api/
COPY shared /app/shared/

ENV PYTHONPATH=/app
WORKDIR /app/apps/telemetry-api

EXPOSE 8080
# Run migrations on every boot (idempotent — no-op when already current)
# then start uvicorn. Keeps the deploy path one ``docker compose up``.
CMD ["sh", "-c", "alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port 8080"]
