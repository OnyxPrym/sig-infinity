"""
Turso-backed database helper.

Drop-in replacement for sqlite3.connect() that routes through libSQL.
Uses embedded replica mode: local file for fast reads, syncs to Turso
for durable writes. If TURSO_DATABASE_URL isn't set, falls back to
plain sqlite3 so local dev keeps working.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
REPLICA_PATH = os.environ.get("TURSO_REPLICA_PATH", "/tmp/sig_replica.db")

_USE_TURSO = bool(TURSO_URL and TURSO_TOKEN)
_libsql = None
_local_fallback_path = None

if _USE_TURSO:
    try:
        import libsql_experimental as libsql
        _libsql = libsql
        print(f"[turso] Using Turso at {TURSO_URL[:40]}...")
    except ImportError as e:
        print(f"[turso] libsql_experimental import failed: {e}")
        print("[turso] Falling back to local sqlite3")
        _USE_TURSO = False
else:
    print("[turso] TURSO_DATABASE_URL / TURSO_AUTH_TOKEN not set — using local sqlite3")


def set_local_fallback_path(path: str) -> None:
    """Server calls this with the plain sqlite path it wants when Turso is off."""
    global _local_fallback_path
    _local_fallback_path = path


def connect() -> "sqlite3.Connection":
    """Return a database connection (Turso or sqlite3)."""
    if _USE_TURSO:
        return _libsql.connect(
            database=REPLICA_PATH,
            sync_url=TURSO_URL,
            auth_token=TURSO_TOKEN,
        )
    if not _local_fallback_path:
        raise RuntimeError("No Turso credentials and no local fallback path set")
    return sqlite3.connect(_local_fallback_path, timeout=30, check_same_thread=False)


def is_turso_enabled() -> bool:
    return _USE_TURSO


def sync(conn) -> None:
    """Push/pull with Turso. No-op when running on local sqlite3."""
    if not _USE_TURSO:
        return
    try:
        conn.sync()
    except Exception as e:
        print(f"[turso] sync failed: {e}")


def checkpoint_and_close(conn) -> None:
    """Best-effort cleanup at shutdown."""
    try:
        if _USE_TURSO:
            try:
                conn.sync()
            except Exception:
                pass
        else:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except Exception as e:
        print(f"[turso] close failed: {e}")