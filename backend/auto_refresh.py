"""
Auto-refresh Quotex session.

Priority:
1. If QUOTEX_SESSION_JSON env var is set, use it directly (no HTTP login).
2. Every REFRESH_INTERVAL_SECONDS, try to refresh via HTTP login.
3. If refresh fails, keep using the existing session.json.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from curl_cffi import requests

BASE = "https://qxbroker.com"
LANG = "en"
IMPERSONATE = "firefox135"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:127.0) Gecko/20100101 Firefox/127.0"

REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL_SECONDS", 2 * 60 * 60))
RETRY_INTERVAL = 5 * 60

EMAIL = os.environ.get("QUOTEX_EMAIL", "moetaesibiz@gmail.com")
PASSWORD = os.environ.get("QUOTEX_PASSWORD", "")
SESSION_JSON_ENV = os.environ.get("QUOTEX_SESSION_JSON", "")

SESSION_PATH = Path(__file__).parent / "session.json"


def _cookies_to_header(jar):
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def load_env_session():
    """If QUOTEX_SESSION_JSON is set, write it to session.json on boot."""
    if not SESSION_JSON_ENV:
        return False
    try:
        data = json.loads(SESSION_JSON_ENV)
        if not data.get("token"):
            print("[auto_refresh] QUOTEX_SESSION_JSON has no token")
            return False
        SESSION_PATH.write_text(json.dumps(data, indent=4))
        print(f"[auto_refresh] Loaded session from env (ssid={data['token'][:16]}...)")
        return True
    except Exception as e:
        print(f"[auto_refresh] Invalid QUOTEX_SESSION_JSON: {e}")
        return False


def fetch_fresh_session():
    if not PASSWORD:
        return None
    try:
        s = requests.Session(impersonate=IMPERSONATE)
        s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.5"})

        r = s.get(f"{BASE}/{LANG}", timeout=20)
        print(f"[auto_refresh] GET /{LANG} -> {r.status_code}")
        if r.status_code != 200:
            return None

        r = s.get(f"{BASE}/{LANG}/sign-in/modal/", timeout=20)
        m = re.search(r'name="_token"\s+value="([^"]+)"', r.text)
        if not m:
            m = re.search(r'value="([^"]+)"\s+name="_token"', r.text)
        if not m:
            print("[auto_refresh] No _token in modal")
            return None
        token = m.group(1)

        r = s.post(f"{BASE}/{LANG}/sign-in/", data={
            "_token": token, "email": EMAIL, "password": PASSWORD, "remember": 1,
        }, headers={
            "Referer": f"{BASE}/{LANG}/sign-in",
            "Origin": BASE,
            "Content-Type": "application/x-www-form-urlencoded",
        }, timeout=20)
        print(f"[auto_refresh] POST /sign-in/ -> {r.status_code}")

        if 'name="keep_code"' in r.text:
            print("[auto_refresh] 2FA required — cannot auto-refresh")
            return None

        if "/trade" not in str(r.url):
            r = s.get(f"{BASE}/{LANG}/trade", timeout=20)

        m = re.search(r'"token"\s*:\s*"([a-f0-9]{32})"', r.text)
        if not m:
            print("[auto_refresh] No SSID pattern in trade page")
            return None

        ssid = m.group(1)
        print(f"[auto_refresh] New SSID: {ssid[:20]}...")
        return {
            "cookies": _cookies_to_header(s.cookies.get_dict()),
            "token": ssid,
            "user_agent": UA,
        }
    except Exception as e:
        print(f"[auto_refresh] HTTP error: {e}")
        return None


def write_session(session):
    SESSION_PATH.write_text(json.dumps(session, indent=4))
    print(f"[auto_refresh] Wrote {SESSION_PATH}")


def apply_to_collector(session):
    try:
        import quotex_collector as qc
        fn = getattr(qc, "force_reconnect", None)
        if callable(fn):
            fn()
            print("[auto_refresh] Signalled collector to reconnect")
    except Exception as e:
        print(f"[auto_refresh] Could not poke collector: {e}")


def refresh_loop():
    # On boot: try env first, then HTTP
    have_session = load_env_session()

    if not have_session:
        print("[auto_refresh] No env session — trying HTTP login on boot")
        session = fetch_fresh_session()
        if session:
            write_session(session)
            have_session = True

    if not have_session and not SESSION_PATH.exists():
        print("[auto_refresh] No session available. Retrying every 5 min...")
        while True:
            time.sleep(RETRY_INTERVAL)
            session = fetch_fresh_session()
            if session:
                write_session(session)
                apply_to_collector(session)
                break

    print(f"[auto_refresh] Session ready. Next refresh in {REFRESH_INTERVAL // 60} min")
    time.sleep(REFRESH_INTERVAL)

    while True:
        session = fetch_fresh_session()
        if session:
            write_session(session)
            apply_to_collector(session)
            time.sleep(REFRESH_INTERVAL)
        else:
            print(f"[auto_refresh] Refresh failed. Retry in {RETRY_INTERVAL // 60} min")
            time.sleep(RETRY_INTERVAL)


def start_background_refresher():
    t = threading.Thread(target=refresh_loop, daemon=True, name="AutoRefresh")
    t.start()
    return t


if __name__ == "__main__":
    refresh_loop()