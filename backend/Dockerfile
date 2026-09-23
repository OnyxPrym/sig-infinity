FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

ENV PYTHONUNBUFFERED=1 \
    PORT=10000 \
    DB_PATH=/data/sig_infinity.db \
    REFRESH_INTERVAL_SECONDS=7200

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS http://localhost:10000/api/status || exit 1

RUN chmod +x entrypoint.sh
CMD ["./entrypoint.sh"]