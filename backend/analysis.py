"""
Sig Infinity AI - Analysis Engine

STRICT SUPERTREND LINE FLIP STRATEGY with flash phases:
  T+0-10s  Orange flash (confirming)
  T+10-25s Green flash (ready)
  T+25s+   Entry window open (30s)
  T+55s    Entry window closes
"""
import logging
import numpy as np
import pandas as pd

logger = logging.getLogger("sig_infinity.analysis")


class Config:
    MIN_1M_CANDLES = 60
    FLASH_ORANGE_SECONDS = 10
    FLASH_GREEN_SECONDS = 15
    SIGNAL_READY_SECONDS = 25
    ENTRY_WINDOW_SECONDS = 30
    EXPIRY_MINUTES = 5
    RESULT_DELAY_SECONDS = 15
    ATR_PERIOD = 8
    MULTIPLIER = 2.0


def _true_range(df):
    h, l, c = df["high"], df["low"], df["close"]
    return pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)


def _atr(df, period):
    tr = _true_range(df)
    atr = tr.copy()
    if len(tr) < period:
        return atr.fillna(0.0)
    atr.iloc[period - 1] = tr.iloc[:period].mean()
    for i in range(period, len(tr)):
        atr.iloc[i] = (atr.iloc[i - 1] * (period - 1) + tr.iloc[i]) / period
    atr.iloc[:period - 1] = atr.iloc[period - 1]
    return atr


def _supertrend(df, period, multiplier):
    n = len(df)
    if n < period + 2:
        return pd.Series([0] * n, index=df.index)
    hl2 = (df["high"] + df["low"]) / 2
    atr = _atr(df, period)
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr
    final_upper = upper.copy()
    final_lower = lower.copy()
    trend = pd.Series([1] * n, index=df.index)
    for i in range(1, n):
        if upper.iloc[i] < final_upper.iloc[i - 1] or df["close"].iloc[i - 1] > final_upper.iloc[i - 1]:
            final_upper.iloc[i] = upper.iloc[i]
        else:
            final_upper.iloc[i] = final_upper.iloc[i - 1]
        if lower.iloc[i] > final_lower.iloc[i - 1] or df["close"].iloc[i - 1] < final_lower.iloc[i - 1]:
            final_lower.iloc[i] = lower.iloc[i]
        else:
            final_lower.iloc[i] = final_lower.iloc[i - 1]
        if df["close"].iloc[i] > final_upper.iloc[i - 1]:
            trend.iloc[i] = 1
        elif df["close"].iloc[i] < final_lower.iloc[i - 1]:
            trend.iloc[i] = -1
        else:
            trend.iloc[i] = trend.iloc[i - 1]
    return trend


def resample_ohlc(df, rule="5min"):
    d = df.set_index("timestamp")
    out = d.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    return out.reset_index()


def swing_levels(df, lb=60, w=3):
    rec = df.iloc[-lb:] if len(df) > lb else df
    hi, lo = [], []
    h, l = rec["high"].values, rec["low"].values
    for i in range(w, len(rec) - w):
        if h[i] == max(h[i - w:i + w + 1]):
            hi.append(h[i])
        if l[i] == min(l[i - w:i + w + 1]):
            lo.append(l[i])
    lc = df["close"].iloc[-1]
    res = min([x for x in hi if x > lc], default=None)
    sup = max([x for x in lo if x < lc], default=None)
    return sup, res


def compute_all_factors(df_1m):
    df = df_1m.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    trend = _supertrend(df, Config.ATR_PERIOD, Config.MULTIPLIER)
    return {
        "last_5_trend": [int(x) for x in trend.iloc[-5:]],
        "curr": int(trend.iloc[-1]),
        "prev": int(trend.iloc[-2]),
        "prev2": int(trend.iloc[-3]),
    }


def analyze_symbol(df_1m, symbol, cooldown_tracker=None, current_time=None):
    """
    Fire only on SuperTrend LINE color flip.

    RED->GREEN = BUY
    GREEN->RED = SELL
    """
    if len(df_1m) < Config.MIN_1M_CANDLES:
        return _wait(symbol, "Warming up.")

    df = df_1m.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df_closed = df.iloc[:-1].reset_index(drop=True)

    if len(df_closed) < 20:
        return _wait(symbol, "Not enough closed candles.")

    try:
        trend = _supertrend(df_closed, Config.ATR_PERIOD, Config.MULTIPLIER)
    except Exception:
        logger.exception("supertrend failed for %s", symbol)
        return _wait(symbol, "Internal error.")

    prev = int(trend.iloc[-1])
    prev2 = int(trend.iloc[-2])

    if prev == prev2 or prev == 0 or prev2 == 0:
        return _wait(symbol, "No SuperTrend color flip.")

    if prev2 == -1 and prev == 1:
        flip_direction = "BUY"
        flip_reason = "SuperTrend flipped RED to GREEN"
    elif prev2 == 1 and prev == -1:
        flip_direction = "SELL"
        flip_reason = "SuperTrend flipped GREEN to RED"
    else:
        return _wait(symbol, "Invalid SuperTrend state.")

    now = current_time if current_time is not None else pd.Timestamp.utcnow()

    if cooldown_tracker is not None:
        suppressed, _ = cooldown_tracker.is_suppressed(symbol, flip_direction, now)
        if suppressed:
            return _wait(symbol, "Signal already fired for this flip.")
        cooldown_tracker.record(symbol, flip_direction, now)

    now_epoch = now.timestamp() if hasattr(now, "timestamp") else float(now)
    entry_opens_epoch = now_epoch
    entry_closes_epoch = entry_opens_epoch + Config.ENTRY_WINDOW_SECONDS
    expiry_epoch = entry_closes_epoch + Config.EXPIRY_MINUTES * 60

    last_close = float(df["close"].iloc[-1])
    sup, res = swing_levels(df, 60)

    return {
        "symbol": symbol,
        "bias": flip_direction,
        "direction_candidate": flip_direction,
        "trigger_timeframe": "SETUP",
        "score": 10,
        "total": 10,
        "reason": flip_reason,
        "factors": {"supertrend_flip": True, "prev2": prev2, "prev": prev},
        "support": float(sup) if sup is not None else None,
        "resistance": float(res) if res is not None else None,
        "price": last_close,
        "entry_price": last_close,
        "entry_time": pd.Timestamp.fromtimestamp(entry_opens_epoch, tz="UTC").isoformat(),
        "expiry_time": pd.Timestamp.fromtimestamp(expiry_epoch, tz="UTC").isoformat(),
        "entry_window_seconds": Config.ENTRY_WINDOW_SECONDS,
        "entry_window_opens_at": pd.Timestamp.fromtimestamp(entry_opens_epoch, tz="UTC").isoformat(),
        "entry_window_closes_at": pd.Timestamp.fromtimestamp(entry_closes_epoch, tz="UTC").isoformat(),
        "generated_at": now_epoch,
    }


def _wait(symbol, reason, trigger_timeframe=None, forming_direction=None):
    out = {"symbol": symbol, "bias": "WAIT", "score": 0, "total": 10, "reason": reason, "factors": {}}
    if trigger_timeframe:
        out["trigger_timeframe"] = trigger_timeframe
    if forming_direction:
        out["forming_direction"] = forming_direction
    return out