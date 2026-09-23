"""
Auto-refresh Quotex session with strict guard.

- If no session exists (no env var, no session.json), do NOTHING.
  No HTTP login attempts. Render stays quiet.
- If a session exists (either from env var or pushed via /api/admin/set-session),
  try refreshing every 2 hours.
- On HTTP failure, keep the existing session and back off to 4 hours.
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
BACKOFF_AFTER_FAIL = 4 * 60 * 60
WATCH_INTERVAL = 60

EMAIL = os.environ.get("QUOTEX_EMAIL", "moetaesibiz@gmail.com")
PASSWORD = os.environ.get("QUOTEX_PASSWORD", "")
SESSION_JSON_ENV = os.environ.get("QUOTEX_SESSION_JSON", "")

SESSION_PATH = Path(__file__).parent / "session.json"


def _cookies_to_header(jar):
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def load_env_session():
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


def apply_to_collector():
    try:
        import quotex_collector as qc
        fn = getattr(qc, "force_reconnect", None)
        if callable(fn):
            fn()
            print("[auto_refresh] Signalled collector to reconnect")
    except Exception as e:
        print(f"[auto_refresh] Could not poke collector: {e}")


def wait_for_first_session():
    """Block until session.json exists. No Quotex hits until it does."""
    loaded_env = load_env_session()
    if loaded_env or SESSION_PATH.exists():
        return True

    print("[auto_refresh] No session configured. Standing by. "
          "Push via /api/admin/set-session or set QUOTEX_SESSION_JSON.")

    while True:
        time.sleep(WATCH_INTERVAL)
        # Reload env var every minute in case it was set externally
        new_env = os.environ.get("QUOTEX_SESSION_JSON", "")
        if new_env and new_env != SESSION_JSON_ENV:
            if load_env_session():
                return True
        if SESSION_PATH.exists():
            try:
                data = json.loads(SESSION_PATH.read_text())
                if data.get("token"):
                    print("[auto_refresh] External session detected")
                    return True
            except Exception:
                pass


def refresh_loop():
    wait_for_first_session()
    print(f"[auto_refresh] Session ready. Refreshing every {REFRESH_INTERVAL // 3600}h")

    while True:
        time.sleep(REFRESH_INTERVAL)
        session = fetch_fresh_session()
        if session:
            write_session(session)
            apply_to_collector()
        else:
            print(f"[auto_refresh] Refresh failed. Backing off {BACKOFF_AFTER_FAIL // 3600}h")
            time.sleep(BACKOFF_AFTER_FAIL)


def start_background_refresher():
    t = threading.Thread(target=refresh_loop, daemon=True, name="AutoRefresh")
    t.start()
    return t


if __name__ == "__main__":
    refresh_loop()