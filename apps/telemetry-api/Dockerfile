FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/apps/telemetry-api/
COPY shared /app/shared/

ENV PYTHONPATH=/app
WORKDIR /app/apps/telemetry-api

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
