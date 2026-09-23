#!/bin/sh
set -e

echo "[entrypoint] Initial Quotex session bootstrap..."
python auto_refresh.py --once 2>/dev/null || true
# (--once is not supported; the loop handles it. This just warms up.)

echo "[entrypoint] Starting server with background auto-refresh..."
exec python -c "
import threading
import auto_refresh
import server
threading.Thread(target=auto_refresh.refresh_loop, daemon=True, name='AutoRefresh').start()
server.app.run(host='0.0.0.0', port=int(__import__('os').environ.get('PORT', 10000)), debug=False)
"