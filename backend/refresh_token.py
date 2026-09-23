from __future__ import annotations
import json, os, re, sys
from pathlib import Path
from curl_cffi import requests

BASE = "https://qxbroker.com"
LANG = "en"
IMPERSONATE = "firefox135"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:127.0) Gecko/20100101 Firefox/127.0"
SESSION_PATH = Path(__file__).parent / "session.json"
EMAIL = os.environ.get("QUOTEX_EMAIL", "moetaesibiz@gmail.com")
PASSWORD = os.environ.get("QUOTEX_PASSWORD", "")


def _cookies_to_header(jar):
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def main() -> int:
    if not PASSWORD:
        print("[refresh] QUOTEX_PASSWORD not set")
        return 1

    s = requests.Session(impersonate=IMPERSONATE)
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.5"})

    r = s.get(f"{BASE}/{LANG}", timeout=20)
    print(f"[refresh] GET /{LANG} -> {r.status_code}")
    if r.status_code != 200:
        return 1

    r = s.get(f"{BASE}/{LANG}/sign-in/modal/", timeout=20)
    print(f"[refresh] GET /sign-in/modal/ -> {r.status_code}")
    m = re.search(r'name="_token"\s+value="([^"]+)"', r.text)
    if not m:
        m = re.search(r'value="([^"]+)"\s+name="_token"', r.text)
    if not m:
        print("[refresh] No _token in modal page")
        return 1
    token = m.group(1)

    r = s.post(f"{BASE}/{LANG}/sign-in/", data={
        "_token": token, "email": EMAIL, "password": PASSWORD, "remember": 1,
    }, headers={
        "Referer": f"{BASE}/{LANG}/sign-in",
        "Origin": BASE,
        "Content-Type": "application/x-www-form-urlencoded",
    }, timeout=20)
    print(f"[refresh] POST /sign-in/ -> {r.status_code}")

    if 'name="keep_code"' in r.text:
        code = os.environ.get("QUOTEX_2FA_CODE", "").strip()
        if not code:
            print("[refresh] 2FA required but QUOTEX_2FA_CODE not set")
            return 1
        r = s.post(f"{BASE}/{LANG}/sign-in/modal", data={
            "_token": token, "email": EMAIL, "password": PASSWORD,
            "remember": 1, "keep_code": 1, "code": code,
        }, headers={
            "Referer": f"{BASE}/{LANG}/sign-in/modal",
            "Origin": BASE,
            "Content-Type": "application/x-www-form-urlencoded",
        }, timeout=20)
        print(f"[refresh] POST /sign-in/modal -> {r.status_code}")

    if "/trade" not in str(r.url):
        r = s.get(f"{BASE}/{LANG}/trade", timeout=20)
        print(f"[refresh] GET /trade -> {r.status_code}")

    # Save the trade page for offline inspection
    Path("trade_page_debug.html").write_text(r.text, encoding="utf-8")

    # Find the SSID: "token":"<32 hex chars>"
    ssid = None
    m = re.search(r'"token"\s*:\s*"([a-f0-9]{32})"', r.text)
    if m:
        ssid = m.group(1)
        print(f"[refresh] Found SSID: {ssid}")

    if not ssid:
        candidates = list(dict.fromkeys(re.findall(r'[a-f0-9]{32}', r.text)))
        print(f"[refresh] No explicit match. {len(candidates)} hex candidates: {candidates[:5]}")
        if candidates:
            ssid = candidates[0]
            print(f"[refresh] Falling back to: {ssid}")

    if not ssid:
        print("[refresh] Could not extract SSID")
        return 1

    SESSION_PATH.write_text(json.dumps({
        "cookies": _cookies_to_header(s.cookies.get_dict()),
        "token": ssid,
        "user_agent": UA,
    }, indent=4))
    print(f"[refresh] Wrote session.json (ssid={ssid})")
    return 0


if __name__ == "__main__":
    sys.exit(main())