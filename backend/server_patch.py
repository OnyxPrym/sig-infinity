"""
PATCH FOR server.py
===================
Add these blocks to your EXISTING server.py. Do not replace the file.

1) Add to the top imports:
       import atexit
       import signal
       import sys

2) Add immediately after `DB_PATH = ...`:
       (see _checkpoint_and_close + signal handlers below)

3) Replace the existing `result_tracker_loop()` function with the one below.
"""

# ---- 1) Add to imports ----
import atexit
import signal
import sys


# ---- 2) Add after DB_PATH = ... ----
def _checkpoint_and_close():
    """Force WAL checkpoint so sig_infinity.db is complete on shutdown."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        print("[shutdown] WAL checkpoint done")
    except Exception as e:
        print(f"[shutdown] WAL checkpoint failed: {e}")


atexit.register(_checkpoint_and_close)


def _sig_handler(signum, frame):
    _checkpoint_and_close()
    sys.exit(0)


signal.signal(signal.SIGTERM, _sig_handler)
signal.signal(signal.SIGINT, _sig_handler)


# ---- 3) Replace result_tracker_loop ----
def result_tracker_loop():
    """Background thread -- resolves pending signals every 10s and
    checkpoints the WAL every ~10 minutes."""
    counter = 0
    while True:
        try:
            resolve_pending_user_signals()
            counter += 1
            if counter % 60 == 0:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                conn.close()
        except Exception as e:
            print(f"[results] error: {e}")
        time.sleep(10)