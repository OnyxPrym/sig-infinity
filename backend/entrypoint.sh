#!/bin/sh
set -e

echo "[entrypoint] Starting server with background auto-refresh..."
exec python -c "
import threading, os
import auto_refresh
import server
threading.Thread(target=auto_refresh.refresh_loop, daemon=True, name='AutoRefresh').start()
server.app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)), debug=False)
"
