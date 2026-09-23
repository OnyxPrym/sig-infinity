#!/bin/sh
# Nightly SQLite backup - keeps 7 days. Add to VPS crontab:
#   0 3 * * * /home/ubuntu/sig-backend/backup.sh
set -e
docker exec sig-api sqlite3 /data/sig_infinity.db ".backup /data/backup_$(date +%Y%m%d).db"
docker exec sig-api sh -c 'ls -t /data/backup_*.db | tail -n +8 | xargs -r rm'
echo "[backup] done $(date)"