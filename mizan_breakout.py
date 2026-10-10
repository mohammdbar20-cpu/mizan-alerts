"""mizan_breakout — H4 breakout confirmation alert for Mizan (stdlib only, no network).

Sends ONE extra Telegram message (after the hourly + story messages) when a 4-hour
candle CLOSES above the user's channel ceiling (BUY idea) or below the channel floor
(SELL idea). Nothing is sent when nothing happens.

Rules
* H4 candles = the engine's resample(hourly, 4h) buckets (UTC 00/04/08/12/16/20), the same
  buckets mizan_story uses. In Berlin summer time they close 02/06/10/14/18/22; in winter
  01/05/09/13/17/21. Broker (GMT+2/+3 server) H4 charts usually close one hour later
  (03/07/11/...), so the bot can confirm a break an hour before the broker chart shows it.
* Channel line value = engine._plan_at(PLAN_UPPER / PLAN_LOWER, H4 close time).
* Event = previous H4 close inside (<= ceiling / >= floor) and this H4 close beyond.
  That definition IS the dedupe: while price stays beyond there is no new event; a new event
  needs an H4 close back inside first (re-arm). A state file + in-memory set make sure one
  event is decided only once (sent or skipped), also across restarts when the disk survives.
* Quiet hours 23:00-07:00 Berlin: an event that closes inside that window is evaluated at
  the first poll from 07:00 on. It is sent only if still valid (no H4 close back inside,
  SL and TP1 not touched since the close, last H1 close and live price still beyond the
  line), otherwise it is skipped for good.
* Freshness: an event is only considered until max(close + 4h, first allowed time + 1h).
* Public entry point maybe_alert(eng, sig) never raises.

Env
  MIZAN_BREAKOUT=true|false           (default true)
  MIZAN_BREAKOUT_RISK_USD=70          (risk per idea in $, used for the lot suggestion)
  MIZAN_BREAKOUT_STATE=<path>         (default "<MIZAN_STATE>.breakout.json")
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
H1 = 3_600_000
H4 = 4 * H1
QUIET_START = 23  # Berlin hour, inclusive
QUIET_END = 7  # Berlin hour, exclusive
BUFFER_ATR = 0.10  # SL buffer = max(BUFFER_MIN, BUFFER_ATR * H4 ATR)
BUFFER_MIN = 1.0
MAX_RISK_ATR = 1.5  # SL distance cap in H4 ATRs
MIN_CLEAR = 0.0  # close must be beyond the line by more than this ($)
SCAN_BARS = 4  # how many recent closed H4 candles are searched for an event
KEEP_DAYS = 10
CONTRACT = 100.0  # 1.00 lot = 100 oz

_DONE: set[str] = set()  # in-memory guard (decided keys), survives a broken state file


# ------------------------------------------------------------------ config

def enabled() -> bool:
    return os.environ.get("MIZAN_BREAKOUT", "true").strip().lower() in {"1", "true", "yes", "on"}


def risk_usd() -> float:
    raw = os.environ.get("MIZAN_BREAKOUT_RISK_USD", "").strip()
    try:
        val = float(raw) if raw else 70.0
        return val if val > 0 else 70.0
    except ValueError:
        return 70.0


def state_path() -> str:
    explicit = os.environ.get("MIZAN_BREAKOUT_STATE", "").strip()
    if explicit:
        return explicit
    return os.environ.get("MIZAN_STATE", ".mizan-state").strip() + ".breakout.json"


# ------------------------------------------------------------------ time

def _berlin(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=BERLIN)


def _hm(ms: int) -> str:
    return _berlin(ms).strftime("%H:%M")


def quiet(ms: int) -> bool:
    h = _berlin(ms).hour
    return h >= QUIET_START or h < QUIET_END


def first_allowed(ms: int) -> int:
    """ms itself if outside quiet hours, else the next 07:00 Berlin."""
    if not quiet(ms):
        return ms
    d = _berlin(ms)
    day = d.date() + timedelta(days=1) if d.hour >= QUIET_START else d.date()
    return int(datetime(day.year, day.month, day.day, QUIET_END, 0, tzinfo=BERLIN).timestamp() * 1000)


# ------------------------------------------------------------------ state

def load_state(path: str | None = None) -> dict:
    try:
        with open(path or state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("done"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"done": {}}


def save_state(data: dict, now_ms: int, path: str | None = None) -> None:
    path = path or state_path()
    cutoff = now_ms - KEEP_DAYS * 86_400_000
    data["done"] = {k: v for k, v in data.get("done", {}).items() if int((v or {}).get("at", 0)) >= cutoff}
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mizan-breakout-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _mark(key: str, status: str, now_ms: int, path: str | None) -> None:
    _DONE.add(key)
    try:
        data = load_state(path)
        data["done"][key] = {"status": status, "at": now_ms}
        save_state(data, now_ms, path)
    except Exception as exc:
        print("breakout state not saved", type(exc).__name__)


# ------------------------------------------------------------------ math

def _atr_at(eng, h4: list, i: int) -> float:
    try:
        a = eng.wilder_atr(h4[: i + 1], 14)[i]
        if a and not math.isnan(a) and a > 0:
            return float(a)
    except Exception:
        pass
    span = h4[max(0, i - 13): i + 1]
    return max(1.0, sum(c.high - c.low for c in span) / max(1, len(span)))


def _lines(eng, t_ms: int) -> tuple[float, float]:
    return eng._plan_at(eng.PLAN_UPPER, t_ms), eng._plan_at(eng.PLAN_LOWER, t_ms)


def find_event(eng, h4: list) -> dict | None:
    """Most recent channel break among the last SCAN_BARS closed H4 candles."""
    n = len(h4)
    for i in range(n - 1, max(0, n - SCAN_BARS) - 1, -1):
        if i < 1:
            break
        c, p = h4[i], h4[i - 1]
        up_c, lo_c = _lines(eng, c.time + H4)
        up_p, lo_p = _lines(eng, p.time + H4)
        if c.close > up_c + MIN_CLEAR and p.close <= up_p:
            return {"i": i, "side": "BUY", "line": up_c, "spec": eng.PLAN_UPPER}
        if c.close < lo_c - MIN_CLEAR and p.close >= lo_p:
            return {"i": i, "side": "SELL", "line": lo_c, "spec": eng.PLAN_LOWER}
    return None


def lot_for(risk_per_oz: float, usd: float) -> float:
    if risk_per_oz <= 0:
        return 0.01
    raw = usd / (risk_per_oz * CONTRACT)
    return max(0.01, math.floor(raw * 100 + 1e-9) / 100)


def _swings(h4: list, upto: int) -> tuple[list[float], list[float]]:
    """Confirmed H4 swing highs/lows (left=2, right=2) known at bar `upto`, last 45 bars."""
    highs, lows = [], []
    for s in range(max(2, upto - 45), upto - 1):
        if s + 2 > upto:
            break
        if all(h4[k].high < h4[s].high for k in range(s - 2, s + 3) if k != s):
            highs.append(h4[s].high)
        if all(h4[k].low > h4[s].low for k in range(s - 2, s + 3) if k != s):
            lows.append(h4[s].low)
    return highs, lows


def next_target(eng, h4: list, i: int, entry: float, side: str, atr: float) -> tuple[float, str]:
    highs, lows = _swings(h4, i)
    cands = [(float(b), "الخط الأزرق") for b in (getattr(eng, "PLAN_BLUES", ()) or ())]
    cands += [(x, "قمة 4 ساعات") for x in highs] + [(x, "قاع 4 ساعات") for x in lows]
    gap = max(1.0, 0.25 * atr)
    if side == "BUY":
        ok = [c for c in cands if c[0] > entry + gap]
        if ok:
            return min(ok, key=lambda c: c[0])
        return float(math.ceil((entry + gap) / 50) * 50), "رقم مدوّر"
    ok = [c for c in cands if c[0] < entry - gap]
    if ok:
        return max(ok, key=lambda c: c[0])
    return float(math.floor((entry - gap) / 50) * 50), "رقم مدوّر"


def build_plan(eng, h4: list, ev: dict, sig: dict | None = None) -> dict:
    i, side, line = ev["i"], ev["side"], ev["line"]
    c = h4[i]
    sign = 1 if side == "BUY" else -1
    atr = _atr_at(eng, h4, i)
    buf = max(BUFFER_MIN, BUFFER_ATR * atr)
    entry = c.close
    sl = (min(line, c.low) - buf) if side == "BUY" else (max(line, c.high) + buf)
    cap = None
    if abs(entry - sl) > MAX_RISK_ATR * atr:
        mid = (c.high + c.low) / 2
        sl_mid = mid - sign * buf
        if 0 < (entry - sl_mid) * sign <= MAX_RISK_ATR * atr:
            sl, cap = sl_mid, "mid"
        else:
            sl, cap = entry - sign * MAX_RISK_ATR * atr, "atr"
    risk = abs(entry - sl)
    usd = risk_usd()
    lot = lot_for(risk, usd)
    tgt, tgt_name = next_target(eng, h4, i, entry, side, atr)
    closes = [x.close for x in h4[: i + 1]]
    macd_txt = None
    try:
        _m, _s, hist = eng.macd_hist(closes)
        rising = hist[-1] > hist[-2]
        if side == "BUY":
            macd_txt = "ماكد الـ4 ساعات صاعد ✅" if rising else "ماكد الـ4 ساعات لسا مش صاعد ⚠️"
        else:
            macd_txt = "ماكد الـ4 ساعات هابط ✅" if not rising else "ماكد الـ4 ساعات لسا مش هابط ⚠️"
    except Exception:
        pass
    bos_txt = None
    try:
        bos = eng._bos(h4, i)
        want = "bullish" if side == "BUY" else "bearish"
        if bos == want:
            bos_txt = "كسر هيكل " + ("صاعد" if side == "BUY" else "هابط") + " على الـ4 ساعات ✅"
        elif (sig or {}).get("bos") == want:
            bos_txt = "كسر هيكل " + ("صاعد" if side == "BUY" else "هابط") + " على الساعة ✅"
    except Exception:
        pass
    rng = c.high - c.low
    body = abs(c.close - c.open)
    return {
        "key": f"{side}|{c.time}",
        "side": side,
        "open_ms": c.time,
        "close_ms": c.time + H4,
        "line": round(line, 2),
        "per_h4": ev["spec"][1] / 6.0,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "risk": round(risk, 2),
        "tp1": round(entry + sign * 2 * risk, 2),
        "tp2": round(entry + sign * 3 * risk, 2),
        "atr": round(atr, 2),
        "cap": cap,
        "lot": lot,
        "lot_risk": round(lot * risk * CONTRACT, 2),
        "risk_usd": usd,
        "target": round(tgt, 2),
        "target_name": tgt_name,
        "dist": round(abs(entry - line), 2),
        "body": round(body, 2),
        "body_pct": round(100 * body / rng) if rng > 0 else 0,
        "with_body": (c.close > c.open) == (side == "BUY"),
        "macd": macd_txt,
        "bos": bos_txt,
    }


def still_valid(eng, plan: dict, h4: list, hourly: list, now_ms: int, live: float | None) -> str | None:
    """None if still valid, else a short reason (logged only)."""
    side, sign = plan["side"], (1 if plan["side"] == "BUY" else -1)
    for c in h4:
        if c.time <= plan["open_ms"] or c.time + H4 > now_ms:
            continue
        up, lo = _lines(eng, c.time + H4)
        line = up if side == "BUY" else lo
        if (c.close - line) * sign <= 0:
            return "H4 closed back inside"
    after = [c for c in hourly if c.time >= plan["close_ms"] and c.time + H1 <= now_ms]
    for c in after:
        if (side == "BUY" and c.low <= plan["sl"]) or (side == "SELL" and c.high >= plan["sl"]):
            return "SL touched"
        if (side == "BUY" and c.high >= plan["tp1"]) or (side == "SELL" and c.low <= plan["tp1"]):
            return "TP1 already touched"
    if after:
        last = after[-1]
        up, lo = _lines(eng, last.time + H1)
        if (last.close - (up if side == "BUY" else lo)) * sign <= 0:
            return "H1 back inside"
    if live is not None:
        up, lo = _lines(eng, now_ms)
        if (live - (up if side == "BUY" else lo)) * sign <= 0:
            return "live price back inside"
        if (live - plan["sl"]) * sign <= 0:
            return "live price beyond SL"
    return None


# ------------------------------------------------------------------ text

def format_alert(plan: dict, now_ms: int, live: float | None = None, deferred: bool = False) -> str:
    buy = plan["side"] == "BUY"
    f = lambda x: f"{x:.2f}"  # noqa: E731
    head = f"🟢 <b>صفقة شراء — سعر الدخول {f(plan['entry'])}</b>" if buy else f"🔴 <b>صفقة بيع — سعر الدخول {f(plan['entry'])}</b>"
    where = "فوق سقف قناتك" if buy else "تحت أرض قناتك"
    lname = "السقف" if buy else "الأرض"
    side_word = "فوقه" if buy else "تحتها"
    span = f"{_hm(plan['open_ms'])}–{_hm(plan['close_ms'])}"
    lines = [
        head,
        f"✅ شمعة 4 ساعات ({span}) سكّرت {where} عند {f(plan['entry'])} — {lname} {f(plan['line'])}، يعني {side_word} بـ {plan['dist']:.2f}$",
        "🧪 فكرة تجريبية — لسا ما انفحصت تاريخياً",
    ]
    if deferred:
        now_txt = f"، والسعر هلق {f(live)}" if live is not None else ""
        lines.append(f"⏰ الشمعة سكّرت {_hm(plan['close_ms'])} بوقت الهدوء (23:00–07:00). هلق {_hm(now_ms)} الفكرة لسا صالحة{now_txt}.")
    limit = "Buy Limit" if buy else "Sell Limit"
    sl_word = "تحت الدعم الجديد" if buy else "فوق المقاومة الجديدة"
    lines += [
        "",
        "📋 <b>تفاصيل الصفقة</b>",
        f"• الدخول: ماركت {f(plan['entry'])} (أو {limit} عند ~{f(plan['line'])} إذا رجع يختبر الخط المكسور)",
        f"• وقف الخسارة: {f(plan['sl'])} {sl_word} ({plan['risk']:.2f}$ بالأونصة)",
    ]
    if plan.get("cap") == "mid":
        lines.append(f"  ↳ الوقف الطبيعي كان أبعد من {MAX_RISK_ATR:g}× معدل حركة الـ4 ساعات، فحطّيناه تحت نص الشمعة" if buy else f"  ↳ الوقف الطبيعي كان أبعد من {MAX_RISK_ATR:g}× معدل حركة الـ4 ساعات، فحطّيناه فوق نص الشمعة")
    elif plan.get("cap") == "atr":
        lines.append(f"  ↳ الوقف الطبيعي كان بعيد كتير، فقصّيناه لـ {MAX_RISK_ATR:g}× معدل حركة الـ4 ساعات ({plan['atr']:.2f}$)")
    lines += [
        f"• الهدف 1 (2R): {f(plan['tp1'])}",
        f"• الهدف 2 (3R): {f(plan['tp2'])}",
        f"• اللوت: {plan['lot']:.2f} ← خطر حوالي {plan['lot_risk']:.0f}$ (الحد {plan['risk_usd']:.0f}$)",
        "• القاعدة: لا تخاطر بأكتر من 0.25–0.5% من حسابك بالصفقة الوحدة",
        "",
        "📐 <b>المستويات</b>",
    ]
    move = abs(plan["per_h4"])
    way = "نازل" if plan["per_h4"] < 0 else "طالع"
    lines.append(
        (f"• سقف القناة المكسور {f(plan['line'])} صار دعم" if buy else f"• أرض القناة المكسورة {f(plan['line'])} صارت مقاومة")
        + f" (الخط {way} حوالي {move:.1f}$ كل 4 ساعات)"
    )
    tgt_line = f"• الهدف الهيكلي الجاي: {f(plan['target'])} ({plan['target_name']})"
    if (buy and plan["target"] < plan["tp1"]) or (not buy and plan["target"] > plan["tp1"]):
        tgt_line += " — قبل الهدف 1، ممكن يوقف عنده"
    lines.append(tgt_line)
    lines += [
        "",
        "✅ <b>أسباب</b>",
        f"• إغلاق شمعة 4 ساعات {'فوق السقف' if buy else 'تحت الأرض'} (مش بس ذيل)",
        f"• المسافة عن الخط: {plan['dist']:.2f}$ (≈{plan['dist'] / plan['atr']:.1f}× معدل حركة الـ4 ساعات)" if plan["atr"] else f"• المسافة عن الخط: {plan['dist']:.2f}$",
        f"• جسم الشمعة: {plan['body']:.2f}$ ({plan['body_pct']}% من مداها)" + (" — شمعة قوية" if plan["with_body"] and plan["body_pct"] >= 60 else "" if plan["with_body"] else " — بس الشمعة عكس الاتجاه ⚠️"),
    ]
    if plan.get("macd"):
        lines.append(f"• {plan['macd']}")
    if plan.get("bos"):
        lines.append(f"• {plan['bos']}")
    lines += [
        "",
        "💡 <b>التنفيذ</b>",
        "• عند الهدف 1 سكّر نص الصفقة وانقل الوقف لسعر الدخول",
        f"• إذا سكّرت شمعة ساعة رجوع جوّا القناة ({'تحت' if buy else 'فوق'} ~{f(plan['line'])}) الفكرة بتنلغي",
        "",
        "ℹ️ تلميح مش أمر — القرار إلك.",
    ]
    return "\n".join(lines)


def _plain(text: str) -> str:
    return re.sub(r"</?b>", "", text)


# ------------------------------------------------------------------ journal

def journal_record(plan: dict, now_ms: int, deferred: bool) -> None:
    """Adds the alert to mizan_trust's journal under 'breakouts'. Never raises."""
    try:
        import mizan_trust

        data = mizan_trust.load_journal()
        rows = data.setdefault("breakouts", {})
        rows[plan["key"]] = {
            "side": plan["side"],
            "close_ms": plan["close_ms"],
            "sent_at": now_ms,
            "entry": plan["entry"],
            "sl": plan["sl"],
            "tp1": plan["tp1"],
            "tp2": plan["tp2"],
            "lot": plan["lot"],
            "deferred": deferred,
        }
        cutoff = now_ms - KEEP_DAYS * 86_400_000
        data["breakouts"] = {k: v for k, v in rows.items() if int(v.get("sent_at") or 0) >= cutoff}
        mizan_trust.save_journal(data, now_ms=now_ms)
    except Exception as exc:
        print("breakout journal skipped", type(exc).__name__)


# ------------------------------------------------------------------ entry

def maybe_alert(eng, sig: dict, now_ms: int | None = None, send=None, path: str | None = None) -> str | None:
    """Call after the hourly + story messages. Returns the text sent, else None. Never raises."""
    try:
        if not enabled():
            return None
        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        h4, hourly = (sig or {}).get("_bars") or ([], [])
        h4 = [c for c in h4 if c.time + H4 <= now_ms]
        hourly = [c for c in hourly if c.time + H1 <= now_ms]
        if len(h4) < 20 or not hourly:
            return None
        ev = find_event(eng, h4)
        if ev is None:
            return None
        plan = build_plan(eng, h4, ev, sig)
        key = plan["key"]
        if key in _DONE or key in load_state(path).get("done", {}):
            return None
        ok_from = first_allowed(plan["close_ms"])
        deadline = max(plan["close_ms"] + H4, ok_from + H1)
        if now_ms >= deadline:
            print("breakout too old", key)
            _mark(key, "stale", now_ms, path)
            return None
        if quiet(now_ms) or now_ms < ok_from:
            print("breakout waits for 07:00", key)
            return None
        live = (sig or {}).get("live")
        try:
            live = float(live) if live is not None else None
        except (TypeError, ValueError):
            live = None
        why = still_valid(eng, plan, h4, hourly, now_ms, live)
        if why:
            print("breakout skipped", key, why)
            _mark(key, "skipped", now_ms, path)
            return None
        deferred = quiet(plan["close_ms"])
        text = format_alert(plan, now_ms, live, deferred)
        sender = send or eng.telegram_send
        try:
            sender(text, parse_mode="HTML")
        except TypeError:
            sender(_plain(text))
        _mark(key, "sent", now_ms, path)
        journal_record(plan, now_ms, deferred)
        print("breakout sent", key)
        return text
    except Exception as exc:
        print("breakout failed", type(exc).__name__, exc)
        return None
