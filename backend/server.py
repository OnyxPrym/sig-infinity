"""
Sig Infinity AI - Master Server (Quotex + DB-backed quota)
"""
import os
import json
import atexit
import signal
import sys
import time
import threading
import sqlite3
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, render_template, session
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
from flask_cors import CORS

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import pandas as pd
import analysis as an
import quotex_collector as qc
import turso_db

# -----------------------------------------------------------
# INIT_AUTH_DB_V1: Ensure tables exist in Turso at import time
# -----------------------------------------------------------
def _ensure_tables():
    tables_sql = [
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS user_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            entry_time TEXT NOT NULL,
            expiry_time TEXT NOT NULL,
            result TEXT,
            resolved INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS quota (
            ip TEXT NOT NULL,
            day TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (ip, day))""",
        """CREATE TABLE IF NOT EXISTS admins (
            ip TEXT PRIMARY KEY,
            unlocked_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS bot_session (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            ssid TEXT NOT NULL,
            cookies TEXT,
            user_agent TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    ]
    try:
        conn = turso_db.connect()
        for sql in tables_sql:
            try:
                conn.execute(sql)
            except Exception as e:
                print("[init_db] %s" % e)
        conn.commit()
        print("[init_db] Turso tables ensured")
    except Exception as e:
        print("[init_db] Failed: %s" % e)

try:
    _ensure_tables()
except Exception as e:
    print("[init_db] init error: %s" % e)

ADMIN_PASSCODE = os.environ.get("ADMIN_PASSCODE", "karanka100")
DAILY_FREE_LIMIT = 40

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "sig-infinity-dev-secret")

CORS(app,
     origins="*",
     supports_credentials=False,
     allow_headers=["Content-Type", "Authorization"],
     methods=["GET", "POST", "OPTIONS"])

@app.after_request
def _cors_headers(response):
    origin = request.headers.get("Origin", "")
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Vary"] = "Origin"
    return response

DB_PATH = str(Path(__file__).parent / "sig_infinity.db")
turso_db.set_local_fallback_path(DB_PATH)

def _checkpoint_and_close():
    """Force WAL checkpoint so sig_infinity.db is complete on shutdown."""
    try:
        conn = turso_db.connect()
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
STATE = {"signals_delivered": 0, "last_signal_by_symbol": {}}


# ---------------------------------------------------------------------------
#  DB setup
# ---------------------------------------------------------------------------
def init_user_signals_db():
    conn = turso_db.connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            entry_time TEXT NOT NULL,
            expiry_time TEXT NOT NULL,
            result TEXT,
            resolved INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    turso_db.sync(conn)
    conn.close()


def init_auth_db():
    conn = turso_db.connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    conn.commit()
    turso_db.sync(conn)
    conn.close()


def init_quota_db():
    conn = turso_db.connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quota (
            ip TEXT NOT NULL,
            day TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (ip, day)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            ip TEXT PRIMARY KEY,
            unlocked_at TEXT NOT NULL
        )
    """)
    conn.commit()
    turso_db.sync(conn)
    conn.close()


def get_client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def is_admin(ip):
    conn = turso_db.connect()
    row = conn.execute("SELECT 1 FROM admins WHERE ip = ?", (ip,)).fetchone()
    conn.close()
    return row is not None


def set_admin(ip):
    conn = turso_db.connect()
    conn.execute("INSERT OR REPLACE INTO admins (ip, unlocked_at) VALUES (?, ?)",
                 (ip, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    turso_db.sync(conn)
    conn.close()


def clear_admin(ip):
    conn = turso_db.connect()
    conn.execute("DELETE FROM admins WHERE ip = ?", (ip,))
    conn.commit()
    turso_db.sync(conn)
    conn.close()


def quota_status(ip):
    today = date.today().isoformat()
    conn = turso_db.connect()
    row = conn.execute("SELECT used FROM quota WHERE ip = ? AND day = ?",
                       (ip, today)).fetchone()
    conn.close()
    used = row[0] if row else 0
    return used, max(0, DAILY_FREE_LIMIT - used)


def consume_quota(ip):
    today = date.today().isoformat()
    conn = turso_db.connect()
    conn.execute("""
        INSERT INTO quota (ip, day, used) VALUES (?, ?, 1)
        ON CONFLICT(ip, day) DO UPDATE SET used = used + 1
    """, (ip, today))
    conn.commit()
    turso_db.sync(conn)
    conn.close()
    return quota_status(ip)


def load_candles(symbol, limit=300):
    conn = turso_db.connect()
    df = pd.read_sql_query(
        "SELECT * FROM candles WHERE source = 'quotex' AND symbol = ? "
        "ORDER BY timestamp DESC LIMIT ?",
        conn, params=(symbol, limit),
    )
    conn.close()
    if df.empty:
        return df
    df = df.iloc[::-1].reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def candle_count(symbol):
    conn = turso_db.connect()
    n = conn.execute(
        "SELECT COUNT(*) FROM candles WHERE source='quotex' AND symbol=?",
        (symbol,),
    ).fetchone()[0]
    conn.close()
    return n


# ---------------------------------------------------------------------------
#  Routes
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/status")
def status():
    counts = {sym: candle_count(sym) for sym in qc.OTC_MARKETS}
    return jsonify({
        "collector_running": qc.STATE["running"],
        "collector_connected": qc.STATE["connected"],
        "session_loaded": qc.STATE["session_loaded"],
        "collector_error": qc.STATE["error"],
        "candle_counts": counts,
        "candles_collected": qc.STATE["candles_collected"],
        "last_update_by_market": qc.STATE["last_update_by_market"],
        "signals_delivered": STATE["signals_delivered"],
        "last_signal_by_symbol": STATE["last_signal_by_symbol"],
        "symbols": list(qc.OTC_MARKETS.keys()),
        "market_open": True,
    })


@app.route("/api/logs")
def logs():
    return jsonify([])


@app.route("/api/quota")
def quota():
    ip = get_client_ip()
    admin = is_admin(ip)
    if admin:
        return jsonify({"is_admin": True, "remaining": None, "limit": None, "ip": ip})
    used, remaining = quota_status(ip)
    return jsonify({"is_admin": False, "remaining": remaining,
                    "limit": DAILY_FREE_LIMIT, "used": used, "ip": ip})


@app.route("/api/admin/unlock", methods=["POST"])
def admin_unlock():
    passcode = (request.json or {}).get("passcode", "")
    ip = get_client_ip()
    if passcode == ADMIN_PASSCODE:
        # Always set IP-based admin (backward compat)
        set_admin(ip)

        # Also set user-based admin if the request is authenticated
        user = get_current_user()
        if user:
            conn = turso_db.connect()
            conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user["user_id"],))
            conn.commit()
            turso_db.sync(conn)
            conn.close()
            return jsonify({"ok": True, "message": "Admin unlocked for user", "user_admin": True})

        return jsonify({"ok": True, "message": "Admin unlocked for IP", "user_admin": False})
    return jsonify({"ok": False, "message": "Incorrect passcode."}), 401


@app.route("/api/admin/lock", methods=["POST"])
def admin_lock():
    ip = get_client_ip()
    clear_admin(ip)
    return jsonify({"ok": True, "message": "Admin locked"})


@app.route("/api/peek/<symbol>", methods=["GET"])
def peek_analysis(symbol):
    symbol = symbol.upper()
    if symbol not in qc.OTC_MARKETS:
        return jsonify({"ok": False, "message": f"Unknown symbol: {symbol}"}), 400
    df = load_candles(symbol, limit=300)
    if df.empty or len(df) < 60:
        return jsonify({"ok": True, "symbol": symbol, "hot": False, "direction": None})
    try:
        result = an.analyze_symbol(df, symbol)
    except Exception as e:
        return jsonify({"ok": True, "symbol": symbol, "hot": False, "direction": None, "error": str(e)})
    bias = result.get("bias", "WAIT")
    return jsonify({
        "ok": True, "symbol": symbol,
        "hot": bias in ("BUY", "SELL"),
        "direction": bias if bias in ("BUY", "SELL") else None,
        "score": int(result.get("score", 0)),
        "total": int(result.get("total", 10)),
    })


@app.route("/api/analyze/<symbol>", methods=["POST"])
def analyze(symbol):
    symbol = symbol.upper()
    if symbol not in qc.OTC_MARKETS:
        return jsonify({"ok": False, "message": f"Unknown symbol: {symbol}"}), 400

    # --- Determine who is making the request ---
    user = get_current_user()      # Logged-in user (via Bearer token)
    ip = get_client_ip()           # Fallback IP
    ip_admin = is_admin(ip)        # IP-based admin (from /api/admin/unlock)

    # Effective admin = user is admin OR IP is admin
    is_effective_admin = ip_admin or (user is not None and user.get("is_admin"))

    # --- Quota pre-check ---
    if not is_effective_admin:
        if user is not None:
            used, remaining = get_quota_for_user(user["user_id"])
        else:
            used, remaining = quota_status(ip)
        if remaining <= 0:
            return jsonify({
                "ok": False,
                "quota_exhausted": True,
                "message": f"Daily limit of {DAILY_FREE_LIMIT} analyses reached. Resets at midnight UTC.",
            }), 429

    if not qc.STATE["connected"]:
        return jsonify({"ok": False, "message": "Collector not connected. Run refresh_token.py."}), 503

    df = load_candles(symbol, limit=500)
    if df.empty or len(df) < 60:
        return jsonify({"ok": False, "message": f"Not enough data for {symbol} yet."}), 503

    # Use the SAME cached flip as /api/forming/ â€” no recompute, no drift.
    st = qc.get_flip_state(symbol)
    if not st or st.get("direction") is None:
        result = {"bias": "WAIT", "ok": True, "reason": "No setup forming.",
                  "symbol": symbol, "score": 0, "total": 10, "factors": {}}
    else:
        flip_time = st.get("flip_time_epoch")
        now_epoch = time.time()
        age = max(0.0, now_epoch - (flip_time or now_epoch))
        flash = an.Config.SIGNAL_DELAY_SECONDS
        window = an.Config.ENTRY_WINDOW_SECONDS
        total_live = flash + window

        if age > total_live:
            result = {"bias": "WAIT", "ok": True, "reason": "Signal window closed.",
                      "symbol": symbol, "score": 0, "total": 10, "factors": {}}
        else:
            entry_opens = now_epoch if age >= flash else (now_epoch + (flash - age))
            entry_closes = entry_opens + window
            last_close = float(df["close"].iloc[-1])
            result = {
                "symbol": symbol,
                "bias": st["direction"],
                "direction_candidate": st["direction"],
                "trigger_timeframe": "SETUP",
                "score": 10,
                "total": 10,
                "reason": f"Setup confirmed for {symbol}.",
                "factors": {"confirmed": True},
                "price": last_close,
                "entry_price": last_close,
                "entry_time": datetime.fromtimestamp(entry_opens, tz=timezone.utc).isoformat(),
                "expiry_time": datetime.fromtimestamp(entry_closes, tz=timezone.utc).isoformat(),
                "entry_window_seconds": window,
                "entry_window_opens_at": datetime.fromtimestamp(entry_opens, tz=timezone.utc).isoformat(),
                "entry_window_closes_at": datetime.fromtimestamp(entry_closes, tz=timezone.utc).isoformat(),
                "generated_at": now_epoch,
                "ok": True,
            }

    # If the entry window has already closed since the flip, don't fire a fresh one.
    ewc = result.get("entry_window_closes_at")
    if ewc and result.get("bias") in ("BUY", "SELL"):
        try:
            closes_at = pd.Timestamp(ewc)
            if pd.Timestamp.now(tz=closes_at.tz) > closes_at:
                result = {"bias": "WAIT", "ok": True, "reason": "Signal window closed.",
                          "symbol": symbol, "score": 0, "total": 10, "factors": {}}
        except Exception:
            pass

    # Save to user's personal signal history ONCE per flip per user.
    if user and result.get("bias") in ("BUY", "SELL"):
        try:
            save_user_signal(
                user["user_id"],
                symbol,
                result["bias"],
                result.get("entry_price", result.get("price")),
                result.get("entry_time", datetime.now(timezone.utc).isoformat()),
                result.get("expiry_time", (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()),
            )
        except Exception as e:
            print(f"[signals] save failed: {e}")

    # --- Consume quota ---
    if is_effective_admin:
        result["tier"] = "admin"
        result["remaining_today"] = None
    elif user is not None:
        used, remaining = consume_user_quota(user["user_id"])
        result["tier"] = "user"
        result["remaining_today"] = remaining
    else:
        used, remaining = consume_quota(ip)
        result["tier"] = "free"
        result["remaining_today"] = remaining

    result["generated_at"] = time.time()

    if result["bias"] in ("BUY", "SELL"):
        STATE["signals_delivered"] += 1
        STATE["last_signal_by_symbol"][symbol] = result["bias"]
    else:
        STATE["last_signal_by_symbol"][symbol] = "WAIT"

    return jsonify(result)


@app.route("/api/debug/<symbol>", methods=["GET"])
def debug_analysis(symbol):
    symbol = symbol.upper()
    if symbol not in qc.OTC_MARKETS:
        return jsonify({"ok": False, "message": f"Unknown symbol: {symbol}"}), 400
    df = load_candles(symbol, limit=300)
    if df.empty:
        return jsonify({"ok": False, "message": "No candles in database"}), 503
    factors = an.compute_all_factors(df)
    return jsonify({"ok": True, "symbol": symbol, "candles_1m": len(df),
                    "factors": factors, "generated_at": time.time()})


@app.route("/api/control/start", methods=["POST"])
def control_start():
    if not qc.STATE["running"]:
        qc.start_collector_thread()
    return jsonify({"ok": True, "collector_running": qc.STATE["running"]})


@app.route("/api/control/stop", methods=["POST"])
def control_stop():
    qc.stop_collector()
    return jsonify({"ok": True, "collector_running": qc.STATE["running"]})


# ---------------------------------------------------------------------------
#  /api/approaching/<symbol> -- indicates how close a whitelisted setup is
# ---------------------------------------------------------------------------
@app.route("/api/approaching/<symbol>", methods=["GET"])
def approaching(symbol):
    symbol = symbol.upper()
    if symbol not in qc.OTC_MARKETS:
        return jsonify({"ok": False, "message": f"Unknown symbol: {symbol}"}), 400

    df = load_candles(symbol, limit=300)
    if df.empty or len(df) < 30:
        return jsonify({"ok": True, "symbol": symbol, "status": "IDLE", "reason": "no data"})

    try:
        import analysis as _an
        result = _an.approaching_status(df, symbol)
    except Exception as e:
        return jsonify({"ok": True, "symbol": symbol, "status": "IDLE", "error": str(e)})

    return jsonify({"ok": True, "symbol": symbol, **result})

# ---------------------------------------------------------------------------
#  AUTH HELPERS
# ---------------------------------------------------------------------------
SESSION_DURATION_SECONDS = 7 * 24 * 60 * 60




# ---------------------------------------------------------------------------
#  ADMIN: Update Quotex session live (no redeploy needed)
# ---------------------------------------------------------------------------
@app.route("/api/admin/set-session", methods=["POST"])
def set_session_endpoint():
    token = request.headers.get("X-Admin-Token", "")
    if token != ADMIN_PASSCODE:
        return jsonify({"ok": False, "message": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    if not data.get("token"):
        return jsonify({"ok": False, "message": "missing token"}), 400

    ssid = data["token"]
    cookies = data.get("cookies", "")
    user_agent = data.get("user_agent", "")

    # 1. Write to session.json (immediate use by collector)
    session_path = Path(__file__).parent / "session.json"
    session_path.write_text(json.dumps(data, indent=4))

    # 2. Persist to Turso (survives container restarts)
    try:
        import turso_db
        conn = turso_db.connect()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS bot_session ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "ssid TEXT NOT NULL, "
            "cookies TEXT, "
            "user_agent TEXT, "
            "updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO bot_session (id, ssid, cookies, user_agent) VALUES (1, ?, ?, ?)",
            (ssid, cookies, user_agent),
        )
        conn.commit()
        conn.close()
        print(f"[set-session] Persisted to Turso (ssid={ssid[:16]}...)")
    except Exception as e:
        print(f"[set-session] Turso write failed: {e}")

    # 3. Force collector to reload
    try:
        qc.force_reconnect()
        print(f"[set-session] Collector reconnect signalled")
    except Exception as e:
        print(f"[set-session] Reconnect failed: {e}")

    return jsonify({"ok": True, "message": "Session updated + persisted to Turso"})


def get_current_user():
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        token = request.args.get("token", "").strip()
    if not token:
        return None
    conn = turso_db.connect()
    row = conn.execute(
        "SELECT s.user_id, u.email, u.is_admin FROM sessions s "
        "JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ? AND s.expires_at > ?",
        (token, time.time())
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"user_id": row[0], "email": row[1], "is_admin": bool(row[2]), "token": token}


def create_session(user_id):
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + SESSION_DURATION_SECONDS
    conn = turso_db.connect()
    conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires_at)
    )
    conn.commit()
    turso_db.sync(conn)
    conn.close()
    return token


def get_quota_for_user(user_id):
    today = date.today().isoformat()
    conn = turso_db.connect()
    row = conn.execute(
        "SELECT used FROM quota WHERE ip = ? AND day = ?",
        (f"user_{user_id}", today)
    ).fetchone()
    conn.close()
    used = row[0] if row else 0
    return used, max(0, DAILY_FREE_LIMIT - used)


def consume_user_quota(user_id):
    today = date.today().isoformat()
    conn = turso_db.connect()
    conn.execute("""
        INSERT INTO quota (ip, day, used) VALUES (?, ?, 1)
        ON CONFLICT(ip, day) DO UPDATE SET used = used + 1
    """, (f"user_{user_id}", today))
    conn.commit()
    turso_db.sync(conn)
    conn.close()
    return get_quota_for_user(user_id)


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or "@" not in email or len(email) < 5:
        return jsonify({"ok": False, "message": "Please enter a valid email."}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "message": "Password must be at least 6 characters."}), 400

    conn = turso_db.connect()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"ok": False, "message": "An account with that email already exists."}), 400

    pw_hash = generate_password_hash(password)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, pw_hash))
    user_id = cur.lastrowid
    conn.commit()
    turso_db.sync(conn)
    conn.close()

    token = create_session(user_id)
    return jsonify({"ok": True, "token": token, "email": email, "message": "Account created."})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    conn = turso_db.connect()
    row = conn.execute(
        "SELECT id, password_hash FROM users WHERE email = ?", (email,)
    ).fetchone()
    conn.close()

    if not row or not check_password_hash(row[1], password):
        return jsonify({"ok": False, "message": "Invalid email or password."}), 401

    token = create_session(row[0])
    return jsonify({"ok": True, "token": token, "email": email, "message": "Logged in."})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    user = get_current_user()
    if user:
        conn = turso_db.connect()
        conn.execute("DELETE FROM sessions WHERE token = ?", (user["token"],))
        conn.commit()
        turso_db.sync(conn)
        conn.close()
    return jsonify({"ok": True, "message": "Logged out."})


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "message": "Not logged in."}), 401
    used, remaining = get_quota_for_user(user["user_id"])
    return jsonify({
        "ok": True,
        "email": user["email"],
        "is_admin": user["is_admin"],
        "remaining": remaining,
        "limit": DAILY_FREE_LIMIT,
        "used": used,
    })


# ---------------------------------------------------------------------------
#  USER SIGNAL HISTORY + RESULT TRACKER
# ---------------------------------------------------------------------------
def save_user_signal(user_id, symbol, direction, entry_price, entry_time_iso, expiry_time_iso):
    conn = turso_db.connect()
    conn.execute("""
        INSERT INTO user_signals
        (user_id, symbol, direction, entry_price, entry_time, expiry_time)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, symbol, direction, float(entry_price), entry_time_iso, expiry_time_iso))
    conn.commit()
    turso_db.sync(conn)
    conn.close()


def get_user_signals(user_id, limit=50):
    conn = turso_db.connect()
    rows = conn.execute("""
        SELECT id, symbol, direction, entry_price, entry_time, expiry_time, result, resolved
        FROM user_signals WHERE user_id = ?
        ORDER BY id DESC LIMIT ?
    """, (user_id, limit)).fetchall()
    conn.close()
    return [{
        "id": r[0], "symbol": r[1], "direction": r[2],
        "entry_price": r[3], "entry_time": r[4], "expiry_time": r[5],
        "result": r[6], "resolved": bool(r[7])
    } for r in rows]


def _candle_close_at(symbol, target_iso):
    """Fetch the close of the candle closest to target_iso from the candles table."""
    try:
        target_dt = pd.to_datetime(target_iso)
    except Exception:
        return None
    conn = turso_db.connect()
    row = conn.execute("""
        SELECT close FROM candles
        WHERE source = 'quotex' AND symbol = ?
        ORDER BY ABS(julianday(timestamp) - julianday(?))
        LIMIT 1
    """, (symbol, target_dt.isoformat())).fetchone()
    conn.close()
    return float(row[0]) if row else None


def resolve_pending_user_signals():
    """Check every unresolved signal whose expiry has passed + 15s, mark WON/LOST."""
    now = time.time()
    conn = turso_db.connect()
    pending = conn.execute("""
        SELECT id, symbol, direction, entry_price, expiry_time
        FROM user_signals WHERE resolved = 0
    """).fetchall()
    conn.close()

    for sid, symbol, direction, entry_price, expiry_iso in pending:
        try:
            expiry_dt = pd.to_datetime(expiry_iso)
            expiry_ts = expiry_dt.timestamp()
        except Exception:
            continue

        # Wait until 15 seconds past expiry
        if now < expiry_ts + 15:
            continue

        exit_price = _candle_close_at(symbol, expiry_iso)
        if exit_price is None:
            continue

        if direction == "BUY":
            result = "WON" if exit_price > entry_price else "LOST"
        else:
            result = "WON" if exit_price < entry_price else "LOST"

        conn = turso_db.connect()
        conn.execute("UPDATE user_signals SET result = ?, resolved = 1 WHERE id = ?", (result, sid))
        conn.commit()
        turso_db.sync(conn)
        conn.close()
        print(f"[results] signal {sid} {symbol} {direction}: {result} (entry={entry_price}, exit={exit_price})")


def result_tracker_loop():
    """Background thread -- resolves pending signals every 10s and
    checkpoints the WAL every ~10 minutes."""
    counter = 0
    while True:
        try:
            resolve_pending_user_signals()
            counter += 1
            if counter % 60 == 0:
                conn = turso_db.connect()
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                conn.close()
        except Exception as e:
            print(f"[results] error: {e}")
        time.sleep(10)

@app.route("/api/my-signals", methods=["GET"])
def my_signals():
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "message": "Not logged in"}), 401
    return jsonify({"ok": True, "signals": get_user_signals(user["user_id"], limit=50)})


@app.route("/api/start-result-tracker", methods=["POST"])
def start_result_tracker():
    t = threading.Thread(target=result_tracker_loop, daemon=True, name="ResultTracker")
    t.start()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
#  /api/forming/<symbol> -- tells the frontend when a setup is forming
#  Does NOT consume quota. Does NOT fire a signal.
# ---------------------------------------------------------------------------
@app.route("/api/forming/<symbol>", methods=["GET"])
def forming(symbol):
    # APPROACHING_V1_SERVER
    # 4 phases: idle | approaching | flash | open
    symbol = symbol.upper()
    if symbol not in qc.OTC_MARKETS:
        return jsonify({"ok": False, "forming": False, "message": f"Unknown symbol: {symbol}"}), 400

    st = qc.get_flip_state(symbol)
    if not st or st.get("error"):
        return jsonify({"ok": True, "forming": False, "phase": "idle"})

    now_epoch = time.time()
    flash = an.Config.SIGNAL_DELAY_SECONDS
    window = an.Config.ENTRY_WINDOW_SECONDS
    total_live = flash + window

    latch_dir = st.get("direction")
    latch_t   = st.get("flip_time_epoch")

    if latch_dir is not None and latch_t is not None:
        age = now_epoch - latch_t
        if age < 0:
            age = 0
        if age <= total_live:
            if age < flash:
                return jsonify({
                    "ok": True, "forming": True, "ready": False,
                    "phase": "flash",
                    "direction": latch_dir,
                    "seconds_until_ready": int(flash - age),
                })
            return jsonify({
                "ok": True, "forming": True, "ready": True,
                "phase": "open",
                "direction": latch_dir,
                "seconds_until_close": int(total_live - age),
            })

    # No active latch â€” check approaching
    if st.get("approaching"):
        return jsonify({
            "ok": True, "forming": True, "ready": False,
            "phase": "approaching",
            "direction": ("BUY" if st.get("curr") == 1 else "SELL"),
            "distance_in_atr": st.get("distance_in_atr"),
            "st_line": st.get("st_line"),
            "current_price": st.get("current_price"),
        })

    return jsonify({"ok": True, "forming": False, "phase": "idle"})



if __name__ == "__main__":
    init_auth_db()
    init_user_signals_db()
    threading.Thread(target=result_tracker_loop, daemon=True, name='ResultTracker').start()
    init_quota_db()
    print("=" * 60)
    print("SIG INFINITY AI - Master Server")
    print("=" * 60)
    print("Starting Quotex collector...")
    qc.start_collector_thread()
    time.sleep(1)
    print("Starting Flask API on port 10000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=False)



















