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
RUN printf '%s' 'IyEvYmluL3NoCnNldCAtZQoKZWNobyAiW2VudHJ5cG9pbnRdIFN0YXJ0aW5nIHNlcnZlciB3aXRoIGJhY2tncm91bmQgYXV0by1yZWZyZXNoLi4uIgoKZXhlYyBweXRob24gLWMgIgppbXBvcnQgdGhyZWFkaW5nLCBvcwppbXBvcnQgYXV0b19yZWZyZXNoCmltcG9ydCBzZXJ2ZXIKaW1wb3J0IHF1b3RleF9jb2xsZWN0b3IgYXMgcWMKCiMgU1RBUlQgVEhFIENPTExFQ1RPUiBUSFJFQUQgKGNyaXRpY2FsKQpxYy5zdGFydF9jb2xsZWN0b3JfdGhyZWFkKCkKcHJpbnQoJ1tlbnRyeXBvaW50XSBDb2xsZWN0b3IgdGhyZWFkIHN0YXJ0ZWQnKQoKIyBTVEFSVCBBVVRPLVJFRlJFU0ggVEhSRUFECnRocmVhZGluZy5UaHJlYWQodGFyZ2V0PWF1dG9fcmVmcmVzaC5yZWZyZXNoX2xvb3AsIGRhZW1vbj1UcnVlLCBuYW1lPSdBdXRvUmVmcmVzaCcpLnN0YXJ0KCkKcHJpbnQoJ1tlbnRyeXBvaW50XSBBdXRvLXJlZnJlc2ggdGhyZWFkIHN0YXJ0ZWQnKQoKIyBSVU4gRkxBU0sKc2VydmVyLmFwcC5ydW4oaG9zdD0nMC4wLjAuMCcsIHBvcnQ9aW50KG9zLmVudmlyb24uZ2V0KCdQT1JUJywgMTAwMDApKSwgZGVidWc9RmFsc2UpCiIK' | base64 -d > /app/entrypoint.sh && \
    chmod +x /app/entrypoint.sh && \
    head -1 /app/entrypoint.sh && \
    echo '--- entrypoint.sh written ---'

CMD ["/app/entrypoint.sh"]