"""Local SQLite only — Turso replaced."""
import sqlite3
from pathlib import Path

_local_path = None

def set_local_fallback_path(path: str) -> None:
    global _local_path
    _local_path = path
    print(f"[db] Local SQLite: {path}")

def connect():
    if not _local_path:
        raise RuntimeError("set_local_fallback_path() not called")
    return sqlite3.connect(_local_path, timeout=30, check_same_thread=False)

def is_turso_enabled() -> bool:
    return False

def sync(conn) -> None:
    pass

def checkpoint_and_close(conn) -> None:
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except Exception:
        pass