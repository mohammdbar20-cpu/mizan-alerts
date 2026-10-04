#!/usr/bin/env python3
"""Mizan XAU/USD engine + Telegram bot (stdlib only).

Fixes vs the original confluence script:
  - No centered rolling swings (no look-ahead)
  - Confirmed fractals only
  - Wilder RSI / ATR
  - Scored execution (default 3/5) instead of 8/8 all()
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal

TELEGRAM_BOT_TOKEN = ""  # from @BotFather
TELEGRAM_CHAT_ID = ""  # numeric chat id
POLL_SECONDS = 180
NOTIFY_WAIT = True

ACCOUNT_BALANCE = 10000.0
RISK_PCT = 0.01
CONTRACT_SIZE = 100.0  # 100 = standard lot
SPREAD = 0.40
MIN_EXECUTION = 3
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
    return 7 <= utc_hour(ms) < 21


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


def analyze(h4: list[Candle], d1: list[Candle]) -> dict:
    i = len(h4) - 1
    if i < 210 or len(d1) < 60:
        return {"signal": "WAIT", "reason": "not enough history", "time": h4[-1].time if h4 else 0}

    closes = [c.close for c in h4]
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    atr = wilder_atr(h4)
    rsi = wilder_rsi(closes)
    macd, sig, hist = macd_hist(closes)
    ch = linreg_channel(closes)[i]
    highs, lows = confirmed_swings(h4)
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

    gates = session_ok(bar.time) and not NEWS_BLOCKED and bias != "neutral"
    span = ch[1] - ch[2]
    pos = (bar.close - ch[2]) / span if span > 0 else 0.5
    last_low = last_swing(lows, i, i)
    last_high = last_swing(highs, i, i)
    bull_bos = bool(last_high and bar.close > last_high.price)
    bear_bos = bool(last_low and bar.close < last_low.price)
    bull_sw = bool(last_low and bar.low < last_low.price and bar.close > last_low.price)
    bear_sw = bool(last_high and bar.high > last_high.price and bar.close < last_high.price)

    bull_fvg = bear_fvg = False
    for k in range(max(2, i - 8), i + 1):
        if h4[k].low > h4[k - 2].high:
            bot = h4[k - 2].high
            if not any(h4[t].low <= bot for t in range(k + 1, i + 1)):
                bull_fvg = True
        if h4[k].high < h4[k - 2].low:
            top = h4[k - 2].low
            if not any(h4[t].high >= top for t in range(k + 1, i + 1)):
                bear_fvg = True

    mom = bar.close - h4[i - 14].close
    macd_up = macd[i] > sig[i] or hist[i] > hist[i - 1]
    macd_dn = macd[i] < sig[i] or hist[i] < hist[i - 1]

    buy = [
        pos <= 0.42,
        bar.close > e200[i] or (e50[i] > e200[i] and bar.close > e50[i]),
        bull_sw or bull_bos or bull_fvg,
        macd_up,
        35 <= rsi[i] <= 62 and (mom >= 0 or rsi[i] > 45),
    ]
    sell = [
        pos >= 0.58,
        bar.close < e200[i] or (e50[i] < e200[i] and bar.close < e50[i]),
        bear_sw or bear_bos or bear_fvg,
        macd_dn,
        38 <= rsi[i] <= 65 and (mom <= 0 or rsi[i] < 55),
    ]
    buy_n, sell_n = sum(buy), sum(sell)

    signal: Side = "WAIT"
    plan = {}
    reason = f"WAIT bias={bias} buy={buy_n}/5 sell={sell_n}/5 session={session_name(bar.time)}"

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
        lot = max(0.01, round((ACCOUNT_BALANCE * RISK_PCT) / (dist * CONTRACT_SIZE), 2))
        sign = 1 if side == "BUY" else -1
        return {
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp1": round(entry + sign * dist, 2),
            "tp2": round(entry + sign * dist * 2.5, 2),
            "lot": lot,
            "risk": round(ACCOUNT_BALANCE * RISK_PCT, 2),
        }

    if gates and bias == "bullish" and buy_n >= MIN_EXECUTION:
        structure = last_low.price if last_low else min(bar.low, ch[2])
        p = make_plan("BUY", structure)
        if p:
            signal, plan = "BUY", p
            reason = f"Daily up + {buy_n}/5 execution. Close 50% at TP1 and move SL to entry."
    elif gates and bias == "bearish" and sell_n >= MIN_EXECUTION:
        structure = last_high.price if last_high else max(bar.high, ch[1])
        p = make_plan("SELL", structure)
        if p:
            signal, plan = "SELL", p
            reason = f"Daily down + {sell_n}/5 execution. Close 50% at TP1 and move SL to entry."

    return {
        "signal": signal,
        "bias": bias,
        "buy_n": buy_n,
        "sell_n": sell_n,
        "time": bar.time,
        "session": session_name(bar.time),
        "close": round(bar.close, 2),
        "reason": reason,
        **plan,
    }


def format_msg(s: dict) -> str:
    lines = [
        "Mizan — XAU/USD",
        "",
        f"Signal: {s['signal']}",
        f"Price: {s.get('close')}",
        f"Daily bias: {s.get('bias')}",
        f"Session: {s.get('session')}",
        f"Score: buy {s.get('buy_n')}/5 · sell {s.get('sell_n')}/5",
    ]
    if s["signal"] != "WAIT":
        lines += [
            "",
            f"Entry: {s.get('entry')}",
            f"SL: {s.get('sl')}",
            f"TP1 (1R): {s.get('tp1')} — 50% off, SL to entry",
            f"TP2 (2.5R): {s.get('tp2')}",
            f"Lot: {s.get('lot')}",
            f"Risk: ${s.get('risk')}",
        ]
    iso = datetime.fromtimestamp(s["time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines += ["", s.get("reason", ""), "", f"Candle: {iso}"]
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


def once() -> dict:
    hourly = fetch_hourly()
    h4 = drop_incomplete(resample(hourly, 4 * 60 * 60 * 1000), 4 * 60 * 60 * 1000)
    d1 = drop_incomplete(resample(hourly, 24 * 60 * 60 * 1000), 24 * 60 * 60 * 1000)
    return analyze(h4, d1)


def notify_once(state_path: str) -> None:
    sig = once()
    key = f"{sig['time']}|{sig['signal']}"
    print(datetime.now(timezone.utc).isoformat(), sig["signal"], sig.get("reason"))
    previous = ""
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as fh:
            previous = fh.read().strip()
    should = key != previous and (sig["signal"] != "WAIT" or notify_wait())
    if should:
        telegram_send(format_msg(sig))
        print("sent")
    else:
        print("unchanged, not sent")
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
            should = key != last_key and (sig["signal"] != "WAIT" or notify_wait())
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
