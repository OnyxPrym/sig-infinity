"""
Auto-refresh Quotex session every 2 hours.
Runs inside the container, alongside server.py.
Reconnects the WebSocket with fresh credentials on success.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from curl_cffi import requests

BASE = "https://qxbroker.com"
LANG = "en"
IMPERSONATE = "firefox133"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:127.0) Gecko/20100101 Firefox/127.0"

REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL_SECONDS", 2 * 60 * 60))  # 2h
RETRY_INTERVAL = 5 * 60                                                          # 5m

EMAIL = os.environ.get("QUOTEX_EMAIL", "moetaesibiz@gmail.com")
PASSWORD = os.environ.get("QUOTEX_PASSWORD", "")

SESSION_PATH = Path(__file__).parent / "session.json"


def _cookies_to_header(jar: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def fetch_fresh_session() -> dict | None:
    """Login to Quotex and return {cookies, token, user_agent} or None."""
    if not PASSWORD:
        print("[auto_refresh] QUOTEX_PASSWORD not set")
        return None

    try:
        s = requests.Session(impersonate=IMPERSONATE)
        s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.5"})

        r = s.get(f"{BASE}/{LANG}", timeout=20)
        if r.status_code != 200:
            print(f"[auto_refresh] GET /{LANG} -> {r.status_code}")
            return None

        r = s.get(f"{BASE}/{LANG}/sign-in/modal/", timeout=20)
        m = re.search(
            r'<input[^>]*name=["\']_token["\'][^>]*value=["\']([^"\']+)["\']', r.text
        )
        if not m:
            print("[auto_refresh] No _token in modal page")
            return None
        token = m.group(1)

        r = s.post(
            f"{BASE}/{LANG}/sign-in/",
            data={"_token": token, "email": EMAIL, "password": PASSWORD, "remember": 1},
            headers={
                "Referer": f"{BASE}/{LANG}/sign-in",
                "Origin": BASE,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=20,
        )

        if 'name="keep_code"' in r.text:
            print("[auto_refresh] 2FA is enabled — cannot auto-refresh. "
                  "Disable 2FA on the Quotex account.")
            return None

        if "/trade" not in str(r.url):
            r = s.get(f"{BASE}/{LANG}/trade", timeout=20)

        ssid = None
        m = re.search(r"window\.settings\s*=\s*(\{.*?\});", r.text, re.S)
        if m:
            try:
                ssid = json.loads(m.group(1)).get("token")
            except Exception:
                pass

        if not ssid:
            r2 = s.get(
                f"{BASE}/api/v1/cabinets/digest",
                headers={"Referer": f"{BASE}/{LANG}/trade"},
                timeout=20,
            )
            if r2.status_code == 200:
                try:
                    ssid = r2.json().get("data", {}).get("token")
                except Exception:
                    pass

        if not ssid:
            print("[auto_refresh] Could not extract SSID")
            return None

        return {
            "cookies": _cookies_to_header(s.cookies.get_dict()),
            "token": ssid,
            "user_agent": UA,
        }

    except Exception as e:
        print(f"[auto_refresh] Login error: {e}")
        return None


def write_session(session: dict) -> None:
    SESSION_PATH.write_text(json.dumps(session, indent=4))
    print(f"[auto_refresh] Wrote {SESSION_PATH} (ssid={session['token'][:16]}…)")


def apply_to_running_collector(session: dict) -> None:
    """Best-effort: update the live collector's session + force reconnect."""
    try:
        import quotex_collector as qc

        if hasattr(qc, "STATE"):
            qc.STATE["session_loaded"] = True

        # If collector exposes a reconnect hook, call it
        for name in ("force_reconnect", "reload_session", "reconnect"):
            fn = getattr(qc, name, None)
            if callable(fn):
                try:
                    fn()
                    print(f"[auto_refresh] Called quotex_collector.{name}()")
                    return
                except Exception as e:
                    print(f"[auto_refresh] {name}() failed: {e}")

        print("[auto_refresh] No reconnect hook on quotex_collector — "
              "session.json updated, collector will pick it up on next reconnect")
    except Exception as e:
        print(f"[auto_refresh] Could not poke collector: {e}")


def refresh_loop() -> None:
    """Background loop: refresh every 2h, retry every 5m on failure."""
    # Small initial delay so server.py finishes booting
    time.sleep(15)

    while True:
        print(f"[auto_refresh] Refreshing Quotex session (interval={REFRESH_INTERVAL}s)...")
        session = fetch_fresh_session()

        if session:
            write_session(session)
            apply_to_running_collector(session)
            print(f"[auto_refresh] Next refresh in {REFRESH_INTERVAL // 3600}h")
            time.sleep(REFRESH_INTERVAL)
        else:
            print(f"[auto_refresh] Refresh failed. Retry in {RETRY_INTERVAL // 60}m")
            time.sleep(RETRY_INTERVAL)


def start_background_refresher() -> threading.Thread:
    t = threading.Thread(target=refresh_loop, daemon=True, name="AutoRefresh")
    t.start()
    return t


if __name__ == "__main__":
    refresh_loop()