"""
quotex_collector.py ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â WebSocket streaming edition.

Replaces the 3-second polling loop (get_candles, ~2-3 min behind) with the
real-time WebSocket stream (start_realtime_candle, sub-second updates).
"""
import asyncio
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from pyquotex.stable_api import Quotex
import analysis as _an
import turso_db
import pandas as pd

OTC_MARKETS = {
    "XAUUSD": "XAUUSD",
    "EURUSD": "EURUSD",
    "USDJPY": "USDJPY",
    "XAGUSD": "XAGUSD",
    "GBPUSD": "GBPUSD",
    "AUDCHF": "AUDCHF",
}
HISTORICAL_CANDLES = 250
PERIOD = 60
STREAM_TICK_SLEEP = 0.3  # FASTTICK_V5  # FASTTICK_V5 - 300ms loop  # FASTTICK_V4
FETCH_WORKERS = 1
SESSION_FILE = Path(__file__).parent / "session.json"
DB_PATH = str(Path(__file__).parent / "sig_infinity.db")
turso_db.set_local_fallback_path(DB_PATH)

# _FLIP_LOCK_V1 ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â prevents double-fire of the same flip when two coroutines race
_FLIP_LOCK = threading.Lock()

# OPTIMIZATION: per-symbol SuperTrend cache ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â avoids recomputing on every tick
_TREND_CACHE = {}

STATE = {
    "running": False,
    "connected": False,
    "session_loaded": False,
    "last_update_by_market": {},
    "candles_collected": {},
    "error": None,
    "flips": {},
}

# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬ AUTO_REFRESH_V1: reconnect signal from auto_refresh.py Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
_RECONNECT_EVENT = threading.Event()


def force_reconnect():
    """Called by auto_refresh.py after writing a fresh session.json.
    Signals the current _run_all() to exit cleanly so the outer retry loop
    reloads session.json and reconnects with the new SSID."""
    print("[collector] force_reconnect() called Ã¢â‚¬â€ signalling loop to exit")
    STATE["refresh_pending"] = True
    _RECONNECT_EVENT.set()


def _reconnect_requested() -> bool:
    return _RECONNECT_EVENT.is_set()
# Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬


def _normalize_candle(c):
    """Convert a pyquotex candle dict to our DB schema."""
    ts = c.get("time") or c.get("timestamp") or c.get("from")
    if ts is None:
        return None
    if ts > 10_000_000_000:
        ts = ts // 1000
    return {
        "time": int(ts),
        "open": float(c.get("open", 0)),
        "high": float(c.get("high", 0)),
        "low": float(c.get("low", 0)),
        "close": float(c.get("close", 0)),
        "ticks": int(c.get("ticks", 0)),
    }


# ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ OPTIMIZATION: persistent DB connection ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬
_DB_CONN = None
_DB_LOCK = threading.Lock()

def _get_db():
    global _DB_CONN
    if _DB_CONN is None:
        _DB_CONN = turso_db.connect()
        # WAL mode: concurrent reads/writes, much faster commits
        _DB_CONN.execute("PRAGMA journal_mode=WAL")
        _DB_CONN.execute("PRAGMA synchronous=NORMAL")
        _DB_CONN.execute("PRAGMA temp_store=MEMORY")
        _DB_CONN.execute("PRAGMA cache_size=-64000")   # 64MB cache
    return _DB_CONN

def save_candles_batch(symbol, candles):
    if not candles:
        return 0
    with _DB_LOCK:
        conn = _get_db()
        inserted = 0
        for c in candles:
            try:
                ts = datetime.fromtimestamp(c["time"], tz=timezone.utc).isoformat()
                conn.execute(
                    "INSERT OR REPLACE INTO candles (source, symbol, timestamp, open, high, low, close, volume) VALUES ('quotex', ?, ?, ?, ?, ?, ?, ?)",
                    (symbol, ts, c["open"], c["high"], c["low"], c["close"], c["ticks"]),
                )
                inserted += 1
            except Exception as e:
                err_msg = str(e)
                if "stream not found" in err_msg or "404" in err_msg:
                    print("[quotex] Turso stream broken, reconnecting...")
                    _reset_db()
                    conn = _get_db()
                    try:
                        conn.execute(
                            "INSERT OR REPLACE INTO candles (source, symbol, timestamp, open, high, low, close, volume) VALUES ('quotex', ?, ?, ?, ?, ?, ?, ?)",
                            (symbol, ts, c["open"], c["high"], c["low"], c["close"], c["ticks"]),
                        )
                        inserted += 1
                    except Exception as e2:
                        print("[quotex] save retry failed %s: %s" % (symbol, e2))
                else:
                    print("[quotex] save error %s: %s" % (symbol, err_msg))
        try:
            conn.commit()
            turso_db.sync(conn)
        except Exception as e:
            err = str(e)
            if "stream not found" in err or "404" in err:
                _reset_db()
            print("[quotex] commit failed: %s" % err)
        return inserted


# ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ FASTER_REACTION_V1 ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬
# Per-symbol trend cache: the full trend/band arrays are computed once, then
# only the LAST bar is recomputed on every tick. The full array is refreshed
# only when a new candle starts (minute rollover).
_TREND_CACHE = {}


def _compute_full_supertrend(close, high, low, period, mult):
    """Compute ATR, SuperTrend bands, and trend in a single pass.
    Returns (trend, final_upper, final_lower, atr)."""
    n = len(close)
    if n < period + 2:
        return [0]*n, [0.0]*n, [0.0]*n, [0.0]*n

    # True range
    tr = [high[0] - low[0]]
    for i in range(1, n):
        a = high[i] - low[i]
        b = abs(high[i] - close[i-1])
        c = abs(low[i] - close[i-1])
        tr.append(a if a >= b and a >= c else (b if b >= c else c))

    # Wilder's ATR
    alpha = 1.0 / period
    atr = [tr[0]]
    for i in range(1, n):
        atr.append(atr[-1] + alpha * (tr[i] - atr[-1]))

    hl2 = [(high[i] + low[i]) * 0.5 for i in range(n)]
    bu = [hl2[i] + mult * atr[i] for i in range(n)]
    bl = [hl2[i] - mult * atr[i] for i in range(n)]

    fu = bu[:]
    fl = bl[:]
    trend = [1] * n

    for i in range(1, n):
        if bu[i] < fu[i-1] or close[i-1] > fu[i-1]:
            fu[i] = bu[i]
        else:
            fu[i] = fu[i-1]
        if bl[i] > fl[i-1] or close[i-1] < fl[i-1]:
            fl[i] = bl[i]
        else:
            fl[i] = fl[i-1]
        if close[i] > fu[i-1]:
            trend[i] = 1
        elif close[i] < fl[i-1]:
            trend[i] = -1
        else:
            trend[i] = trend[i-1]

    return trend, fu, fl, atr


def compute_and_cache_flip(app_symbol, latest_candle=None):
    """FASTER_REACTION_V2

    Ultra-fast incremental SuperTrend.
    - First call per symbol: reads DB, computes full trend, caches everything.
    - Subsequent calls: caller passes the latest candle; we update only the
      last bar in the cached arrays (O(1), no DB read).
    """
    import pandas as _pd
    period = _an.Config.ATR_PERIOD
    mult = _an.Config.MULTIPLIER

    cached = _TREND_CACHE.get(app_symbol)

    # Fast path: caller gave us the latest candle AND we have a cache
    if cached is not None and latest_candle is not None:
        try:
            ts = latest_candle["time"]
            price = latest_candle["close"]
            high_p = latest_candle["high"]
            low_p = latest_candle["low"]

            # Convert ts (int epoch) to Timestamp in UTC
            from datetime import datetime as _dt, timezone as _tz
            ts_dt = _pd.Timestamp(_dt.fromtimestamp(ts, tz=_tz.utc))

            close = cached["close"]
            high  = cached["high"]
            low   = cached["low"]
            fu    = cached["fu"]
            fl    = cached["fl"]
            atr   = cached["atr"]
            trend = cached["trend"]
            ts_list = cached["ts_list"]

            # Did the candle roll over to a new minute?
            if ts_dt != ts_list[-1]:
                # New candle ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â append a new bar and recompute it
                close.append(price)
                high.append(high_p)
                low.append(low_p)
                ts_list.append(ts_dt)
                # Grow the band/trend arrays
                fu.append(0.0)
                fl.append(0.0)
                atr.append(atr[-1])
                trend.append(trend[-1])
                cached["n"] = len(close)
            else:
                # Same candle ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â update the last bar
                close[-1] = price
                high[-1] = high_p
                low[-1] = low_p

            i = len(close) - 1
            if i == 0:
                STATE["flips"][app_symbol] = {"error": "warming up"}
                return

            # Incremental ATR[i]
            a = high[i] - low[i]
            b = abs(high[i] - close[i-1])
            c = abs(low[i] - close[i-1])
            tr_i = a if a >= b and a >= c else (b if b >= c else c)
            atr[i] = atr[i-1] + (1.0 / period) * (tr_i - atr[i-1])

            hl2_i = (high[i] + low[i]) * 0.5
            bu_i = hl2_i + mult * atr[i]
            bl_i = hl2_i - mult * atr[i]

            if bu_i < fu[i-1] or close[i-1] > fu[i-1]:
                fu[i] = bu_i
            else:
                fu[i] = fu[i-1]
            if bl_i > fl[i-1] or close[i-1] < fl[i-1]:
                fl[i] = bl_i
            else:
                fl[i] = fl[i-1]
            if close[i] > fu[i-1]:
                trend[i] = 1
            elif close[i] < fl[i-1]:
                trend[i] = -1
            else:
                trend[i] = trend[i-1]

            trend_curr = int(trend[-1])
            trend_prev = int(trend[-2])

            if trend_curr == 1:
                st_line = float(fl[-1])
            elif trend_curr == -1:
                st_line = float(fu[-1])
            else:
                st_line = float(fl[-1])

            current_price = float(close[-1])
            current_atr = float(atr[-1])
            dist_in_atr = abs(current_price - st_line) / current_atr if current_atr > 0 else 999.0
        except Exception as e:
            STATE["flips"][app_symbol] = {"error": str(e)}
            return
    else:
        # Slow path: read from DB and recompute everything
        try:
            df = _pd.read_sql_query(
                "SELECT timestamp, open, high, low, close FROM candles WHERE source='quotex' AND symbol=? ORDER BY timestamp ASC",
                turso_db.connect(), params=(app_symbol,),
            )
        except Exception as e:
            STATE["flips"][app_symbol] = {"error": str(e)}
            return
        n = len(df)
        if n < period + 4:
            STATE["flips"][app_symbol] = {"error": "warming up"}
            return

        try:
            df["timestamp"] = _pd.to_datetime(df["timestamp"])
            close = df["close"].tolist()
            high  = df["high"].tolist()
            low   = df["low"].tolist()
            ts_list = df["timestamp"].tolist()

            trend, fu, fl, atr = _compute_full_supertrend(close, high, low, period, mult)
            _TREND_CACHE[app_symbol] = {
                "n": n,
                "period": period,
                "mult": mult,
                "close": close,
                "high": high,
                "low": low,
                "ts_list": ts_list,
                "trend": trend,
                "fu": fu,
                "fl": fl,
                "atr": atr,
            }
            trend_curr = int(trend[-1])
            trend_prev = int(trend[-2])
            if trend_curr == 1:
                st_line = float(fl[-1])
            elif trend_curr == -1:
                st_line = float(fu[-1])
            else:
                st_line = float(fl[-1])
            current_price = float(close[-1])
            current_atr = float(atr[-1])
            dist_in_atr = abs(current_price - st_line) / current_atr if current_atr > 0 else 999.0
        except Exception as e:
            STATE["flips"][app_symbol] = {"error": str(e)}
            return

    now = time.time()
    existing = STATE["flips"].get(app_symbol) or {}
    latch_total = _an.Config.SIGNAL_DELAY_SECONDS + _an.Config.ENTRY_WINDOW_SECONDS

    if existing.get("direction") is not None:
        age = now - (existing.get("flip_time_epoch") or now)
        if age > latch_total:
            existing["direction"] = None
            existing["flip_time_epoch"] = None

    live_flip = (trend_curr != trend_prev) and trend_curr != 0
    live_direction = None
    if live_flip:
        live_direction = "BUY" if trend_curr == 1 else "SELL"

    with _FLIP_LOCK:
        # WHIPSAW GUARD: if a latch is active and the trend re-flips in the
        # OPPOSITE direction, invalidate the pending signal immediately.
        if existing.get("direction") is not None and live_flip:
            if live_direction != existing.get("direction"):
                try:
                    print("[copy] WHIPSAW %s: cancel %s, new %s at %s" % (
                        app_symbol, existing.get("direction"),
                        live_direction, ts_list[-1].isoformat()))
                except Exception:
                    pass
                existing["direction"] = None
                existing["flip_time_epoch"] = None

        # Fire the latch on a fresh flip
        if live_flip and existing.get("direction") is None:
            existing["flip_time_epoch"] = now
            existing["direction"] = live_direction
            try:
                print("[copy] FLIP %s %s at %s" % (
                    app_symbol, live_direction, ts_list[-1].isoformat()))
            except Exception:
                pass

    approaching = (dist_in_atr < 0.35) and (not live_flip) and (existing.get("direction") is None)

    existing["candle_ts"]       = ts_list[-1].isoformat()
    existing["curr"]            = trend_curr
    existing["prev"]            = trend_prev
    existing["live_flip"]       = live_flip
    existing["live_direction"]  = live_direction
    existing["approaching"]     = approaching
    existing["distance_in_atr"] = dist_in_atr
    existing["st_line"]         = st_line
    existing["current_price"]   = current_price
    existing["computed_at"]     = now
    STATE["flips"][app_symbol] = existing



def load_session():
    """Load Quotex session. Turso first, then fall back to local file."""
    # 1. Try Turso (survives container restarts)
    try:
        import turso_db
        conn = turso_db.connect()
        cur = conn.execute("SELECT ssid, user_agent FROM bot_session WHERE id = 1")
        row = cur.fetchone()
        conn.close()
        if row and row[0] and row[1]:
            print("[quotex] Session loaded from TURSO (ssid=%s...)" % row[0][:16])
            return row[0], row[1]
    except Exception as e:
        print("[quotex] Turso session load failed (fallback to file): %s" % e)

    # 2. Fall back to local session.json
    if SESSION_FILE.exists():
        try:
            with open(SESSION_FILE, "r") as f:
                data = json.load(f)
            token = data.get("token")
            user_agent = data.get("user_agent")
            if token and user_agent:
                print("[quotex] Session loaded from session.json (ssid=%s...)" % token[:16])
                return token, user_agent
        except Exception as e:
            print("[quotex] session.json read failed: %s" % e)

    raise RuntimeError("No session available (Turso empty + session.json missing)")


async def _fetch_history(client, app_symbol, quotex_asset):
    try:
        candles = await client.get_historical_candles(
            quotex_asset, HISTORICAL_CANDLES * 60, PERIOD,
            timeout=45, max_workers=FETCH_WORKERS,
        )
        if candles:
            n = save_candles_batch(app_symbol, candles)
            STATE["candles_collected"][app_symbol] = len(candles)
            print("[quotex] %s: loaded %d historical candles" % (app_symbol, n))
        else:
            print("[quotex] %s: empty history response" % app_symbol)
    except Exception as e:
        print("[quotex] %s history error: %s" % (app_symbol, e))


async def _stream_loop(client, app_symbol, quotex_asset):
    """FASTTICK_V4 ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â real-time tick aggregator with keep-alive writes."""
    import time as _time

    sub_ok = False
    for attempt in range(1, 6):
        try:
            await client.start_candles_stream(quotex_asset, PERIOD)
            sub_ok = True
            print("[quotex] %s subscribed (attempt %d)" % (app_symbol, attempt))
            break
        except Exception as e:
            print("[quotex] %s subscribe attempt %d failed: %s" % (app_symbol, attempt, e))
            await asyncio.sleep(2)
    if not sub_ok:
        print("[quotex] %s could not subscribe after 5 attempts; retrying in 10s" % app_symbol)

    current_minute = None
    current_candle = None
    last_tick_ts = 0.0
    last_tick_wall = _time.time()
    last_heartbeat = _time.time()

    while STATE["running"]:
        try:
            tick = client.api.realtime_candles.get(quotex_asset) if client.api else None
            now_ts = _time.time()
            now_minute = int(now_ts // 60) * 60

            price = None
            if tick and isinstance(tick, list) and len(tick) >= 3:
                tick_ts = float(tick[1])
                if tick_ts != last_tick_ts:
                    last_tick_ts = tick_ts
                    last_tick_wall = now_ts
                price = float(tick[2])

            if now_minute != current_minute:
                if current_candle is not None:
                    try:
                        save_candles_batch(app_symbol, [current_candle])
                        compute_and_cache_flip(app_symbol, current_candle)
                    except Exception as e:
                        print("[quotex] %s finalize error: %s" % (app_symbol, e))
                current_minute = now_minute
                current_candle = {"time": now_minute, "open": (price if price is not None else 0), "high": (price if price is not None else 0), "low": (price if price is not None else 0), "close": (price if price is not None else 0), "ticks": (1 if price is not None else 0)}
                try:
                    save_candles_batch(app_symbol, [current_candle])
                    compute_and_cache_flip(app_symbol, current_candle)
                except Exception as e:
                    print("[quotex] %s new-candle write error: %s" % (app_symbol, e))
            else:
                if price is not None:
                    if price > current_candle["high"]: current_candle["high"] = price
                    if price < current_candle["low"]:  current_candle["low"]  = price
                    current_candle["close"] = price
                    current_candle["ticks"] += 1
                try:
                    save_candles_batch(app_symbol, [current_candle])
                    compute_and_cache_flip(app_symbol, current_candle)
                except Exception as e:
                    print("[quotex] %s update write error: %s" % (app_symbol, e))

            STATE["last_update_by_market"][app_symbol] = now_ts

            if now_ts - last_tick_wall > 15:
                print("[quotex] %s no ticks for 15s, full reset" % app_symbol)
                try:
                    await client.stop_candles_stream(quotex_asset)
                except Exception as e:
                    print("[quotex] %s stop error: %s" % (app_symbol, e))
                await asyncio.sleep(0.5)
                try:
                    await client.start_candles_stream(quotex_asset, PERIOD)
                    print("[quotex] %s re-subscribed OK" % app_symbol)
                except Exception as e:
                    print("[quotex] %s re-subscribe failed: %s" % (app_symbol, e))
                last_tick_wall = now_ts
                last_tick_ts = 0.0

            if now_ts - last_heartbeat > 60:
                print("[quotex] %s heartbeat: last tick %.1fs ago, minute %s" % (app_symbol, now_ts - last_tick_wall, current_minute))
                last_heartbeat = now_ts

        except Exception as e:
            print("[quotex] %s unexpected error: %s" % (app_symbol, e))
            await asyncio.sleep(0.5)

        await asyncio.sleep(0.3)


def get_flip_state(app_symbol):
    return STATE["flips"].get(app_symbol) or {"error": "not computed yet"}


async def _run_all():
    token, user_agent = load_session()
    STATE["session_loaded"] = True
    print("[quotex] Session loaded. Connecting...")

    client = Quotex(email="", password="", lang="en")
    client.set_session(ssid=token, user_agent=user_agent)

    check, reason = await client.connect()
    if not check:
        STATE["connected"] = False
        STATE["error"] = "Connection failed: %s" % reason
        print("[quotex] CONNECT FAILED: %s" % reason)
        return

    STATE["connected"] = True
    STATE["error"] = None
    print("[quotex] CONNECTED")

    try:
        balance = await client.get_balance()
        print("[quotex] Demo balance: %s" % balance)
    except Exception as e:
        print("[quotex] Balance check failed: %s" % e)

    print("[quotex] Fetching historical candles (sequential, 3s apart)...")
    for app_symbol, quotex_asset in OTC_MARKETS.items():
        await _fetch_history(client, app_symbol, quotex_asset)
        await asyncio.sleep(3)

    print("[quotex] Starting WebSocket stream (real-time)...")
    await asyncio.gather(*[_stream_loop(client, s, a) for s, a in OTC_MARKETS.items()])

    # AUTO_REFRESH_V1: gather returned Ã¢â‚¬â€ either stop_collector() or force_reconnect()
    print("[quotex] Stream loop exited Ã¢â‚¬â€ closing client")
    try:
        await client.close()
    except Exception as e:
        print("[quotex] client.close() error: %s" % e)


def collector_thread_main():
    STATE["running"] = True
    while STATE["running"]:
        # Check for reconnect signal at the top of each iteration
        if _RECONNECT_EVENT.is_set():
            print("[quotex] Reconnect signal received - restarting with fresh session")
            _RECONNECT_EVENT.clear()
            STATE["refresh_pending"] = False

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run_all())
        except Exception as e:
            print("[quotex] Fatal error, retrying in 15s: %s" % e)
            STATE["connected"] = False
            STATE["error"] = str(e)
        finally:
            try:
                loop.close()
            except Exception:
                pass
        if STATE["running"]:
            # AUTO_REFRESH_V1: if this exit was due to a token refresh,
            # reconnect immediately (no 15s wait).
            if STATE.get("refresh_pending"):
                STATE["refresh_pending"] = False
                print("[quotex] Reconnecting immediately with fresh session...")
                time.sleep(1)
            else:
                time.sleep(15)


def start_collector_thread():
    init_db()
    t = threading.Thread(target=collector_thread_main, daemon=True, name="QuotexCollector")
    t.start()
    return t


def stop_collector():
    STATE["running"] = False


def init_db():
    conn = turso_db.connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            source TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (source, symbol, timestamp)
        )
    """)
    conn.commit()
    turso_db.sync(conn)
    conn.close()
