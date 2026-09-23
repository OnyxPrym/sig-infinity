"""
Sig Infinity AI — Data Collector
---------------------------------
Pulls OHLC candles from Twelve Data (https://twelvedata.com), a legitimate
licensed market-data provider with a free tier. Get an API key at
https://twelvedata.com/apikey and set TWELVE_DATA_API_KEY as an env var.

No trading-platform account, browser automation, or unofficial/reverse
engineered client is used anywhere in this pipeline.

Symbols use standard spot-market tickers (EUR/USD, GBP/USD, USD/JPY, XAU/USD)
since synthetic "OTC" instruments are broker-internal and are not offered by
any licensed data vendor.
"""

import os
import time
import sqlite3
import requests
import pandas as pd
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads backend/.env automatically if present
except ImportError:
    pass


def market_is_open(now: datetime = None) -> bool:
    """
    Forex/gold spot market hours: open Sunday 22:00 UTC (5pm ET) through
    Friday 22:00 UTC (5pm ET), closed the rest of the weekend.
    """
    now = now or datetime.now(timezone.utc)
    weekday = now.weekday()  # Monday=0 ... Sunday=6
    hour = now.hour

    if weekday == 5:  # Saturday
        return False
    if weekday == 6 and hour < 22:  # Sunday before 22:00 UTC
        return False
    if weekday == 4 and hour >= 22:  # Friday after 22:00 UTC
        return False
    return True

API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
BASE_URL = "https://api.twelvedata.com/time_series"

SYMBOLS = {
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "XAUUSD": "XAU/USD",
}

DB_PATH = os.path.join(os.path.dirname(__file__), "sig_infinity.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            symbol TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (symbol, timestamp)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def log(level: str, message: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO logs (ts, level, message) VALUES (?, ?, ?)",
                  (datetime.now(timezone.utc).isoformat(), level, message))
    conn.commit()
    conn.close()


def fetch_candles(symbol_key: str, outputsize: int = 200) -> pd.DataFrame:
    """Fetch 1-minute candles for a symbol from Twelve Data."""
    if not API_KEY:
        raise RuntimeError(
            "TWELVE_DATA_API_KEY is not set. Get a free key at "
            "https://twelvedata.com/apikey and set it as an environment variable."
        )
    td_symbol = SYMBOLS[symbol_key]
    params = {
        "symbol": td_symbol,
        "interval": "1min",
        "outputsize": outputsize,
        "apikey": API_KEY,
        "format": "JSON",
        "order": "ASC",
    }
    resp = requests.get(BASE_URL, params=params, timeout=15)
    data = resp.json()
    if "values" not in data:
        raise RuntimeError(f"Data source error for {symbol_key}: {data.get('message', data)}")

    rows = []
    for v in data["values"]:
        rows.append({
            "timestamp": pd.to_datetime(v["datetime"]),
            "open": float(v["open"]),
            "high": float(v["high"]),
            "low": float(v["low"]),
            "close": float(v["close"]),
            "volume": float(v.get("volume", 0) or 0),
        })
    df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    return df


def store_candles(symbol_key: str, df: pd.DataFrame):
    conn = sqlite3.connect(DB_PATH)
    for _, r in df.iterrows():
        conn.execute(
            """INSERT OR REPLACE INTO candles
               (symbol, timestamp, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (symbol_key, r["timestamp"].isoformat(), r["open"], r["high"],
             r["low"], r["close"], r["volume"]),
        )
    conn.commit()
    conn.close()


def load_candles(symbol_key: str) -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT * FROM candles WHERE symbol = ? ORDER BY timestamp ASC",
        conn, params=(symbol_key,),
    )
    conn.close()
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def recent_logs(limit: int = 50):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT ts, level, message FROM logs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [{"ts": r[0], "level": r[1], "message": r[2]} for r in rows]


def refresh_all(retries: int = 2):
    """Poll the data source for every symbol and persist candles + logs."""
    init_db()

    if not market_is_open():
        log("INFO", "Market closed (weekend) — skipping poll.")
        return {key: {"ok": False, "market_closed": True} for key in SYMBOLS}

    results = {}
    for key in SYMBOLS:
        for attempt in range(retries + 1):
            try:
                df = fetch_candles(key)
                store_candles(key, df)
                results[key] = {"ok": True, "candles": len(df)}
                log("INFO", f"{key}: fetched {len(df)} candles")
                break
            except Exception as e:
                if attempt == retries:
                    results[key] = {"ok": False, "error": str(e)}
                    log("ERROR", f"{key}: {e}")
                else:
                    time.sleep(1)
    return results
