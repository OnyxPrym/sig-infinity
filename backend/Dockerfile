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

# Generate entrypoint.sh from base64 at build time (bypasses git line-ending corruption)
RUN printf '%s' 'IyEvYmluL3NoCnNldCAtZQoKZWNobyAiW2VudHJ5cG9pbnRdIFN0YXJ0aW5nIHNlcnZlciB3aXRoIGJhY2tncm91bmQgYXV0by1yZWZyZXNoLi4uIgpleGVjIHB5dGhvbiAtYyAiCmltcG9ydCB0aHJlYWRpbmcsIG9zCmltcG9ydCBhdXRvX3JlZnJlc2gKaW1wb3J0IHNlcnZlcgp0aHJlYWRpbmcuVGhyZWFkKHRhcmdldD1hdXRvX3JlZnJlc2gucmVmcmVzaF9sb29wLCBkYWVtb249VHJ1ZSwgbmFtZT0nQXV0b1JlZnJlc2gnKS5zdGFydCgpCnNlcnZlci5hcHAucnVuKGhvc3Q9JzAuMC4wLjAnLCBwb3J0PWludChvcy5lbnZpcm9uLmdldCgnUE9SVCcsIDEwMDAwKSksIGRlYnVnPUZhbHNlKQoiCg==' | base64 -d > /app/entrypoint.sh && \
    chmod +x /app/entrypoint.sh && \
    head -1 /app/entrypoint.sh && \
    echo '--- entrypoint.sh written ---'

CMD ["/app/entrypoint.sh"]