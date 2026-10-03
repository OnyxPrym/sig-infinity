#!/bin/sh
set -e

echo "[entrypoint] Starting sync daemon in background..."
python /app/sync_daemon.py &

echo "[entrypoint] Starting server..."
exec python -c "
import threading, os
import auto_refresh
import server
import quotex_collector as qc

qc.start_collector_thread()
threading.Thread(target=auto_refresh.refresh_loop, daemon=True, name='AutoRefresh').start()
server.app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 7860)), debug=False)
"