"""
Sig Infinity AI - Analysis Engine
Proprietary signal engine. Internal logic is not exposed.
"""
import logging
import numpy as np
import pandas as pd

logger = logging.getLogger("sig_infinity.analysis")


class Config:
    MIN_1M_CANDLES = 60
    FLASH_SECONDS = 15
    CONFIRM_SECONDS = 10
    SIGNAL_DELAY_SECONDS = FLASH_SECONDS + CONFIRM_SECONDS
    ENTRY_WINDOW_SECONDS = 30
    SIGNAL_DELAY_SECONDS = 10
    EXPIRY_MINUTES = 5
    RESULT_DELAY_SECONDS = 15
    ATR_PERIOD = 8
    MULTIPLIER = 2.0


def _true_range(df):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr


def _atr(df, period):
    # Wilder's smoothing (RMA) â€” matches TradingView ta.atr() and Quotex's SuperTrend.
    tr = _true_range(df)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _supertrend(df, period, multiplier):
    """
    Compute internal trend state per candle.
    Returns a Series where +1 = bullish state, -1 = bearish state.
    Proprietary - do not expose to frontend.
    """
    if len(df) < period + 2:
        return pd.Series([0] * len(df), index=df.index)

    hl2 = (df["high"] + df["low"]) / 2
    atr = _atr(df, period)
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    final_upper = upper.copy()
    final_lower = lower.copy()
    trend = pd.Series([1] * len(df), index=df.index)

    for i in range(1, len(df)):
        if pd.isna(atr.iloc[i]):
            final_upper.iloc[i] = upper.iloc[i]
            final_lower.iloc[i] = lower.iloc[i]
            trend.iloc[i] = 1
            continue

        # Adjust bands to prevent repainting (standard SuperTrend logic)
        if (upper.iloc[i] < final_upper.iloc[i-1]) or (df["close"].iloc[i-1] > final_upper.iloc[i-1]):
            final_upper.iloc[i] = upper.iloc[i]
        else:
            final_upper.iloc[i] = final_upper.iloc[i-1]

        if (lower.iloc[i] > final_lower.iloc[i-1]) or (df["close"].iloc[i-1] < final_lower.iloc[i-1]):
            final_lower.iloc[i] = lower.iloc[i]
        else:
            final_lower.iloc[i] = final_lower.iloc[i-1]

        # Determine trend direction
        if df["close"].iloc[i] > final_upper.iloc[i-1]:
            trend.iloc[i] = 1   # bullish
        elif df["close"].iloc[i] < final_lower.iloc[i-1]:
            trend.iloc[i] = -1  # bearish
        else:
            trend.iloc[i] = trend.iloc[i-1]

    return trend


def resample_ohlc(df, rule="5min"):
    d = df.set_index("timestamp")
    out = d.resample(rule).agg({"open": "first", "high": "max",
                                 "low": "min", "close": "last",
                                 "volume": "sum"}).dropna()
    return out.reset_index()


def swing_levels(df, lb=60, w=3):
    rec = df.iloc[-lb:] if len(df) > lb else df
    hi, lo = [], []
    h, l = rec["high"].values, rec["low"].values
    for i in range(w, len(rec) - w):
        if h[i] == max(h[i-w:i+w+1]): hi.append(h[i])
        if l[i] == min(l[i-w:i+w+1]): lo.append(l[i])
    lc = df["close"].iloc[-1]
    res = min([x for x in hi if x > lc], default=None)
    sup = max([x for x in lo if x < lc], default=None)
    return sup, res


def analyze_symbol(df_1m, symbol, cooldown_tracker=None, current_time=None):
    """
    STRICT SUPERTREND FLIP STRATEGY:
    - Only fires when SuperTrend changes color on the last CLOSED candle
    - Red -> Green = BUY
    - Green -> Red = SELL
    - Nothing else fires a signal

    Uses Wilder's RMA ATR (matches TradingView / Quotex SuperTrend exactly).
    """
    if len(df_1m) < Config.MIN_1M_CANDLES:
        return _wait(symbol, "Warming up - insufficient history.")

    if "timestamp" not in df_1m.columns:
        return _wait(symbol, "Data format error.")

    df = df_1m.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    # Compute SuperTrend on 1-minute candles
    try:
        trend = _supertrend(df, Config.ATR_PERIOD, Config.MULTIPLIER)
    except Exception:
        logger.exception("supertrend compute failed for %s", symbol)
        return _wait(symbol, "Internal error.")

    if len(trend) < 3:
        return _wait(symbol, "Not enough data.")

    # We look at the LAST CLOSED candle and the one before it.
    # trend[-1] = current forming candle's trend
    # trend[-2] = last closed candle's trend
    curr_state = int(trend.iloc[-1])
    prev_state = int(trend.iloc[-2])
    prev2_state = int(trend.iloc[-3])

    # STRICT RULE: only fire on an actual flip on the last closed candle
    # Meaning: trend[-2] != trend[-3] (flip happened on the closed candle)
    # AND trend[-1] == trend[-2] (currently in that new color, no second flip)
    flip_happened_on_closed = (prev_state != prev2_state) and (prev_state != 0) and (prev2_state != 0)
    still_same_color = (curr_state == prev_state)

    if not flip_happened_on_closed or not still_same_color:
        return _wait(symbol, "No SuperTrend flip.")

    # Direction: current color determines direction
    # GREEN (1) = BUY
    # RED (-1) = SELL
    if prev_state == 1:
        flip_direction = "BUY"
    elif prev_state == -1:
        flip_direction = "SELL"
    else:
        return _wait(symbol, "Invalid trend state.")

    # Cooldown to prevent re-firing same flip multiple times
    now = current_time if current_time is not None else pd.Timestamp.utcnow()
    if cooldown_tracker is not None:
        suppressed, _ = cooldown_tracker.is_suppressed(symbol, flip_direction, now)
        if suppressed:
            return _wait(symbol, "Signal already fired for this flip.")
        cooldown_tracker.record(symbol, flip_direction, now)

    last_candle_time = df["timestamp"].iloc[-2]  # last closed candle time
    if last_candle_time.tz is not None and now.tz is None:
        now = now.tz_localize(last_candle_time.tz)
    elif last_candle_time.tz is None and now.tz is not None:
        now = now.tz_localize(None)

    entry_window_opens_at = now
    entry_window_closes_at = now + pd.Timedelta(seconds=Config.ENTRY_WINDOW_SECONDS)
    expiry_time = entry_window_closes_at + pd.Timedelta(minutes=Config.EXPIRY_MINUTES)

    last_close = float(df["close"].iloc[-1])
    sup, res = swing_levels(df, 60)

    return {
        "symbol": symbol,
        "bias": flip_direction,
        "direction_candidate": flip_direction,
        "trigger_timeframe": "SETUP",
        "score": 10,
        "total": 10,
        "reason": f"SuperTrend flipped {'GREEN' if prev_state == 1 else 'RED'} on {symbol}.",
        "factors": {"supertrend_flip": True, "curr": curr_state, "prev": prev_state, "prev2": prev2_state},
        "support": float(sup) if sup is not None else None,
        "resistance": float(res) if res is not None else None,
        "price": last_close,
        "entry_price": last_close,
        "entry_time": entry_window_opens_at.isoformat(),
        "expiry_time": expiry_time.isoformat(),
        "entry_window_seconds": Config.ENTRY_WINDOW_SECONDS,
        "entry_window_opens_at": entry_window_opens_at.isoformat(),
        "entry_window_closes_at": entry_window_closes_at.isoformat(),
        "generated_at": now.isoformat(),
    }

def _wait(symbol, reason, trigger_timeframe=None, forming_direction=None):
    out = {"symbol": symbol, "bias": "WAIT", "score": 0, "total": 10,
           "reason": reason, "factors": {}}
    if trigger_timeframe:
        out["trigger_timeframe"] = trigger_timeframe
    if forming_direction:
        out["forming_direction"] = forming_direction
    return out









