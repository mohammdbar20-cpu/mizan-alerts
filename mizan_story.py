#!/usr/bin/env python3
"""mizan_story — قصة الساعة لإشعار ميزان (stdlib only, no network).

Builds a short Levantine-Arabic narrative (3–6 lines) for the hourly Mizan
notification: what the last closed hourly candle did against the user's
descending H4 channel (ceiling / green midline / floor), the blue levels,
round 50s, yesterday's and today's high/low and confirmed H4 swings, plus
the next *potential* targets and the nearest level behind price.

Public API
    story_lines(h4, hourly, sig=None, live=None, eng=None, now_ms=None) -> list[str]
    story_block(h4, hourly, sig=None, live=None, eng=None, now_ms=None) -> str

* Never raises: any error -> [] / "".
* No network calls: works only on the candles passed in.
* `eng` is the engine module (needs PLAN_* constants and _plan_at). When the
  engine runs as `python mizan_engine.py` it is `__main__`, so the engine
  should pass `eng=sys.modules[__name__]`. If eng is None we look for
  `__main__` then `mizan_engine` in sys.modules (we never import/re-execute
  the engine ourselves). Indicator helpers are taken from eng when present,
  otherwise local stdlib copies are used.
"""

from __future__ import annotations

import math
import sys
import time
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo

    _BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover
    _BERLIN = timezone.utc

H1 = 60 * 60 * 1000
H4 = 4 * H1
DAY_SHIFT = 2 * H1  # trading day starts 22:00 UTC (after the daily gold break)

# ---------------------------------------------------------------- helpers


def _resolve_eng(eng):
    if eng is not None and hasattr(eng, "_plan_at"):
        return eng
    for name in ("__main__", "mizan_engine"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "_plan_at") and hasattr(mod, "PLAN_UPPER"):
            return mod
    return None


def _ema(values, period):
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _rma(values, period):
    out = [math.nan] * len(values)
    if len(values) < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def _atr(c, period=14):
    tr = []
    for i, x in enumerate(c):
        if i == 0:
            tr.append(x.high - x.low)
        else:
            p = c[i - 1].close
            tr.append(max(x.high - x.low, abs(x.high - p), abs(x.low - p)))
    return _rma(tr, period)


def _rsi(closes, period=14):
    g, l = [0.0], [0.0]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g.append(max(d, 0.0))
        l.append(max(-d, 0.0))
    ag, al = _rma(g, period), _rma(l, period)
    out = [math.nan] * len(closes)
    for i, (a, b) in enumerate(zip(ag, al)):
        if not (math.isnan(a) or math.isnan(b)):
            out[i] = 100.0 if b == 0 else 100 - 100 / (1 + a / b)
    return out


def _macd_hist(closes):
    e12, e26 = _ema(closes, 12), _ema(closes, 26)
    macd = [a - b for a, b in zip(e12, e26)]
    sig = _ema(macd, 9)
    return [a - b for a, b in zip(macd, sig)]


def _hm(ms):
    return datetime.fromtimestamp(ms / 1000, tz=_BERLIN).strftime("%H:%M")


def _day_key(ms):
    return datetime.fromtimestamp((ms + DAY_SHIFT) / 1000, tz=timezone.utc).date()


def _p(x):
    return f"{x:.0f}"


def _pick(seed, options):
    return options[seed % len(options)] if options else ""


# ------------------------------------------------------------- levels

# kind -> (rank for merging/naming, short label, long name)
_KINDS = {
    "ceil": (1, "سقف القناة", "سقف القناة الهابطة"),
    "mid": (2, "الخط الأخضر", "الخط الأخضر (نص القناة)"),
    "floor": (1, "أرض القناة", "أرض القناة"),
    "blue": (3, "الأزرق", "الخط الأزرق"),
    "pdh": (4, "قمة مبارح", "قمة مبارح"),
    "pdl": (4, "قاع مبارح", "قاع مبارح"),
    "dh": (6, "قمة اليوم", "قمة اليوم"),
    "dl": (6, "قاع اليوم", "قاع اليوم"),
    "sh": (5, "قمة 4 ساعات", "قمة 4 ساعات"),
    "sl": (5, "قاع 4 ساعات", "قاع 4 ساعات"),
    "r50": (7, "رقم مدوّر", "الرقم المدوّر"),
}

# event priority by level kind
_LEVEL_PRIO = {"ceil": 100, "floor": 100, "blue": 90, "mid": 85, "pdh": 75, "pdl": 75,
               "r50": 55, "sh": 44, "sl": 44}


class _Lvl:
    __slots__ = ("kind", "price", "prev")

    def __init__(self, kind, price, prev=None):
        self.kind = kind
        self.price = float(price)
        self.prev = float(price if prev is None else prev)

    def name(self):
        if self.kind == "blue":
            return f"الخط الأزرق {_p(self.price)}"
        if self.kind == "r50":
            return f"الرقم المدوّر {_p(self.price)}"
        return f"{_KINDS[self.kind][2]} ({_p(self.price)})"

    def tag(self):
        if self.kind in ("blue", "r50"):
            return f"{_p(self.price)} ({_KINDS[self.kind][1]})"
        return f"{_p(self.price)} ({_KINDS[self.kind][1]})"


def _merge(levels, tol):
    """Merge levels closer than tol, keeping the best-ranked name."""
    out = []
    for lv in sorted(levels, key=lambda x: x.price):
        if out and abs(lv.price - out[-1].price) <= tol:
            if _KINDS[lv.kind][0] < _KINDS[out[-1].kind][0]:
                out[-1] = lv
            continue
        out.append(lv)
    return out


# ------------------------------------------------------------- core


def _build(h4, hourly, sig, live, eng, now_ms):
    eng = _resolve_eng(eng)
    if not hourly or len(hourly) < 30:
        return []
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    hourly = [c for c in hourly if c.time + H1 <= now_ms]  # closed candles only
    h4 = [c for c in (h4 or []) if c.time + H4 <= now_ms]
    if len(hourly) < 30:
        return []
    if live is None and isinstance(sig, dict):
        live = sig.get("live")
    try:
        live = float(live) if live is not None else None
    except (TypeError, ValueError):
        live = None

    for c in hourly[-30:]:
        vals = (c.open, c.high, c.low, c.close)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in vals):
            return []
    for a_, b_ in zip(hourly[-30:], hourly[-29:]):
        if abs(b_.close / a_.close - 1) > 0.08:
            return []  # broken tape: stay silent rather than tell a wrong story
    n = len(hourly)
    cur, prev = hourly[-1], hourly[-2]
    t_cur, t_prev = cur.time + H1, prev.time + H1
    price = live if live is not None else cur.close
    seed = int(cur.time // H1)

    closes = [c.close for c in hourly]
    atr_f = getattr(eng, "wilder_atr", None) or _atr
    rsi_f = getattr(eng, "wilder_rsi", None) or _rsi
    try:
        atr = atr_f(hourly, 14)[-1]
    except Exception:
        atr = _atr(hourly, 14)[-1]
    if not atr or math.isnan(atr):
        atr = max(1.0, sum(c.high - c.low for c in hourly[-14:]) / 14)
    try:
        rsi_all = rsi_f(closes, 14)
    except Exception:
        rsi_all = _rsi(closes, 14)
    hist = _macd_hist(closes)

    # ---- channel (user's plan) at candle close
    chan = {}
    if eng is not None:
        try:
            for kind, spec in (("ceil", eng.PLAN_UPPER), ("mid", eng.PLAN_MID), ("floor", eng.PLAN_LOWER)):
                chan[kind] = (eng._plan_at(spec, t_cur), eng._plan_at(spec, t_prev), spec)
        except Exception:
            chan = {}
    blues = tuple(getattr(eng, "PLAN_BLUES", ()) or ()) if eng is not None else ()

    # ---- days
    today_key = _day_key(cur.time)
    days = {}
    for c in hourly:
        days.setdefault(_day_key(c.time), []).append(c)
    keys = sorted(days)
    today = days.get(today_key, [])
    earlier_today = today[:-1]
    pd = None
    pkeys = [k for k in keys if k < today_key]
    if pkeys:
        pc = days[pkeys[-1]]
        pd = (max(c.high for c in pc), min(c.low for c in pc))
    day_hi_before = max((c.high for c in earlier_today), default=None)
    day_lo_before = min((c.low for c in earlier_today), default=None)

    # ---- level universe
    levels = []
    for kind, (now_v, prev_v, _s) in chan.items():
        levels.append(_Lvl(kind, now_v, prev_v))
    blue_set = set()
    for b in blues:
        try:
            levels.append(_Lvl("blue", float(b)))
            blue_set.add(round(float(b)))
        except (TypeError, ValueError):
            pass
    base = math.floor(price / 50) * 50
    for k in range(-4, 6):
        r = base + 50 * k
        if round(r) not in blue_set:
            levels.append(_Lvl("r50", r))
    if pd:
        levels += [_Lvl("pdh", pd[0]), _Lvl("pdl", pd[1])]
    # confirmed H4 swings (left=2, right=2; confirmed only)
    swings = []
    if len(h4) >= 7:
        lo_i = max(2, len(h4) - 45)
        for s in range(lo_i, len(h4) - 2):
            hi = all(h4[k].high < h4[s].high for k in range(s - 2, s + 3) if k != s)
            lo = all(h4[k].low > h4[s].low for k in range(s - 2, s + 3) if k != s)
            if hi:
                swings.append(_Lvl("sh", h4[s].high))
            if lo:
                swings.append(_Lvl("sl", h4[s].low))
    if today:
        levels += [_Lvl("dh", max(c.high for c in today)), _Lvl("dl", min(c.low for c in today))]
    sw_hi = [x for x in swings if x.kind == "sh"][-4:]
    sw_lo = [x for x in swings if x.kind == "sl"][-4:]
    levels += sw_hi + sw_lo

    # ---- events on the last closed candle
    events = []  # (prio, dir, kind_of_event, payload)
    rng = cur.high - cur.low
    body = abs(cur.close - cur.open)
    eps = 0.3
    cross_lv = [lv for lv in levels if lv.kind in _LEVEL_PRIO]
    for lv in cross_lv:
        L, Lp = lv.price, lv.prev
        pr = _LEVEL_PRIO[lv.kind]
        clear = max(1.0, atr * 0.15) if lv.kind in ("r50", "sh", "sl") else 0.0
        if prev.close <= Lp and cur.close > L + clear:
            events.append((pr, 1, "x_up", lv))
            continue
        if prev.close >= Lp and cur.close < L - clear:
            events.append((pr, -1, "x_dn", lv))
            continue
        if clear and min(prev.close, cur.close) <= L <= max(prev.close, cur.close):
            continue  # marginal close through a minor level: ignore
        if lv.kind in ("sh", "sl"):
            continue  # swings: crosses only (rejections are too noisy)
        if lv.kind == "r50":
            need = max(1.0, atr * 0.15)
        else:
            need = eps
        if prev.close < Lp and cur.close < L and cur.high > L + need:
            events.append((pr - 15, -1, "rej_top", lv))
        elif prev.close > Lp and cur.close > L and cur.low < L - need:
            events.append((pr - 15, 1, "rej_bot", lv))
        elif lv.kind in ("ceil", "floor", "mid", "blue", "pdh", "pdl"):
            m = 1.5
            if prev.close < Lp and L - m <= cur.high <= L + eps and cur.close < cur.high - max(1.0, rng * 0.4):
                events.append((pr - 30, -1, "touch_top", lv))
            elif prev.close > Lp and L - eps <= cur.low <= L + m and cur.close > cur.low + max(1.0, rng * 0.4):
                events.append((pr - 30, 1, "touch_bot", lv))

    # new day high / low (only when the day already has history)
    pdh_hit = any(e[2] == "x_up" and e[3].kind == "pdh" for e in events)
    pdl_hit = any(e[2] == "x_dn" and e[3].kind == "pdl" for e in events)
    new_hi = day_hi_before is not None and len(earlier_today) >= 3 and cur.high > day_hi_before and not pdh_hit
    new_lo = day_lo_before is not None and len(earlier_today) >= 3 and cur.low < day_lo_before and not pdl_hit
    if new_hi and new_lo:
        new_hi, new_lo = cur.close >= cur.open, cur.close < cur.open
    if new_hi:
        events.append((48, 1, "day_hi", cur.high))
    if new_lo:
        events.append((48, -1, "day_lo", cur.low))
    # big candle
    big = rng > 1.5 * atr
    if big:
        events.append((58, 1 if cur.close > cur.open else -1, "big", (rng, rng / atr)))
    # long wick rejection
    up_w = cur.high - max(cur.open, cur.close)
    dn_w = min(cur.open, cur.close) - cur.low
    floor_w = max(1.5, atr * 0.35)
    if up_w >= 2 * max(body, 0.3) and up_w >= floor_w and up_w > dn_w:
        events.append((45, -1, "wick_top", up_w))
    elif dn_w >= 2 * max(body, 0.3) and dn_w >= floor_w and dn_w > up_w:
        events.append((45, 1, "wick_bot", dn_w))
    # momentum flip
    if len(hist) >= 2 and hist[-1] > 0 >= hist[-2]:
        events.append((40, 1, "macd_up", None))
    elif len(hist) >= 2 and hist[-1] < 0 <= hist[-2]:
        events.append((40, -1, "macd_dn", None))
    # RSI extremes
    r_now = rsi_all[-1] if rsi_all else math.nan
    r_prev = rsi_all[-2] if len(rsi_all) > 1 else math.nan
    if not math.isnan(r_now):
        if r_now > 70:
            events.append((42 if not (r_prev > 70) else 30, -1, "rsi_hi", r_now))
        elif r_now < 30:
            events.append((42 if not (r_prev < 30) else 30, 1, "rsi_lo", r_now))

    # ---- "held since" context: channel / blue crosses in the previous 2–6 hours
    held = None
    if chan or blues:
        for back in range(2, 7):
            if n - back - 1 < 0:
                break
            a, b = hourly[n - back - 1], hourly[n - back]
            for lv in cross_lv:
                if lv.kind not in ("ceil", "floor", "mid", "blue"):
                    continue
                spec = chan.get(lv.kind, (None, None, None))[2]
                if spec is not None:
                    La, Lb = eng._plan_at(spec, a.time + H1), eng._plan_at(spec, b.time + H1)
                else:
                    La = Lb = lv.price
                up = a.close <= La and b.close > Lb
                dn = a.close >= La and b.close < Lb
                if not (up or dn):
                    continue
                side = 1 if up else -1
                ok = True
                for c in hourly[n - back:]:
                    Lc = eng._plan_at(spec, c.time + H1) if spec is not None else lv.price
                    if (c.close - Lc) * side <= 0:
                        ok = False
                        break
                if ok and held is None:
                    held = (side, lv, back, b)
            if held:
                break
    fresh_kinds = {e[3].kind for e in events if e[2] in ("x_up", "x_dn") and isinstance(e[3], _Lvl)}
    if held and held[1].kind not in fresh_kinds:
        events.append((62, held[0], "held", held))

    # failed breakout: fresh cross against a cross made in the last 3 hours
    recent_cross = {}
    for back in range(2, 4):
        if n - back - 1 < 0:
            break
        a, b = hourly[n - back - 1], hourly[n - back]
        for kind, (_nv, _pv, spec) in chan.items():
            La, Lb = eng._plan_at(spec, a.time + H1), eng._plan_at(spec, b.time + H1)
            if a.close <= La and b.close > Lb:
                recent_cross.setdefault(kind, 1)
            elif a.close >= La and b.close < Lb:
                recent_cross.setdefault(kind, -1)
        for bl in blues:
            if a.close <= bl < b.close:
                recent_cross.setdefault(("blue", round(bl)), 1)
            elif a.close >= bl > b.close:
                recent_cross.setdefault(("blue", round(bl)), -1)
    tagged = []
    for e in events:
        if e[2] in ("x_up", "x_dn") and isinstance(e[3], _Lvl):
            key = e[3].kind if e[3].kind != "blue" else ("blue", round(e[3].price))
            d = 1 if e[2] == "x_up" else -1
            if recent_cross.get(key) == -d:
                e = (e[0] + 5, e[1], e[2] + "_fail", e[3])
        tagged.append(e)
    events = tagged

    # ---- rank + group
    events.sort(key=lambda e: -e[0])
    # overall direction
    lvl_dir = [e for e in events if e[2].startswith(("x_", "rej", "touch", "held"))]
    if lvl_dir:
        direction = lvl_dir[0][1]
    else:
        score = (1 if cur.close > cur.open else -1 if cur.close < cur.open else 0)
        score += 1 if hist[-1] > hist[-2] else -1
        three = cur.close - hourly[-4].close
        score += 1 if three > atr * 0.3 else -1 if three < -atr * 0.3 else 0
        direction = 1 if score >= 2 else -1 if score <= -2 else 0
    sig_side = (sig or {}).get("signal") if isinstance(sig, dict) else None
    if sig_side == "BUY":
        direction = 1
    elif sig_side == "SELL":
        direction = -1

    # group crosses of the same type into one sentence
    groups = []
    used = set()
    for idx, e in enumerate(events):
        if idx in used:
            continue
        if e[2] in ("x_up", "x_dn", "x_up_fail", "x_dn_fail", "rej_top", "rej_bot", "touch_top", "touch_bot"):
            base_t = e[2].replace("_fail", "")
            same = [e]
            for j in range(idx + 1, len(events)):
                f = events[j]
                if j not in used and f[2].replace("_fail", "") == base_t:
                    same.append(f)
                    used.add(j)
            # drop low-value round-number / swing duplicates when a main line is in the group
            if any(s[3].kind in ("ceil", "floor", "mid", "blue", "pdh", "pdl") for s in same):
                same = [s for s in same if s[3].kind not in ("r50", "sh", "sl")] or same
            same = same[:2]
            groups.append((e[0], e[1], base_t, same, any(s[2].endswith("_fail") for s in same)))
        else:
            groups.append((e[0], e[1], e[2], [e], False))
        used.add(idx)
    # big candle is an adjective of a cross if both exist
    has_cross = any(g[2] in ("x_up", "x_dn") for g in groups)
    big_g = next((g for g in groups if g[2] == "big"), None)
    if big_g and has_cross:
        groups = [g for g in groups if g[2] != "big"]
    # one day-high/low is enough when a stronger level event points the same way
    top = groups[:3]

    # ---- sentences
    def names(items):
        ns = [s[3].name() for s in items]
        return ns[0] if len(ns) == 1 else "، ".join(ns[:-1]) + " و" + ns[-1]

    cl = f"{cur.close:.2f}"
    strong = " بشمعة قوية" if (big_g and has_cross) else ""

    def sentence(g):
        _pr, d, t, items, fail = g
        if t == "x_up":
            if all(s_[3].kind in ("pdl", "sl") for s_ in items):
                s = f"السعر رجع فوق {names(items)}{strong} وسكّر عند {cl}"
            else:
                s = _pick(seed, [
                    f"السعر كسر {names(items)} لفوق{strong} وسكّر فوقه عند {cl}",
                    f"السعر طلع فوق {names(items)}{strong} وسكّر عند {cl}",
                ])
            if fail:
                s += " — يعني الهبوط الأخير طلع كسر كاذب"
            return s
        if t == "x_dn":
            if all(s_[3].kind in ("pdh", "sh") for s_ in items):
                s = f"السعر رجع تحت {names(items)}{strong} وسكّر عند {cl}"
            else:
                s = _pick(seed, [
                    f"السعر كسر {names(items)} لتحت{strong} وسكّر تحته عند {cl}",
                    f"السعر نزل تحت {names(items)}{strong} وسكّر عند {cl}",
                ])
            if fail:
                s += " — يعني الطلعة الأخيرة طلعت كسر كاذب"
            return s
        if t == "rej_top":
            return f"السعر جرّب {names(items)} ووصل لـ {cur.high:.2f} بس انرفض وسكّر تحته عند {cl} — رفض"
        if t == "rej_bot":
            return f"السعر نزل تحت {names(items)} لحد {cur.low:.2f} بس رجع سكّر فوقه عند {cl} — رفض"
        if t == "touch_top":
            return f"السعر لمس {names(items)} من تحت ({cur.high:.2f}) ورجع لتحت"
        if t == "touch_bot":
            return f"السعر لمس {names(items)} من فوق ({cur.low:.2f}) وارتد"
        e = items[0]
        if t == "held":
            side, lv, back, b = e[3]
            where = "فوق" if side > 0 else "تحت"
            dur = "ساعتين" if back == 2 else f"{back} ساعات"
            return f"السعر لسا ثابت {where} {lv.name()} من {dur} (من شمعة {_hm(b.time)})"
        if t == "day_hi":
            return f"السعر عمل قمة جديدة لليوم عند {e[3]:.2f}"
        if t == "day_lo":
            return f"السعر عمل قاع جديد لليوم عند {e[3]:.2f}"
        if t == "big":
            r, x = e[3]
            way = "طالعة" if d > 0 else "نازلة"
            return f"شمعة الساعة كبيرة و{way}: مداها {r:.1f}$ (حوالي {x:.1f}× المعدل)"
        if t == "wick_top":
            return f"ذيل طويل من فوق ({e[3]:.1f}$) — في ضغط بيع عند {cur.high:.2f}"
        if t == "wick_bot":
            return f"ذيل طويل من تحت ({e[3]:.1f}$) — في شراء عند {cur.low:.2f}"
        if t == "macd_up":
            return "الزخم انقلب لصاعد (هيستوغرام الماكد على الساعة صار موجب)"
        if t == "macd_dn":
            return "الزخم انقلب لهابط (هيستوغرام الماكد على الساعة صار سالب)"
        if t == "rsi_hi":
            return f"RSI الساعة {e[3]:.0f} — تشبّع شراء، ممكن ياخد استراحة"
        if t == "rsi_lo":
            return f"RSI الساعة {e[3]:.0f} — تشبّع بيع، ممكن يرتد"
        return ""

    lines = []
    title = f"🧭 قصة الساعة ({_hm(cur.time)}–{_hm(cur.time + H1)})"
    lines.append(title)

    # zone description (used when quiet)
    def zone_text(px):
        if not chan:
            return f"السعر حوالي {px:.2f}"
        u, m, f = chan["ceil"][0], chan["mid"][0], chan["floor"][0]
        near = max(2.0, atr * 0.15)
        if abs(px - u) <= near:
            return f"السعر لازق بسقف القناة ({_p(u)}) — {'فوقه' if px > u else 'تحته'} بـ {abs(px - u):.1f}$"
        if abs(px - f) <= near:
            return f"السعر لازق بأرض القناة ({_p(f)}) — {'فوقها' if px > f else 'تحتها'} بـ {abs(px - f):.1f}$"
        if abs(px - m) <= near:
            return f"السعر عم يلعب حوالين الخط الأخضر ({_p(m)})"
        if px > u:
            return f"السعر فوق سقف القناة ({_p(u)}) بـ {px - u:.1f}$"
        if px < f:
            return f"السعر تحت أرض القناة ({_p(f)}) بـ {f - px:.1f}$"
        if px >= m:
            return f"السعر بالنص العالي من القناة بين الخط الأخضر {_p(m)} والسقف {_p(u)}"
        return f"السعر بالنص الواطي من القناة بين الأرض {_p(f)} والخط الأخضر {_p(m)}"

    if top:
        main = [sentence(g) for g in top if sentence(g)]
        lines.append("📖 شو صار: " + main[0] + ".")
        if len(main) > 1:
            lines.append("➕ كمان: " + "؛ ".join(main[1:]) + ".")
    else:
        calm = "حركة هادية" if rng < atr * 0.8 else "حركة عادية بدون كسر"
        lines.append(f"📖 شو صار: {zone_text(cur.close)}، {calm} (مدى الساعة {rng:.1f}$).")

    live_break = None
    # ---- live price already moved past important levels since the close?
    if live is not None and abs(live - cur.close) >= max(1.0, atr * 0.2):
        lo_, hi_ = sorted((cur.close, live))
        passed = [lv for lv in levels if lv.kind in ("ceil", "floor", "mid", "blue", "pdh", "pdl", "r50")
                  and lo_ < lv.price < hi_]
        passed = _merge(passed, max(2.0, atr * 0.25))
        if passed:
            way = "فوق" if live > cur.close else "تحت"
            ns = [lv.name() for lv in (passed if live > cur.close else list(reversed(passed)))[:2]]
            txt = ns[0] if len(ns) == 1 else ns[0] + " و" + ns[1]
            lines.append(f"⏱️ هلق (سبوت {live:.2f}) السعر صار {way} {txt}.")
            direction = 1 if live > cur.close else -1
            ch_passed = [lv for lv in passed if lv.kind in ("ceil", "floor", "mid")]
            live_break = (ch_passed[0], direction) if ch_passed else (passed[0], direction)

    # ---- targets / support-resistance
    tol = max(2.0, atr * 0.25)
    pool = _merge([lv for lv in levels], tol)
    gap = max(1.0, atr * 0.15)
    above_all = [lv for lv in pool if lv.price > price + 0.3]
    below_all = [lv for lv in reversed(pool) if lv.price < price - 0.3]
    above = [lv for lv in above_all if lv.price > price + gap]
    below = [lv for lv in below_all if lv.price < price - gap]
    # add today's H/L as levels only for targets (not crosses)
    if direction > 0 and above:
        t = above[:2]
        tgt = " ثم ".join(lv.tag() for lv in t)
        lines.append(f"🎯 الهدف الجاي المحتمل: {tgt}." if (top or live_break) else f"🎯 لو كمّل لفوق، الهدف المحتمل: {tgt}.")
        if below_all:
            s = below_all[0]
            what = "الكسر" if (live_break or any(g[2] in ("x_up", "held") for g in top)) else "الرفض" if any(g[2] in ("rej_bot", "touch_bot") for g in top) else None
            if what:
                lines.append(f"🛡️ الدعم الأقرب {s.tag()} — إذا رجع وسكّر تحت {_p(s.price)} {what} بيسقط.")
            else:
                lines.append(f"🛡️ الدعم الأقرب {s.tag()}.")
    elif direction < 0 and below:
        t = below[:2]
        tgt = " ثم ".join(lv.tag() for lv in t)
        lines.append(f"🎯 الهدف الجاي المحتمل لتحت: {tgt}." if (top or live_break) else f"🎯 لو كمّل لتحت، الهدف المحتمل: {tgt}.")
        if above_all:
            r = above_all[0]
            what = "الكسر" if (live_break or any(g[2] in ("x_dn", "held") for g in top)) else "الرفض" if any(g[2] in ("rej_top", "touch_top") for g in top) else None
            if what:
                lines.append(f"🧱 المقاومة الأقرب {r.tag()} — إذا رجع وسكّر فوق {_p(r.price)} {what} بيسقط.")
            else:
                lines.append(f"🧱 المقاومة الأقرب {r.tag()}.")
    else:
        parts = []
        if above_all:
            parts.append(f"فوق: {above_all[0].tag()}")
        if below_all:
            parts.append(f"تحت: {below_all[0].tag()}")
        if parts:
            lines.append("🎯 الاتجاه مش واضح — أقرب مستويات: " + " • ".join(parts) + ".")

    # ---- H4 confirmation for channel breaks / holds
    brk = None
    if live_break and live_break[0].kind in ("ceil", "floor", "mid"):
        brk = live_break
    else:
        for g in top:
            if g[2] in ("x_up", "x_dn"):
                lv = next((s_[3] for s_ in g[3] if s_[3].kind in ("ceil", "floor", "mid")), None)
                if lv is not None:
                    brk = (lv, 1 if g[2] == "x_up" else -1)
                    break
            if g[2] == "held" and g[3][0][3][1].kind in ("ceil", "floor", "mid"):
                side_, lv_, back_, b_ = g[3][0][3]
                done = any((c.time + H1) % H4 == 0 and c.time + H1 < cur.time + H1 for c in hourly[n - back_:])
                if not done:
                    brk = (lv_, side_)
                break
    if brk and chan and eng is not None:
        lv, d = brk
        spec = chan[lv.kind][2]
        word = "فوق" if d > 0 else "تحت"
        short = _KINDS[lv.kind][1]
        h4_close = (cur.time // H4) * H4 + H4
        if h4_close == cur.time + H1 and live_break is None:
            if (cur.close - eng._plan_at(spec, h4_close)) * d > 0:
                lines.append(f"✅ شمعة الأربع ساعات سكّرت {word} {short} ({_p(eng._plan_at(spec, h4_close))}) — الكسر أقوى، بس ممكن يرجع يختبره.")
        else:
            if h4_close <= cur.time + H1:
                h4_close += H4
            lines.append(f"⚠️ الكسر بيتأكد بإغلاق شمعة 4 ساعات ({_hm(h4_close)}) {word} {short} عند ~{_p(eng._plan_at(spec, h4_close))}.")

    if len(lines) > 6:
        lines = [ln for ln in lines if not ln.startswith("➕")] if any(ln.startswith("➕") for ln in lines) else lines
    return lines[:6]


def story_lines(h4, hourly, sig=None, live=None, eng=None, now_ms=None) -> list:
    """Narrative lines for the hourly message. Never raises; [] on any problem."""
    try:
        return _build(h4, hourly, sig, live, eng, now_ms)
    except Exception:
        return []


def story_block(h4, hourly, sig=None, live=None, eng=None, now_ms=None) -> str:
    """Same as story_lines but joined with newlines ("" on any problem)."""
    try:
        return "\n".join(story_lines(h4, hourly, sig=sig, live=live, eng=eng, now_ms=now_ms))
    except Exception:
        return ""


if __name__ == "__main__":  # quick manual check: python mizan_story.py  (needs mizan_engine.py next to it)
    try:
        import mizan_engine as _e  # noqa: only for this manual check (network via engine)

        _hr = _e.drop_incomplete(_e.fetch_hourly(), H1)
        _h4 = _e.drop_incomplete(_e.resample(_hr, H4), H4)
        print(story_block(_h4, _hr, live=_e.live_spot(), eng=_e))
    except Exception as exc:
        print("story check failed:", exc)
