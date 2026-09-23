"""
Headless Quotex token refresher.
- Logs in with curl_cffi (TLS impersonation) to bypass Cloudflare.
- Submits 2FA code if the account has it (reads QUOTEX_2FA_CODE env or stdin).
- Writes SSID + cookies to session.json.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from curl_cffi import requests

BASE = "https://qxbroker.com"
LANG = "en"
IMPERSONATE = "firefox133"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:127.0) Gecko/20100101 Firefox/127.0"

SESSION_PATH = Path(__file__).parent / "session.json"
EMAIL = os.environ.get("QUOTEX_EMAIL", "moetaesibiz@gmail.com")
PASSWORD = os.environ.get("QUOTEX_PASSWORD", "")


def _cookies_to_header(jar: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def main() -> int:
    if not PASSWORD:
        print("[refresh] QUOTEX_PASSWORD env var is empty. Set it and retry.")
        return 1

    s = requests.Session(impersonate=IMPERSONATE)
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.5"})

    r = s.get(f"{BASE}/{LANG}")
    print(f"[refresh] GET /{LANG} -> {r.status_code}")
    if r.status_code != 200:
        return 1

    r = s.get(f"{BASE}/{LANG}/sign-in/modal/")
    print(f"[refresh] GET /sign-in/modal/ -> {r.status_code}")
    m = re.search(
        r'<input[^>]*name=["\']_token["\'][^>]*value=["\']([^"\']+)["\']', r.text
    )
    if not m:
        print("[refresh] No _token found in modal page")
        return 1
    token = m.group(1)

    r = s.post(
        f"{BASE}/{LANG}/sign-in/",
        data={"_token": token, "email": EMAIL, "password": PASSWORD, "remember": 1},
        headers={
            "Referer": f"{BASE}/{LANG}/sign-in",
            "Origin": BASE,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    print(f"[refresh] POST /sign-in/ -> {r.status_code}")

    if 'name="keep_code"' in r.text:
        code = os.environ.get("QUOTEX_2FA_CODE") or input("[refresh] 2FA code: ").strip()
        r = s.post(
            f"{BASE}/{LANG}/sign-in/modal",
            data={
                "_token": token,
                "email": EMAIL,
                "password": PASSWORD,
                "remember": 1,
                "keep_code": 1,
                "code": code,
            },
            headers={
                "Referer": f"{BASE}/{LANG}/sign-in/modal",
                "Origin": BASE,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        print(f"[refresh] POST /sign-in/modal -> {r.status_code}")

    if "/trade" not in str(r.url):
        r = s.get(f"{BASE}/{LANG}/trade")
        print(f"[refresh] GET /trade -> {r.status_code}")

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
        )
        if r2.status_code == 200:
            try:
                ssid = r2.json().get("data", {}).get("token")
            except Exception:
                pass

    if not ssid:
        print("[refresh] Could not extract SSID")
        return 2

    SESSION_PATH.write_text(
        json.dumps(
            {
                "cookies": _cookies_to_header(s.cookies.get_dict()),
                "token": ssid,
                "user_agent": UA,
            },
            indent=4,
        )
    )
    print(f"[refresh] Wrote {SESSION_PATH} (ssid={ssid[:16]}\u2026)")
    return 0


if __name__ == "__main__":
    sys.exit(main())