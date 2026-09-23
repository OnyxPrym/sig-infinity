"""
Sig Infinity AI -- MSS (Market Structure Shift) module
Pure price-action. Detects trend birth, not trend existence.
"""
import numpy as np
import pandas as pd


def ema(s, p):
    return s.ewm(span=p, adjust=False).mean()


def rsi(s, period=14):
    d = s.diff()
    g = d.clip(lower=0)
    l = -d.clip(upper=0)
    ag = g.ewm(alpha=1/period, adjust=False).mean()
    al = l.ewm(alpha=1/period, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def adx_like(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    up = h.diff(); dn = -l.diff()
    pdm = ((up > dn) & (up > 0)) * up
    ndm = ((dn > up) & (dn > 0)) * dn
    pdi = 100 * (pdm.rolling(period).mean() / atr.replace(0, np.nan))
    ndi = 100 * (ndm.rolling(period).mean() / atr.replace(0, np.nan))
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.rolling(period).mean().fillna(0)


def find_pivots(df, left=2, right=2):
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    pivot_highs, pivot_lows = [], []
    for i in range(left, n - right):
        if highs[i] == max(highs[i-left:i+right+1]):
            pivot_highs.append((i, float(highs[i])))
        if lows[i] == min(lows[i-left:i+right+1]):
            pivot_lows.append((i, float(lows[i])))
    return pivot_highs, pivot_lows


def market_structure_state(df, lookback=60):
    if len(df) < lookback + 5:
        return {"trend": "RANGING", "mss_bullish": False, "mss_bearish": False,
                "higher_highs": 0, "lower_lows": 0, "bars_since_mss": 999,
                "last_swing_high": None, "last_swing_low": None}
    recent = df.iloc[-lookback:].reset_index(drop=True)
    ph, pl = find_pivots(recent, left=2, right=2)
    if len(ph) < 2 or len(pl) < 2:
        return {"trend": "RANGING", "mss_bullish": False, "mss_bearish": False,
                "higher_highs": 0, "lower_lows": 0, "bars_since_mss": 999,
                "last_swing_high": None, "last_swing_low": None}
    last_high_idx, last_high_price = ph[-1]
    prev_high_idx, prev_high_price = ph[-2]
    last_low_idx, last_low_price = pl[-1]
    prev_low_idx, prev_low_price = pl[-2]
    current_close = float(recent["close"].iloc[-1])
    bars_since_last_high = len(recent) - 1 - last_high_idx
    bars_since_last_low = len(recent) - 1 - last_low_idx
    mss_bullish = current_close > last_high_price and 2 <= bars_since_last_high <= 5
    mss_bearish = current_close < last_low_price and 2 <= bars_since_last_low <= 5
    hh_count = 0
    for i in range(1, min(len(ph), 4)):
        if ph[-i][1] > ph[-i-1][1]:
            hh_count += 1
        else:
            break
    ll_count = 0
    for i in range(1, min(len(pl), 4)):
        if pl[-i][1] < pl[-i-1][1]:
            ll_count += 1
        else:
            break
    if hh_count >= 2 and last_high_price > prev_high_price:
        trend = "UP"
    elif ll_count >= 2 and last_low_price < prev_low_price:
        trend = "DOWN"
    else:
        trend = "RANGING"
    return {
        "trend": trend,
        "last_swing_high": float(last_high_price),
        "last_swing_low": float(last_low_price),
        "prev_swing_high": float(prev_high_price),
        "prev_swing_low": float(prev_low_price),
        "mss_bullish": mss_bullish,
        "mss_bearish": mss_bearish,
        "higher_highs": hh_count,
        "lower_lows": ll_count,
        "bars_since_mss": bars_since_last_high if mss_bullish else (bars_since_last_low if mss_bearish else 999),
        "current_close": current_close,
    }


def trend_birth_confirm(df, direction):
    if len(df) < 30:
        return False, "not enough data"
    state = market_structure_state(df, lookback=60)
    trend = state["trend"]
    mss_bull = state["mss_bullish"]
    mss_bear = state["mss_bearish"]
    bars = state["bars_since_mss"]
    if direction == "BUY" and not mss_bull:
        return False, f"No fresh bullish MSS (trend={trend})"
    if direction == "SELL" and not mss_bear:
        return False, f"No fresh bearish MSS (trend={trend})"
    if bars > 5:
        return False, f"MSS too old ({bars} bars)"
    if direction == "BUY" and trend == "DOWN":
        return False, "Trend is DOWN - no counter-trend BUY"
    if direction == "SELL" and trend == "UP":
        return False, "Trend is UP - no counter-trend SELL"
    return True, f"MSS {direction} confirmed (trend={trend}, bars_since_break={bars})"
