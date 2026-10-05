#!/usr/bin/env python3
"""Mizan XAU/USD engine + Telegram bot (stdlib only).

Fixes vs the original confluence script:
  - No centered rolling swings (no look-ahead)
  - Confirmed fractals only
  - Wilder RSI / ATR
  - A trade needs all four: location, fresh sweep or break, real momentum, and no exhaustion
  - No new decision on Friday after 17:00 UTC
  - Daily series is resampled from the same hourly tape
  - Break-even after TP1 is documented in the alert
  - Spread + contract size in lot math

Configure the three constants below, then:

    python3 mizan_engine.py

The loop fetches COMEX gold (GC=F), analyzes the last closed 4H bar,
and sends BUY / SELL / WAIT when the signal changes.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal

TELEGRAM_BOT_TOKEN = ""  # from @BotFather
TELEGRAM_CHAT_ID = ""  # numeric chat id
POLL_SECONDS = 180
NOTIFY_WAIT = True

ACCOUNT_BALANCE = 10000.0
RISK_PCT = 0.02
CONTRACT_SIZE = 100.0  # 100 = standard lot
SPREAD = 0.40
NEWS_BLOCKED = False

UA = {"User-Agent": "MizanDesk/1.0"}


@dataclass
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read()


def spot_mid() -> float:
    payload = json.loads(
        _get("https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD")
    )
    row = payload[0]["spreadProfilePrices"][0]
    return (float(row["bid"]) + float(row["ask"])) / 2


def fetch_hourly() -> list[Candle]:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
        "?interval=60m&range=1y&includePrePost=false"
    )
    payload = json.loads(_get(url))
    result = payload["chart"]["result"][0]
    ts = result["timestamp"]
    q = result["indicators"]["quote"][0]
    out: list[Candle] = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        hi = max(o, h, l, c)
        lo = min(o, h, l, c)
        vol = q.get("volume", [0])[i] or 0
        out.append(Candle(int(t) * 1000, float(o), float(hi), float(lo), float(c), float(vol)))
    if not out:
        return out
    try:
        spot = spot_mid()
        basis = out[-1].close - spot
        if 0.05 < abs(basis) < 80:
            out = [
                Candle(c.time, c.open - basis, c.high - basis, c.low - basis, c.close - basis, c.volume)
                for c in out
            ]
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError):
        pass
    return out


def resample(candles: list[Candle], period_ms: int) -> list[Candle]:
    buckets: dict[int, Candle] = {}
    for c in candles:
        key = (c.time // period_ms) * period_ms
        prev = buckets.get(key)
        if prev is None:
            buckets[key] = Candle(key, c.open, c.high, c.low, c.close, c.volume)
        else:
            prev.high = max(prev.high, c.high)
            prev.low = min(prev.low, c.low)
            prev.close = c.close
            prev.volume += c.volume
    return [buckets[k] for k in sorted(buckets)]


def drop_incomplete(candles: list[Candle], period_ms: int) -> list[Candle]:
    if not candles:
        return candles
    now = int(time.time() * 1000)
    if now < candles[-1].time + period_ms:
        return candles[:-1]
    return candles


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rma(values: list[float], period: int) -> list[float]:
    out = [math.nan] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def wilder_rsi(closes: list[float], period: int = 14) -> list[float]:
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = rma(gains, period)
    avg_l = rma(losses, period)
    out = [math.nan] * len(closes)
    for i, (g, l) in enumerate(zip(avg_g, avg_l)):
        if math.isnan(g) or math.isnan(l):
            continue
        out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def wilder_atr(candles: list[Candle], period: int = 14) -> list[float]:
    tr = []
    for i, c in enumerate(candles):
        if i == 0:
            tr.append(c.high - c.low)
        else:
            prev = candles[i - 1].close
            tr.append(max(c.high - c.low, abs(c.high - prev), abs(c.low - prev)))
    return rma(tr, period)


def macd_hist(closes: list[float]) -> tuple[list[float], list[float], list[float]]:
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    macd = [a - b for a, b in zip(e12, e26)]
    signal = ema(macd, 9)
    hist = [a - b for a, b in zip(macd, signal)]
    return macd, signal, hist


def linreg_channel(closes: list[float], length: int = 50, std_mult: float = 2.0):
    n = len(closes)
    out = [None] * n
    if n < length:
        return out
    for i in range(length - 1, n):
        window = closes[i - length + 1 : i + 1]
        sum_x = sum_y = sum_xy = sum_xx = 0.0
        for k, y in enumerate(window):
            sum_x += k
            sum_y += y
            sum_xy += k * y
            sum_xx += k * k
        denom = length * sum_xx - sum_x * sum_x
        slope = 0.0 if denom == 0 else (length * sum_xy - sum_x * sum_y) / denom
        intercept = (sum_y - slope * sum_x) / length
        var = sum((y - (slope * k + intercept)) ** 2 for k, y in enumerate(window)) / length
        std = math.sqrt(var)
        mid = slope * (length - 1) + intercept
        out[i] = (mid, mid + std_mult * std, mid - std_mult * std, slope)
    return out


@dataclass
class Swing:
    index: int
    price: float
    confirmed_at: int


def confirmed_swings(candles: list[Candle], left: int = 2, right: int = 2):
    highs: list[Swing] = []
    lows: list[Swing] = []
    n = len(candles)
    for i in range(left, n - right):
        hi, lo = candles[i].high, candles[i].low
        is_high = is_low = True
        for k in range(i - left, i + right + 1):
            if k == i:
                continue
            if candles[k].high >= hi:
                is_high = False
            if candles[k].low <= lo:
                is_low = False
        conf = i + right
        if is_high:
            highs.append(Swing(i, hi, conf))
        if is_low:
            lows.append(Swing(i, lo, conf))
    return highs, lows


def last_swing(swings: list[Swing], bar: int, before: int) -> Swing | None:
    for s in reversed(swings):
        if s.confirmed_at <= bar and s.index < before:
            return s
    return None


def utc_hour(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).hour


def session_ok(ms: int) -> bool:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    if dt.weekday() == 4 and dt.hour >= 17:
        return False
    return 7 <= dt.hour < 21


def session_name(ms: int) -> str:
    h = utc_hour(ms)
    if 7 <= h < 12:
        return "London"
    if 12 <= h < 17:
        return "New York"
    if 17 <= h < 21:
        return "NY close"
    return "Asia"


Side = Literal["BUY", "SELL", "WAIT"]


BERLIN = ZoneInfo("Europe/Berlin")


def berlin_hm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=BERLIN).strftime("%H:%M")


def decision_window(ms: int) -> str | None:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    if dt.weekday() == 4 and dt.hour >= 16:
        return None
    if dt.hour == 8:
        return "london"
    if dt.hour == 12:
        return "ny"
    return None


def day_start(ms: int) -> int:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000)


def range_between(candles: list[Candle], start: int, end: int) -> tuple[float, float] | None:
    chosen = [c for c in candles if start <= c.time < end]
    if not chosen:
        return None
    return max(c.high for c in chosen), min(c.low for c in chosen)


def nearest_50(price: float) -> float:
    return round(price / 50) * 50


def cap_before_round(side: str, entry: float, target: float, min_travel: float) -> float:
    direction = 1 if side == "BUY" else -1
    level = math.ceil((entry + 0.01) / 50) * 50 if side == "BUY" else math.floor((entry - 0.01) / 50) * 50
    for _ in range(6):
        between = entry < level < target if side == "BUY" else target < level < entry
        if not between:
            return target
        front = level - direction * 3
        if abs(front - entry) >= min_travel:
            return front
        level += direction * 50
    return target


def structure_state(h4: list[Candle], i: int) -> dict:
    highs = []
    lows = []
    left = right = 2
    for s in range(left, i - right + 1):
        is_high = all(h4[k].high < h4[s].high for k in range(s - left, s + right + 1) if k != s)
        is_low = all(h4[k].low > h4[s].low for k in range(s - left, s + right + 1) if k != s)
        if is_high:
            highs.append((s, h4[s].high, s + right))
        if is_low:
            lows.append((s, h4[s].low, s + right))
    if not highs or not lows:
        return {"bos": "neutral", "bos_at": -1, "bos_price": None, "zone": "equilibrium", "eq": None, "hi": None, "lo": None}
    hi = highs[-1][1]
    lo = lows[-1][1]
    span = hi - lo
    eq = (hi + lo) / 2
    price = h4[i].close
    band = max(span * 0.08, 4)
    zone = "premium" if price >= eq + band else "discount" if price <= eq - band else "equilibrium"
    bos, bos_at, bos_price = "neutral", -1, None
    for k in range(max(right + 1, i - 18), i + 1):
        ph = [s for s in highs if s[2] < k]
        pl = [s for s in lows if s[2] < k]
        if ph and h4[k].close > ph[-1][1] and h4[k - 1].close <= ph[-1][1]:
            bos, bos_at, bos_price = "bullish", k, ph[-1][1]
        if pl and h4[k].close < pl[-1][1] and h4[k - 1].close >= pl[-1][1]:
            bos, bos_at, bos_price = "bearish", k, pl[-1][1]
    return {"bos": bos, "bos_at": bos_at, "bos_price": bos_price, "zone": zone, "eq": eq, "hi": hi, "lo": lo}


def parallel_setup(h4: list[Candle], atrs: list[float], i: int) -> dict | None:
    atr = atrs[i]
    if math.isnan(atr) or atr <= 0:
        return None
    highs, lows = [], []
    for s in range(max(2, i - 40), i - 1):
        if all(h4[k].high < h4[s].high for k in range(s - 2, s + 3) if k != s):
            highs.append(s)
        if all(h4[k].low > h4[s].low for k in range(s - 2, s + 3) if k != s):
            lows.append(s)
    if len(highs) < 2 or len(lows) < 2:
        return None
    best = None
    for a in range(len(highs)):
        for b in range(a + 1, len(highs)):
            i1, i2 = highs[a], highs[b]
            slope = (h4[i2].high - h4[i1].high) / (i2 - i1)
            for j in lows:
                tol = max(8.0, atr * 0.35)
                ct = cf = outside = 0
                ok = True
                for k in range(max(i1, i - 36), i + 1):
                    ceil = h4[i2].high + slope * (k - i2)
                    flor = h4[j].low + slope * (k - j)
                    if ceil - flor < 25:
                        ok = False
                        break
                    if abs(h4[k].high - ceil) <= tol:
                        ct += 1
                    if abs(h4[k].low - flor) <= tol:
                        cf += 1
                    if h4[k].close > ceil + tol or h4[k].close < flor - tol:
                        outside += 1
                if not ok or ct < 2 or cf < 2 or outside > 1:
                    continue
                ceil = h4[i2].high + slope * (i - i2)
                flor = h4[j].low + slope * (i - j)
                score = ct + cf - outside * 3
                if best is None or score > best[0]:
                    best = (score, flor, ceil, slope)
    if best is None:
        return None
    _score, flor, ceil, slope = best
    bar = h4[i]
    pad = max(4.0, atr * 0.12)
    if bar.low <= flor + atr * 0.15 and bar.close > flor and bar.close < ceil - atr * 0.2:
        sl = min(bar.low, flor) - pad
        risk = flor - sl
        if 12 <= risk <= atr * 1.1 and ceil > flor + risk * 1.2 and bar.close > sl:
            trade = {"side": "BUY", "entry": flor, "sl": sl, "tp1": (flor + ceil) / 2, "tp2": ceil, "kind": "channel", "slope": slope, "flor": flor, "ceil": ceil}
            return trade
    if bar.high >= ceil - atr * 0.15 and bar.close < ceil and bar.close > flor + atr * 0.2:
        sl = max(bar.high, ceil) + pad
        risk = sl - ceil
        if 12 <= risk <= atr * 1.1 and ceil - flor > risk * 1.2 and bar.close < sl:
            trade = {"side": "SELL", "entry": ceil, "sl": sl, "tp1": (flor + ceil) / 2, "tp2": flor, "kind": "channel", "slope": slope, "flor": flor, "ceil": ceil}
            return trade
    return None


def boundary_setup(h4: list[Candle], chs: list, atrs: list[float], i: int) -> dict | None:
    ch = chs[i]
    atr = atrs[i]
    if ch is None or math.isnan(atr) or atr <= 0:
        return None
    mid, up, dn, _slope = ch
    bar = h4[i]
    pad = max(3.0, atr * 0.10)
    width = up - dn
    if width < 20:
        return None
    utc = datetime.fromtimestamp(bar.time / 1000, tz=timezone.utc)
    if utc.weekday() == 4 and utc.hour >= 16:
        return None
    pos = (bar.close - dn) / width
    shelf = [h4[k].low for k in range(max(0, i - 8), i + 1) if h4[k].low <= dn + atr * 0.45]
    if (
        len(shelf) >= 2
        and max(shelf) - min(shelf) <= atr * 0.8
        and bar.close > dn
        and bar.low <= dn + atr * 0.15
    ):
        sl = min(shelf) - pad
        entry = dn
        risk = entry - sl
        if entry > sl + 4 and risk <= atr * 1.1 and mid > entry + risk * 0.4 and bar.close > sl:
            return {"side": "BUY", "entry": entry, "sl": sl, "tp1": mid, "tp2": up}
    prior = None
    for k in range(i - 2, max(0, i - 12) - 1, -1):
        ck = chs[k]
        if ck and h4[k].high >= ck[1] - atr * 0.55:
            prior = k
            break
    if prior is None:
        return None
    pulled = any(chs[k] and h4[k].low <= chs[k][1] - atr * 0.5 for k in range(prior + 1, i))
    retest = bar.high >= up - atr * 0.15 and bar.close < up
    if not (pulled and retest):
        return None
    sl = max(bar.high, up) + pad
    entry = up
    risk = sl - entry
    if entry < sl - 4 and risk <= atr * 1.2 and mid < entry - risk * 0.35 and dn < mid and bar.close < sl:
        return {"side": "SELL", "entry": entry, "sl": sl, "tp1": mid, "tp2": dn}
    return None


def boundary_busy(h4: list[Candle], chs: list, atrs: list[float], i: int) -> bool:
    active = None
    hit1 = False
    for j in range(max(0, i - 24), i):
        bar = h4[j]
        if active is not None:
            if active["side"] == "BUY":
                done = (not hit1 and bar.low <= active["sl"]) or bar.high >= active["tp2"] or (hit1 and bar.low <= active["entry"])
                hit_mid = bar.high >= active["tp1"]
            else:
                done = (not hit1 and bar.high >= active["sl"]) or bar.low <= active["tp2"] or (hit1 and bar.high >= active["entry"])
                hit_mid = bar.low <= active["tp1"]
            if done:
                active = None
                hit1 = False
            elif hit_mid:
                hit1 = True
                continue
            else:
                continue
        nxt = parallel_setup(h4, atrs, j)
        if nxt is not None:
            active = nxt
            hit1 = False
    return active is not None


def analyze(h4: list[Candle], d1: list[Candle], hourly: list[Candle] | None = None) -> dict:
    i = len(h4) - 1
    if i < 210 or len(d1) < 60:
        return {"signal": "WAIT", "reason": "not enough history", "time": h4[-1].time if h4 else 0}

    closes = [c.close for c in h4]
    atr = wilder_atr(h4)
    rsi = wilder_rsi(closes)
    macd, sig, hist = macd_hist(closes)
    chs = linreg_channel(closes)
    ch = chs[i]
    bar = h4[i]
    atr_val = atr[i]
    if ch is None or math.isnan(atr_val) or math.isnan(rsi[i]):
        return {"signal": "WAIT", "reason": "indicators warming", "time": bar.time}

    d_closes = [c.close for c in d1]
    d_e = ema(d_closes, min(200, max(50, len(d1) - 1)))
    d_ch = linreg_channel(d_closes, min(40, len(d1)))[-1]
    d_atr = wilder_atr(d1)[-1]
    slope_th = 0.0 if (d_ch is None or math.isnan(d_atr)) else 0.18 * d_atr / min(40, len(d1))
    bias = "neutral"
    if d_ch is not None:
        if d1[-1].close > d_e[-1] and d_ch[3] > slope_th:
            bias = "bullish"
        elif d1[-1].close < d_e[-1] and d_ch[3] < -slope_th:
            bias = "bearish"

    window = decision_window(bar.time)
    late_friday = datetime.fromtimestamp(bar.time / 1000, tz=timezone.utc).weekday() == 4 and utc_hour(bar.time) >= 16
    prev_close = h4[i - 1].close
    channel_sell = (
        not late_friday
        and ch is not None
        and bias == "bearish"
        and ch[3] < 0
        and prev_close > ch[0]
        and bar.close < ch[0]
        and bar.close < bar.open
    )
    channel_buy = (
        not late_friday
        and ch is not None
        and bias == "bullish"
        and ch[3] > 0
        and prev_close < ch[0]
        and bar.close > ch[0]
        and bar.close > bar.open
    )
    ch_prev = linreg_channel(closes)[i - 1]
    band_buy = (
        not late_friday
        and ch is not None
        and ch_prev is not None
        and prev_close < ch_prev[2]
        and bar.close > ch[2]
        and bar.close > bar.open
        and bar.low <= ch[2] + 0.5 * atr_val
    )
    band_sell = (
        not late_friday
        and ch is not None
        and ch_prev is not None
        and prev_close > ch_prev[1]
        and bar.close < ch[1]
        and bar.close < bar.open
        and bar.high >= ch[1] - 0.5 * atr_val
        and ch[1] - bar.close <= 1.1 * atr_val
    )
    day = day_start(bar.time)
    source = hourly if hourly else [c for c in h4 if datetime.fromtimestamp(c.time / 1000, tz=timezone.utc).hour == 0]
    asia = range_between(source, day, day + 7 * 60 * 60 * 1000)
    yesterday = d1[-2] if len(d1) >= 2 else None
    pool_high = (yesterday.high if window == "ny" and yesterday else None) if window == "ny" else (asia[0] if asia else None)
    pool_low = (yesterday.low if window == "ny" and yesterday else None) if window == "ny" else (asia[1] if asia else None)
    swept_high = pool_high is not None and bar.high > pool_high and bar.close < pool_high
    swept_low = pool_low is not None and bar.low < pool_low and bar.close > pool_low
    magnet = nearest_50(bar.close)
    stuck = abs(bar.close - magnet) <= 2
    macd_up = macd[i] > sig[i] and hist[i] > hist[i - 1]
    macd_dn = macd[i] < sig[i] and hist[i] < hist[i - 1]
    buy = [bool(channel_buy or band_buy or swept_low), bool(channel_buy or band_buy or macd_up), not stuck, rsi[i] <= 62]
    sell = [bool(channel_sell or band_sell or swept_high), bool(channel_sell or band_sell or macd_dn), not stuck, rsi[i] >= 38]
    buy_n, sell_n = sum(buy), sum(sell)
    asia_txt = f"Asia {asia[1]:.2f}-{asia[0]:.2f}" if asia else "Asia range unknown"
    magnet_txt = f"Nearest round number {magnet:.0f}."
    london_close = berlin_hm(day + 12 * 60 * 60 * 1000)
    ny_close = berlin_hm(day + 16 * 60 * 60 * 1000)
    window_txt = (
        f"London close {london_close} Germany"
        if window == "london"
        else f"New York close {ny_close} Germany"
        if window == "ny"
        else "upper band reclaim"
        if band_sell
        else "lower band reclaim"
        if band_buy
        else "channel midline break"
        if channel_sell or channel_buy
        else "not a decision candle"
    )
    pool_name = "yesterday" if window == "ny" else "Asia"

    signal: Side = "WAIT"
    plan: dict = {}
    reason = f"WAIT. Daily {bias}. {asia_txt}. {magnet_txt} {window_txt}."

    def make_plan(side: str, structure: float) -> dict | None:
        pad = 1.5 * atr_val
        entry = bar.close
        sl = (min(structure, entry) - pad) if side == "BUY" else (max(structure, entry) + pad)
        min_d, max_d = 0.6 * atr_val, 2.8 * atr_val
        if side == "BUY":
            sl = min(sl, entry - min_d)
            sl = max(sl, entry - max_d)
        else:
            sl = max(sl, entry + min_d)
            sl = min(sl, entry + max_d)
        dist = abs(entry - sl) + SPREAD
        if dist <= 0:
            return None
        sign = 1 if side == "BUY" else -1
        tp1 = cap_before_round(side, entry, entry + sign * dist, dist * 0.55)
        tp2 = cap_before_round(side, entry, entry + sign * dist * 2.5, dist * 1.2)
        lot = 0.5
        risk = round(lot * dist * CONTRACT_SIZE, 2)
        return {
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp1": round(tp1, 2),
            "tp2": round(tp2, 2),
            "lot": lot,
            "risk": risk,
        }

    idea = parallel_setup(h4, atr, i)
    struct = structure_state(h4, i)
    fresh = struct["bos_at"] >= i - 8
    if False and idea:
        if idea["side"] == "BUY" and (macd_dn or struct["zone"] == "premium"):
            idea = None
        elif idea and idea["side"] == "SELL" and (macd_up or (fresh and struct["bos"] == "bullish")):
            idea = None
        if idea and "slope" in idea:
            tol = max(8.0, atr_val * 0.35)
            prev = None
            for k in range(i - 1, max(0, i - 18) - 1, -1):
                ceil_k = idea["ceil"] + idea["slope"] * (k - i)
                flor_k = idea["flor"] + idea["slope"] * (k - i)
                if idea["side"] == "SELL" and h4[k].high >= ceil_k - tol:
                    prev = k
                    break
                if idea["side"] == "BUY" and h4[k].low <= flor_k + tol:
                    prev = k
                    break
            if prev is not None and (
                (idea["side"] == "SELL" and hist[i] >= hist[prev]) or (idea["side"] == "BUY" and hist[i] <= hist[prev])
            ):
                idea = None
    if False and idea and idea.get("kind") != "sweep" and not fresh:
        if idea["side"] == "BUY" and struct["zone"] == "premium":
            idea = None
        if idea and idea["side"] == "SELL" and struct["zone"] == "discount":
            idea = None
    busy = boundary_busy(h4, chs, atr, i)
    if False and idea is None and window and not stuck and not busy and not NEWS_BLOCKED:
        pad = max(3.0, 0.1 * atr_val)
        if bias == "bullish" and swept_low and macd_up and rsi[i] <= 62:
            sl = bar.low - pad
            risk = bar.close - sl
            if risk > 4 and ch is not None:
                mid, up, dn, _s = ch
                tp1 = mid if mid > bar.close + risk * 0.4 else bar.close + risk
                tp2 = up if up > tp1 else bar.close + risk * 2.5
                idea = {"side": "BUY", "entry": bar.close, "sl": sl, "tp1": tp1, "tp2": tp2, "kind": "sweep"}
        elif bias == "bearish" and swept_high and macd_dn and rsi[i] >= 38:
            sl = bar.high + pad
            risk = sl - bar.close
            if risk > 4 and ch is not None:
                mid, up, dn, _s = ch
                tp1 = mid if mid < bar.close - risk * 0.4 else bar.close - risk
                tp2 = dn if dn < tp1 else bar.close - risk * 2.5
                idea = {"side": "SELL", "entry": bar.close, "sl": sl, "tp1": tp1, "tp2": tp2, "kind": "sweep"}
    if False and idea is None and not busy and not NEWS_BLOCKED and struct["bos"] != "neutral" and struct.get("eq") is not None and 0 <= i - struct["bos_at"] <= 12 and struct["bos_at"] < i:
        pad = max(3.0, 0.1 * atr_val)
        level = struct.get("bos_price")
        if struct["bos"] == "bullish" and struct["zone"] != "premium" and rsi[i] <= 62 and level and bar.low <= level + atr_val * 0.2 and bar.low >= level - atr_val * 0.45 and bar.close > level:
            sl = bar.low - pad
            risk = level - sl
            if 4 < risk <= atr_val * 1.25:
                tp1 = struct["eq"] if struct["eq"] > level + risk * 0.3 else level + risk
                tp2 = struct["hi"] if struct["hi"] and struct["hi"] > tp1 else level + risk * 2.5
                idea = {"side": "BUY", "entry": level, "sl": sl, "tp1": tp1, "tp2": tp2, "kind": "bos"}
        elif struct["bos"] == "bearish" and struct["zone"] != "discount" and rsi[i] >= 38 and level and bar.high >= level - atr_val * 0.2 and bar.high <= level + atr_val * 0.45 and bar.close < level:
            sl = bar.high + pad
            risk = sl - level
            if 4 < risk <= atr_val * 1.25:
                tp1 = struct["eq"] if struct["eq"] < level - risk * 0.3 else level - risk
                tp2 = struct["lo"] if struct["lo"] and struct["lo"] < tp1 else level - risk * 2.5
                idea = {"side": "SELL", "entry": level, "sl": sl, "tp1": tp1, "tp2": tp2, "kind": "bos"}
    if idea and hourly:
        end = bar.time + 4 * 60 * 60 * 1000
        hours = [c for c in hourly if bar.time <= c.time < end]
        if len(hours) >= 2:
            filled = killed = False
            for hour in hours:
                if idea["side"] == "BUY":
                    if hour.low <= idea["sl"]:
                        killed = True
                    if hour.low <= idea["entry"]:
                        filled = True
                else:
                    if hour.high >= idea["sl"]:
                        killed = True
                    if hour.high >= idea["entry"]:
                        filled = True
            if not filled or killed:
                idea = None
    if NEWS_BLOCKED:
        reason = "WAIT. High-impact news is blocked."
    elif busy:
        reason = "WAIT. The previous channel trade is still open. One trade at a time."
        window_txt = "trade open"
    elif idea:
        dist = abs(idea["entry"] - idea["sl"]) + SPREAD
        lot = 0.5
        risk = round(lot * dist * CONTRACT_SIZE, 2)
        signal = idea["side"]
        plan = {
            "entry": round(idea["entry"], 2),
            "sl": round(idea["sl"], 2),
            "tp1": round(idea["tp1"], 2),
            "tp2": round(idea["tp2"], 2),
            "lot": lot,
            "risk": risk,
        }
        window_txt = "channel floor" if idea["side"] == "BUY" and idea.get("kind") != "sweep" else "ceiling retest" if idea["side"] == "SELL" and idea.get("kind") != "sweep" else window_txt
        reason = (
            "شراء عند قاع القناة. الوقف المقترح خارج القاع وتضعه أنت. نصف الصفقة عند المنتصف والباقي عند السقف."
            if idea["side"] == "BUY"
            else "بيع عند سقف القناة. الوقف المقترح خارج السقف وتضعه أنت. نصف الصفقة عند المنتصف والباقي عند القاع."
        )
    else:
        reason = "WAIT. No channel touch and no liquidity sweep. Daily bias, London or New York, MACD, RSI and round numbers are still required for a sweep."

    return {
        "signal": signal,
        "bias": bias,
        "buy_n": buy_n,
        "sell_n": sell_n,
        "time": bar.time,
        "session": window_txt,
        "close": round(bar.close, 2),
        "reason": reason,
        "bos": struct["bos"],
        "zone": struct["zone"],
        "eq": None if struct["eq"] is None else round(struct["eq"], 2),
        **plan,
    }


def format_msg(s: dict) -> str:
    side = {"BUY": "شراء BUY", "SELL": "بيع SELL"}.get(s["signal"], "انتظار WAIT")
    when = datetime.fromtimestamp(s["time"] / 1000, tz=BERLIN).strftime("%Y-%m-%d %H:%M")
    lines = [
        "ميزان — XAU/USD",
        "",
        f"الإشارة: {side}",
        f"السعر: {s.get('close')}",
        f"الشمعة: {when} بتوقيت ألمانيا",
    ]
    if s["signal"] != "WAIT":
        lines += [
            "",
            f"الدخول: {s.get('entry')}",
            f"حد الخطر: {s.get('sl')} — لا تخرج فور لمسه",
            "اخرج فقط إذا أغلقت الساعة التالية خلف هذا الحد",
            f"الخروج المقصود: {s.get('tp1')} — حوالي 200 دولار على 0.50 لوت",
            f"إذا كسر الهيكل معك: {s.get('tp2')}",
            "الحجم: 0.50 لوت",
        ]
    lines += ["", s.get("reason", "")]
    return "\n".join(lines)


def bot_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN).strip()


def chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID).strip()


def notify_wait() -> bool:
    raw = os.environ.get("NOTIFY_WAIT")
    if raw is None:
        return NOTIFY_WAIT
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def poll_seconds() -> int:
    raw = os.environ.get("POLL_SECONDS", "").strip()
    if not raw:
        return POLL_SECONDS
    try:
        return max(60, int(raw))
    except ValueError:
        return POLL_SECONDS


def telegram_send(text: str) -> None:
    token, chat = bot_token(), chat_id()
    if not token or not chat:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps(
        {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    ).encode()
    req = urllib.request.Request(
        url, data=body, headers={**UA, "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        res.read()


def analyze_hour(h4: list[Candle], hourly: list[Candle]) -> dict:
    empty = {
        "signal": "WAIT",
        "time": hourly[-1].time if hourly else 0,
        "close": round(hourly[-1].close, 2) if hourly else 0,
        "reason": "انتظار. البيانات غير كافية.",
        "entry": None,
        "sl": None,
        "tp1": None,
        "tp2": None,
        "stay": "—",
        "bos": "neutral",
    }
    if len(h4) < 30 or len(hourly) < 40:
        return empty
    now_ms = int(time.time() * 1000)
    if now_ms < hourly[-1].time + 60 * 60 * 1000:
        hourly = hourly[:-1]
    if len(hourly) < 40:
        empty["reason"] = "انتظار. شمعة الساعة لم تُغلق بعد."
        return empty
    atr = wilder_atr(h4, 14)
    seen = _fit_channel(h4, atr, len(h4) - 1)
    if seen is None:
        empty["reason"] = "انتظار. لا قناة واضحة."
        return empty
    flor, ceil, mid = seen
    span = ceil - flor
    _, _, hist = macd_hist([c.close for c in hourly])
    h = len(hourly) - 1
    hour = hourly[h]
    pos = (hour.close - flor) / span if span else 0.5
    body = abs(hour.close - hour.open)
    candle_mid = (hour.high + hour.low) / 2
    side = None
    if body >= 1.5 and pos <= 0.45 and hour.close > hour.open and hour.close >= candle_mid and hist[h] > hist[h - 1]:
        side = "BUY"
    elif body >= 1.5 and pos >= 0.55 and hour.close < hour.open and hour.close <= candle_mid and hist[h] < hist[h - 1]:
        side = "SELL"
    if side is None:
        empty["reason"] = "انتظار. لا شمعة ساعة في نصف القناة المناسب مع ماكد في نفس الجهة."
        return empty
    structure = _bos(hourly, h)
    agrees = (side == "BUY" and structure == "bullish") or (side == "SELL" and structure == "bearish")
    sign = 1 if side == "BUY" else -1
    entry = hour.close
    stop = (min(hour.low, entry - 4) - 1) if side == "BUY" else (max(hour.high, entry + 4) + 1)
    if abs(entry - stop) < 4:
        stop = entry - sign * 4
    if abs(entry - stop) > 12:
        stop = entry - sign * 12
    tp1 = entry + sign * 4
    tp2 = tp1
    if agrees:
        toward = mid > entry + 8 if side == "BUY" else mid < entry - 8
        tp2 = mid if toward else entry + sign * 10
        tp2 = min(max(tp2, entry + 8), entry + 20) if side == "BUY" else max(min(tp2, entry - 8), entry - 20)
    stay = "حتى نهاية شمعة الأربع ساعات" if agrees else "ساعة واحدة"
    verb = "شراء" if side == "BUY" else "بيع"
    return {
        "signal": side,
        "time": hour.time,
        "close": round(hour.close, 2),
        "reason": f"{verb}. شمعة الساعة مع القناة وماكد. اصبر حتى ربح 4 دولارات، حوالي 200 دولار على 0.50 لوت. لا تخرج فور لمس حد الخطر. اخرج فقط إذا أغلقت الساعة التالية خلفه.",
        "entry": round(entry, 2),
        "sl": round(stop, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "stay": stay,
        "bos": structure,
    }


def _fit_channel(h4: list[Candle], atr: list[float], i: int):
    a = atr[i]
    if math.isnan(a) or a <= 0 or i < 20:
        return None
    highs: list[int] = []
    lows: list[int] = []
    for s in range(max(2, i - 40), i - 1):
        is_high = is_low = True
        for k in range(s - 2, s + 3):
            if k == s or k < 0 or k > i:
                continue
            if h4[k].high >= h4[s].high:
                is_high = False
            if h4[k].low <= h4[s].low:
                is_low = False
        if is_high:
            highs.append(s)
        if is_low:
            lows.append(s)
    if len(highs) < 2 or len(lows) < 2:
        return None
    best = None
    for a1 in range(len(highs)):
        for b1 in range(a1 + 1, len(highs)):
            i1, i2 = highs[a1], highs[b1]
            slope = (h4[i2].high - h4[i1].high) / (i2 - i1)
            for j in lows:
                tol = max(8.0, a * 0.35)
                ct = cf = outside = 0
                ok = True
                for k in range(max(i1, i - 36), i + 1):
                    ceil = h4[i2].high + slope * (k - i2)
                    flor = h4[j].low + slope * (k - j)
                    if ceil - flor < 25:
                        ok = False
                        break
                    if abs(h4[k].high - ceil) <= tol:
                        ct += 1
                    if abs(h4[k].low - flor) <= tol:
                        cf += 1
                    if h4[k].close > ceil + tol or h4[k].close < flor - tol:
                        outside += 1
                if not ok or ct < 2 or cf < 2 or outside > 1:
                    continue
                ceil = h4[i2].high + slope * (i - i2)
                flor = h4[j].low + slope * (i - j)
                score = ct + cf - outside * 3
                if best is None or score > best[0]:
                    best = (score, flor, ceil, (flor + ceil) / 2)
    if best is None:
        return None
    return best[1], best[2], best[3]


def _bos(candles: list[Candle], i: int) -> str:
    swing_high = swing_low = None
    high_at = low_at = -1
    for s in range(max(2, i - 48), i - 1):
        is_high = is_low = True
        for k in range(s - 2, s + 3):
            if k == s or k < 0 or k > i:
                continue
            if candles[k].high >= candles[s].high:
                is_high = False
            if candles[k].low <= candles[s].low:
                is_low = False
        if is_high:
            swing_high, high_at = candles[s].high, s
        if is_low:
            swing_low, low_at = candles[s].low, s
    close = candles[i].close
    if high_at >= i - 8 and swing_high is not None and close > swing_high:
        return "bullish"
    if low_at >= i - 8 and swing_low is not None and close < swing_low:
        return "bearish"
    return "neutral"


def once() -> dict:
    hourly = drop_incomplete(fetch_hourly(), 60 * 60 * 1000)
    h4 = drop_incomplete(resample(hourly, 4 * 60 * 60 * 1000), 4 * 60 * 60 * 1000)
    return analyze_hour(h4, hourly)


def allow_send(signal: str, key: str, previous: str) -> bool:
    if key and key == previous:
        return False
    if signal != "WAIT":
        return True
    if not notify_wait():
        return False
    if not previous:
        return True
    return previous.rsplit("|", 1)[-1] != "WAIT"


def notify_once(state_path: str) -> None:
    sig = once()
    key = f"{sig['time']}|{sig['signal']}"
    print(datetime.now(timezone.utc).isoformat(), sig["signal"], sig.get("reason"))
    previous = ""
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as fh:
            previous = fh.read().strip()
    if allow_send(sig["signal"], key, previous):
        telegram_send(format_msg(sig))
        print("sent")
    else:
        print("not sent")
    parent = os.path.dirname(state_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as fh:
        fh.write(key)


def main() -> None:
    last_key = ""
    print("Mizan bot running. Ctrl+C to stop.")
    while True:
        try:
            sig = once()
            key = f"{sig['time']}|{sig['signal']}"
            print(datetime.now(timezone.utc).isoformat(), sig["signal"], sig.get("reason"))
            should = allow_send(sig["signal"], key, last_key)
            if should:
                telegram_send(format_msg(sig))
                last_key = key
                print("  sent")
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError, RuntimeError) as exc:
            print("error", exc)
        time.sleep(poll_seconds())


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"mizan ok\n"
        self.send_response(200)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def serve_health(port: int) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), _Health)
    print(f"listening on 0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    if "--once" in sys.argv:
        if not bot_token() or not chat_id():
            raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        notify_once(os.environ.get("MIZAN_STATE", ".mizan-state"))
    else:
        port_raw = os.environ.get("PORT", "").strip()
        has_creds = bool(bot_token() and chat_id())
        if port_raw:
            port = int(port_raw)
            if has_creds:
                threading.Thread(target=main, name="mizan-loop", daemon=True).start()
            else:
                print("PORT is set but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are empty")
            serve_health(port)
        elif has_creds:
            main()
        else:
            sig = once()
            print(format_msg(sig))
            print("\nNo token set — printed once. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to loop.")
