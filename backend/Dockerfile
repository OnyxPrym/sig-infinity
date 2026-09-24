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
    DB_PATH=/tmp/sig_infinity.db \
    REFRESH_INTERVAL_SECONDS=7200

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS http://localhost:10000/api/status || exit 1

RUN printf '%s' 'IyEvYmluL3NoCnNldCAtZQoKZWNobyAiW2VudHJ5cG9pbnRdIFN0YXJ0aW5nIHNlcnZlci4uLiIKCmV4ZWMgcHl0aG9uIC1jICIKaW1wb3J0IHRocmVhZGluZywgb3MsIHRpbWUKaW1wb3J0IGF1dG9fcmVmcmVzaAppbXBvcnQgc2VydmVyCmltcG9ydCBxdW90ZXhfY29sbGVjdG9yIGFzIHFjCgojIFNURVAgMTogTG9hZCBzZXNzaW9uIGZyb20gZW52IEZJUlNUICh3cml0ZXMgL2FwcC9zZXNzaW9uLmpzb24pCnByaW50KCdbZW50cnlwb2ludF0gTG9hZGluZyBzZXNzaW9uIGZyb20gZW52Li4uJykKdHJ5OgogICAgbG9hZGVkID0gYXV0b19yZWZyZXNoLmxvYWRfZW52X3Nlc3Npb24oKQogICAgcHJpbnQoZidbZW50cnlwb2ludF0gU2Vzc2lvbiBsb2FkZWQgZnJvbSBlbnY6IHtsb2FkZWR9JykKZXhjZXB0IEV4Y2VwdGlvbiBhcyBlOgogICAgcHJpbnQoZidbZW50cnlwb2ludF0gU2Vzc2lvbiBsb2FkIGVycm9yOiB7ZX0nKQoKIyBTVEVQIDI6IEdpdmUgaXQgYSBtb21lbnQgdG8gd3JpdGUgdGhlIGZpbGUKdGltZS5zbGVlcCgwLjUpCgojIFNURVAgMzogTk9XIHN0YXJ0IHRoZSBjb2xsZWN0b3IgKHNlc3Npb24uanNvbiBleGlzdHMpCnByaW50KCdbZW50cnlwb2ludF0gU3RhcnRpbmcgY29sbGVjdG9yIHRocmVhZC4uLicpCnFjLnN0YXJ0X2NvbGxlY3Rvcl90aHJlYWQoKQpwcmludCgnW2VudHJ5cG9pbnRdIENvbGxlY3RvciB0aHJlYWQgc3RhcnRlZCcpCgojIFNURVAgNDogU3RhcnQgYXV0by1yZWZyZXNoIChiYWNrZ3JvdW5kKQp0aHJlYWRpbmcuVGhyZWFkKHRhcmdldD1hdXRvX3JlZnJlc2gucmVmcmVzaF9sb29wLCBkYWVtb249VHJ1ZSwgbmFtZT0nQXV0b1JlZnJlc2gnKS5zdGFydCgpCnByaW50KCdbZW50cnlwb2ludF0gQXV0by1yZWZyZXNoIHRocmVhZCBzdGFydGVkJykKCiMgU1RFUCA1OiBSdW4gRmxhc2sKc2VydmVyLmFwcC5ydW4oaG9zdD0nMC4wLjAuMCcsIHBvcnQ9aW50KG9zLmVudmlyb24uZ2V0KCdQT1JUJywgMTAwMDApKSwgZGVidWc9RmFsc2UpCiIK' | base64 -d > /app/entrypoint.sh && \
    chmod +x /app/entrypoint.sh && \
    head -1 /app/entrypoint.sh && \
    echo '--- entrypoint.sh written ---'

CMD ["/app/entrypoint.sh"]