import os, sys, time, json, signal, secrets
from datetime import date, datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, request
from werkzeug.security import generate_password_hash, check_password_hash
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

ADMIN_PASSCODE = os.environ.get("ADMIN_PASSCODE", "karanka100")
DAILY_FREE_LIMIT = 40
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "sig-infinity-dev-secret")
CORS(app, origins="*", supports_credentials=False, allow_headers=["Content-Type", "Authorization"], methods=["GET", "POST", "OPTIONS"])


@app.before_request
def _preflight():
    if request.method == "OPTIONS":
        r = jsonify({"ok": True})
        r.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
        r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        r.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Admin-Token"
        r.headers["Vary"] = "Origin"
        return r


@app.after_request
def _cors(r):
    o = request.headers.get("Origin", "")
    if o:
        r.headers["Access-Control-Allow-Origin"] = o
        r.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Admin-Token"
        r.headers["Vary"] = "Origin"
    return r


DB_PATH = str(Path(__file__).parent / "sig_infinity.db")
turso_db.set_local_fallback_path(DB_PATH)
STATE = {"signals_delivered": 0}


def init_tables():
    try:
        c = turso_db.connect()
        c.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, is_admin INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at REAL NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS quota (ip TEXT NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (ip, day))")
        c.execute("CREATE TABLE IF NOT EXISTS admins (ip TEXT PRIMARY KEY, unlocked_at TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS bot_session (id INTEGER PRIMARY KEY CHECK (id = 1), ssid TEXT NOT NULL, cookies TEXT, user_agent TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        c.commit()
        c.close()
        print("[init_db] tables ensured")
    except Exception as e:
        print("[init_db]", e)


init_tables()


def get_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "?")


def is_admin(ip):
    try:
        c = turso_db.connect()
        r = c.execute("SELECT 1 FROM admins WHERE ip = ?", (ip,)).fetchone()
        c.close()
        return r is not None
    except Exception:
        return False


def set_admin(ip):
    try:
        c = turso_db.connect()
        c.execute("INSERT OR REPLACE INTO admins VALUES (?, ?)", (ip, datetime.now(timezone.utc).isoformat()))
        c.commit()
        c.close()
    except Exception:
        pass


def quota_status(ip):
    today = date.today().isoformat()
    try:
        c = turso_db.connect()
        r = c.execute("SELECT used FROM quota WHERE ip = ? AND day = ?", (ip, today)).fetchone()
        c.close()
        u = r[0] if r else 0
        return u, max(0, DAILY_FREE_LIMIT - u)
    except Exception:
        return 0, DAILY_FREE_LIMIT


def consume_quota(ip):
    today = date.today().isoformat()
    try:
        c = turso_db.connect()
        c.execute("INSERT INTO quota VALUES (?, ?, 1) ON CONFLICT(ip, day) DO UPDATE SET used = used + 1", (ip, today))
        c.commit()
        c.close()
    except Exception:
        pass
    return quota_status(ip)


_CC = {}
_CC_TTL = 30


def candle_count(sym):
    now = time.time()
    cached = _CC.get(sym)
    if cached and now - cached[1] < _CC_TTL:
        return cached[0]
    try:
        c = turso_db.connect()
        n = c.execute("SELECT COUNT(*) FROM candles WHERE source = ? AND symbol = ?", ("quotex", sym)).fetchone()[0]
        c.close()
        _CC[sym] = (n, now)
        return n
    except Exception:
        return cached[0] if cached else 0


def load_candles(sym, limit=50):
    try:
        c = turso_db.connect()
        df = pd.read_sql_query("SELECT * FROM candles WHERE source = ? AND symbol = ? ORDER BY timestamp DESC LIMIT ?", c, params=("quotex", sym, limit))
        c.close()
        if df.empty:
            return df
        df = df.iloc[::-1].reset_index(drop=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df
    except Exception as e:
        print("[load_candles]", sym, e)
        return pd.DataFrame()


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/api/phase/<symbol>")
def phase(symbol):
    symbol = symbol.upper()
    if symbol not in qc.OTC_MARKETS:
        return jsonify({"ok": False, "message": "Unknown"}), 400
    try:
        df = load_candles(symbol, 50)
        if df.empty or len(df) < 22:
            return jsonify({"ok": True, "forming": False, "phase": "idle"})
        trend = an._supertrend(df, an.Config.ATR_PERIOD, an.Config.MULTIPLIER)
        last20 = [int(x) for x in trend.iloc[-20:]]
        curr = int(trend.iloc[-1])
        prev = int(trend.iloc[-2])
        curr_color = "GREEN" if curr == 1 else "RED"
        if curr == prev or curr == 0 or prev == 0:
            return jsonify({"ok": True, "forming": False, "phase": "idle", "curr": curr, "prev": prev, "last20": last20, "current_color": curr_color})
        direction = "BUY" if curr == 1 else "SELL"
        return jsonify({"ok": True, "forming": True, "phase": "open", "direction": direction, "curr": curr, "prev": prev, "last20": last20, "current_color": curr_color})
    except Exception as e:
        print("[phase]", symbol, e)
        return jsonify({"ok": True, "forming": False, "phase": "idle"})


@app.route("/api/debug/<symbol>")
def debug_ep(symbol):
    symbol = symbol.upper()
    if symbol not in qc.OTC_MARKETS:
        return jsonify({"ok": False}), 400
    try:
        df = load_candles(symbol, 50)
        trend = an._supertrend(df, an.Config.ATR_PERIOD, an.Config.MULTIPLIER)
        last20 = [int(x) for x in trend.iloc[-20:]]
        curr = int(trend.iloc[-1])
        prev = int(trend.iloc[-2])
        return jsonify({"ok": True, "symbol": symbol, "ATR": an.Config.ATR_PERIOD, "MULT": an.Config.MULTIPLIER, "last20": last20, "current": "GREEN" if curr == 1 else "RED", "prev": "GREEN" if prev == 1 else "RED", "would_fire": curr != prev, "direction": ("BUY" if curr == 1 else "SELL") if curr != prev else None})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/status")
def status():
    try:
        counts = {s: candle_count(s) for s in qc.OTC_MARKETS}
    except Exception:
        counts = {}
    return jsonify({"collector_running": qc.STATE["running"], "collector_connected": qc.STATE["connected"], "session_loaded": qc.STATE["session_loaded"], "collector_error": qc.STATE["error"], "candle_counts": counts, "candles_collected": qc.STATE["candles_collected"], "last_update_by_market": qc.STATE["last_update_by_market"], "symbols": list(qc.OTC_MARKETS.keys())})


SESSION_SECS = 7 * 24 * 3600


def get_user():
    tok = request.headers.get("Authorization", "").replace("Bearer ", "").strip() or request.args.get("token", "").strip()
    if not tok:
        return None
    try:
        c = turso_db.connect()
        r = c.execute("SELECT s.user_id, u.email, u.is_admin FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ? AND s.expires_at > ?", (tok, time.time())).fetchone()
        c.close()
    except Exception:
        return None
    if not r:
        return None
    return {"user_id": r[0], "email": r[1], "is_admin": bool(r[2]), "token": tok}


def new_session(uid):
    tok = secrets.token_urlsafe(32)
    try:
        c = turso_db.connect()
        c.execute("INSERT INTO sessions VALUES (?, ?, ?)", (tok, uid, time.time() + SESSION_SECS))
        c.commit()
        c.close()
    except Exception:
        pass
    return tok


def user_quota(uid):
    today = date.today().isoformat()
    try:
        c = turso_db.connect()
        r = c.execute("SELECT used FROM quota WHERE ip = ? AND day = ?", ("u_" + str(uid), today)).fetchone()
        c.close()
        u = r[0] if r else 0
        return u, max(0, DAILY_FREE_LIMIT - u)
    except Exception:
        return 0, DAILY_FREE_LIMIT


def use_user_quota(uid):
    today = date.today().isoformat()
    try:
        c = turso_db.connect()
        c.execute("INSERT INTO quota VALUES (?, ?, 1) ON CONFLICT(ip, day) DO UPDATE SET used = used + 1", ("u_" + str(uid), today))
        c.commit()
        c.close()
    except Exception:
        pass
    return user_quota(uid)


@app.route("/api/auth/register", methods=["POST"])
def register():
    d = request.json or {}
    email = (d.get("email") or "").strip().lower()
    pw = d.get("password") or ""
    if not email or "@" not in email:
        return jsonify({"ok": False, "message": "Invalid email"}), 400
    if len(pw) < 6:
        return jsonify({"ok": False, "message": "Password too short"}), 400
    try:
        c = turso_db.connect()
        if c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
            c.close()
            return jsonify({"ok": False, "message": "Email exists"}), 400
        cur = c.cursor()
        cur.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, generate_password_hash(pw)))
        uid = cur.lastrowid
        c.commit()
        c.close()
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500
    return jsonify({"ok": True, "token": new_session(uid), "email": email})


@app.route("/api/auth/login", methods=["POST"])
def login():
    d = request.json or {}
    email = (d.get("email") or "").strip().lower()
    pw = d.get("password") or ""
    try:
        c = turso_db.connect()
        r = c.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        c.close()
    except Exception:
        return jsonify({"ok": False, "message": "DB error"}), 500
    if not r or not check_password_hash(r[1], pw):
        return jsonify({"ok": False, "message": "Invalid"}), 401
    return jsonify({"ok": True, "token": new_session(r[0]), "email": email})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    u = get_user()
    if u:
        try:
            c = turso_db.connect()
            c.execute("DELETE FROM sessions WHERE token = ?", (u["token"],))
            c.commit()
            c.close()
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/auth/me")
def me():
    u = get_user()
    if not u:
        return jsonify({"ok": False}), 401
    used, rem = user_quota(u["user_id"])
    return jsonify({"ok": True, "email": u["email"], "is_admin": u["is_admin"], "remaining": rem, "limit": DAILY_FREE_LIMIT, "used": used})


@app.route("/api/analyze/<symbol>", methods=["POST"])
def analyze(symbol):
    symbol = symbol.upper()
    if symbol not in qc.OTC_MARKETS:
        return jsonify({"ok": False, "message": "Unknown"}), 400
    u = get_user()
    ip = get_ip()
    admin = is_admin(ip) or (u and u["is_admin"])
    if not admin:
        used, rem = user_quota(u["user_id"]) if u else quota_status(ip)
        if rem <= 0:
            return jsonify({"ok": False, "quota_exhausted": True, "message": "Daily limit reached"}), 429
    df = load_candles(symbol, 50)
    if df.empty or len(df) < 22:
        return jsonify({"ok": False, "message": "No data"}), 503
    trend = an._supertrend(df, an.Config.ATR_PERIOD, an.Config.MULTIPLIER)
    curr = int(trend.iloc[-1])
    prev = int(trend.iloc[-2])
    if curr == prev or curr == 0 or prev == 0:
        result = {"ok": True, "bias": "WAIT", "reason": "No SuperTrend flip", "symbol": symbol}
    else:
        direction = "BUY" if curr == 1 else "SELL"
        color = "GREEN" if curr == 1 else "RED"
        now = time.time()
        result = {"ok": True, "symbol": symbol, "bias": direction, "reason": "SuperTrend flipped to " + color, "price": float(df["close"].iloc[-1]), "entry_window_seconds": an.Config.ENTRY_WINDOW_SECONDS, "entry_time": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(), "expiry_time": datetime.fromtimestamp(now + an.Config.ENTRY_WINDOW_SECONDS + an.Config.EXPIRY_MINUTES * 60, tz=timezone.utc).isoformat()}
    if not admin:
        if u:
            use_user_quota(u["user_id"])
        else:
            consume_quota(ip)
    if result.get("bias") in ("BUY", "SELL"):
        STATE["signals_delivered"] += 1
    return jsonify(result)


@app.route("/api/admin/unlock", methods=["POST"])
def admin_unlock():
    p = (request.json or {}).get("passcode", "")
    if p != ADMIN_PASSCODE:
        return jsonify({"ok": False}), 401
    set_admin(get_ip())
    return jsonify({"ok": True})


@app.route("/api/admin/set-session", methods=["POST"])
def set_sess():
    if request.headers.get("X-Admin-Token", "") != ADMIN_PASSCODE:
        return jsonify({"ok": False}), 401
    d = request.get_json(silent=True) or {}
    if not d.get("token"):
        return jsonify({"ok": False, "message": "missing token"}), 400
    (Path(__file__).parent / "session.json").write_text(json.dumps(d, indent=4))
    try:
        c = turso_db.connect()
        c.execute("INSERT OR REPLACE INTO bot_session VALUES (1, ?, ?, ?)", (d["token"], d.get("cookies", ""), d.get("user_agent", "")))
        c.commit()
        c.close()
    except Exception as e:
        print("[set-session]", e)
    try:
        qc.force_reconnect()
    except Exception:
        pass
    return jsonify({"ok": True, "message": "Session updated"})


@app.route("/api/quota")
def quota():
    ip = get_ip()
    if is_admin(ip):
        return jsonify({"is_admin": True, "remaining": None, "limit": None})
    used, rem = quota_status(ip)
    return jsonify({"is_admin": False, "remaining": rem, "limit": DAILY_FREE_LIMIT, "used": used})


@app.route("/")
def index():
    return jsonify({"ok": True, "service": "Sig Infinity AI"})


@app.route("/api/my-signals")
def signals():
    u = get_user()
    if not u:
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "signals": []})


if __name__ == "__main__":
    print("Starting Sig Infinity AI")
    qc.start_collector_thread()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=False)