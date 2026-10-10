"""Mizan trust layer (stdlib only).

1. Results log + daily report at 23:00 Europe/Berlin (Mon-Fri).
2. Timeframe agreement line (H4 / H1 / 15m) for the hourly message.
3. Invalidation line for BUY / SELL.

Storage design (Render disk is ephemeral):
- A small JSON journal (MIZAN_JOURNAL, default "<MIZAN_STATE>.journal.json") keeps every
  hourly result the bot actually sent (BUY / SELL / WAIT, entry, SL, TP). It is only a cache.
- Outcomes are NEVER stored as truth. At report time they are recomputed from candles
  (1m -> 5m -> 1h, finest that covers the signal), so a restart never loses a result.
- Hours missing from the journal (bot restarted / redeployed) are rebuilt by re-running
  the engine's analyze_hour() on the hourly candles as they were at that hour, and are
  flagged "rebuilt" in the report.

Every public function takes the engine module as `eng` (same pattern as mizan_story),
so this file never imports mizan_engine itself.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
M1, M5, M15, H1, H4 = 60_000, 300_000, 900_000, 3_600_000, 14_400_000
LOT = 0.5  # the size every Mizan message uses
REPORT_HOUR = 23  # Berlin, gold daily close
REPORT_GRACE_MIN = 45  # report may go out 23:00-23:44 Berlin (covers a late poll / short restart)
KEEP_DAYS = 10
MIN_GAP = 2.0  # an invalidation level must be at least this far from entry
AR_DAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


# ------------------------------------------------------------------ helpers

def _now_ms(now_ms: int | None = None) -> int:
    return int(time.time() * 1000) if now_ms is None else int(now_ms)


def _berlin(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=BERLIN)


def _hm(ms: int) -> str:
    return _berlin(ms).strftime("%H:%M")


def _fmt(p: float) -> str:
    return f"{p:.2f}"


def enabled() -> bool:
    raw = os.environ.get("MIZAN_DAILY_REPORT", "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ------------------------------------------------------------------ journal

def journal_path() -> str:
    explicit = os.environ.get("MIZAN_JOURNAL", "").strip()
    if explicit:
        return explicit
    return os.environ.get("MIZAN_STATE", ".mizan-state").strip() + ".journal.json"


def load_journal(path: str | None = None) -> dict:
    path = path or journal_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("signals", {})
            data.setdefault("reports", {})
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "signals": {}, "reports": {}}


def save_journal(data: dict, path: str | None = None, now_ms: int | None = None) -> None:
    path = path or journal_path()
    cutoff = _now_ms(now_ms) - KEEP_DAYS * 86_400_000
    data["signals"] = {k: v for k, v in data.get("signals", {}).items() if int(k) >= cutoff}
    keep_dates = {(_berlin(_now_ms(now_ms)) - timedelta(days=d)).date().isoformat() for d in range(KEEP_DAYS)}
    data["reports"] = {k: v for k, v in data.get("reports", {}).items() if k in keep_dates}
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mizan-journal-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)  # atomic: a crash never leaves half a file
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _row(sig: dict, src: str) -> dict:
    return {
        "time": int(sig.get("time") or 0),
        "signal": sig.get("signal", "WAIT"),
        "entry": sig.get("entry"),
        "sl": sig.get("sl"),
        "tp": sig.get("tp1"),
        "tf": sig.get("tf_state"),
        "inval": sig.get("inval"),
        "src": src,
    }


def record(sig: dict, path: str | None = None) -> None:
    """Call after the hourly message was sent. Never raises."""
    try:
        if not sig.get("time"):
            return
        data = load_journal(path)
        data["signals"][str(int(sig["time"]))] = _row(sig, "live")
        save_journal(data, path)
    except Exception as exc:  # the log must never break the alert
        print("journal skipped", type(exc).__name__)


# ------------------------------------------------------------ 2. agreement

def trend(eng, bars: list) -> int | None:
    """+1 up, -1 down, 0 flat, None unknown.

    Up   = last close above EMA20 AND EMA20 higher than 3 bars ago.
    Down = last close below EMA20 AND EMA20 lower than 3 bars ago.
    Anything else = flat (mixed).
    """
    if len(bars) < 25:
        return None
    closes = [c.close for c in bars]
    e20 = eng.ema(closes, 20)
    slope = e20[-1] - e20[-4]
    if closes[-1] > e20[-1] and slope > 0:
        return 1
    if closes[-1] < e20[-1] and slope < 0:
        return -1
    return 0


def frame_trends(eng, sig: dict, bars15: list | None) -> dict:
    """Trends as they were when the signal hour closed (closed candles only)."""
    h4, hourly = sig.get("_bars") or ([], [])
    t_close = int(sig["time"]) + H1
    return {
        "h4": trend(eng, [c for c in h4 if c.time + H4 <= t_close]),
        "h1": trend(eng, [c for c in hourly if c.time + H1 <= t_close]),
        "m15": None if bars15 is None else trend(eng, [c for c in bars15 if c.time + M15 <= t_close]),
    }


_NAMES = (("h4", "الـ4 ساعات"), ("h1", "الساعة"), ("m15", "الربع ساعة"))
_ARROW = {1: "⬆️", -1: "⬇️", 0: "↔️", None: "؟"}


def agreement(signal: str, tr: dict) -> tuple[str, str]:
    """Returns (state, line). state is 'strong' / 'weak' / 'info'."""
    if signal not in ("BUY", "SELL"):
        short = {"h4": "4س", "h1": "ساعة", "m15": "ربع"}
        bits = " • ".join(f"{short[k]} {_ARROW[tr.get(k)]}" for k, _ in _NAMES)
        return "info", f"📐 الاتجاهات: {bits}"
    want = 1 if signal == "BUY" else -1
    if all(tr.get(k) == want for k, _ in _NAMES):
        return "strong", "📐 التوافق: قوية ✅ (الـ4 ساعات والساعة والربع ساعة متفقين)"
    bad = []
    for k, name in _NAMES:
        v = tr.get(k)
        if v == want:
            continue
        bad.append(f"{name} {'عكسك' if v == -want else 'محايد' if v == 0 else 'ما انقرا'}")
    return "weak", f"📐 التوافق: ضعيفة ⚠️ استنى — {'، '.join(bad)}"


# ----------------------------------------------------------- 3. invalidation

def invalidation(eng, sig: dict) -> tuple[float, str] | None:
    """Nearest structural level on the losing side, between entry and SL.

    Candidates (valid at the close of the NEXT hour, because that is the candle that decides):
    green midline, your channel floor/ceiling, blue lines, the engine's fitted H4 floor/ceiling.
    BUY  -> highest candidate with  SL <= level <= entry - MIN_GAP.
    SELL -> lowest  candidate with  entry + MIN_GAP <= level <= SL.
    None qualify -> the SL itself (the engine already says: exit only on a close beyond it).
    """
    side, entry, sl = sig.get("signal"), sig.get("entry"), sig.get("sl")
    if side not in ("BUY", "SELL") or entry is None or sl is None:
        return None
    t = int(sig["time"]) + 2 * H1
    cands = [
        (eng._plan_at(eng.PLAN_MID, t), "الخط الأخضر"),
        (eng._plan_at(eng.PLAN_LOWER, t), "أرض قناتك"),
        (eng._plan_at(eng.PLAN_UPPER, t), "سقف قناتك"),
    ]
    cands += [(float(b), "الخط الأزرق") for b in eng.PLAN_BLUES]
    if sig.get("flor") is not None:
        cands.append((float(sig["flor"]), "أرض قناة الـ4 ساعات"))
    if sig.get("ceil") is not None:
        cands.append((float(sig["ceil"]), "سقف قناة الـ4 ساعات"))
    if side == "BUY":
        ok = [c for c in cands if sl <= c[0] <= entry - MIN_GAP]
        pick = max(ok, key=lambda c: c[0]) if ok else (float(sl), "حد الخطر")
    else:
        ok = [c for c in cands if entry + MIN_GAP <= c[0] <= sl]
        pick = min(ok, key=lambda c: c[0]) if ok else (float(sl), "حد الخطر")
    return round(pick[0], 2), pick[1]


def invalidation_line(signal: str, level: float, label: str) -> str:
    word = "تحت" if signal == "BUY" else "فوق"
    return f"❌ الفكرة بتنتهي إذا سكّرت شمعة ساعة {word} {_fmt(level)} ({label})"


def enrich(eng, sig: dict, bars15: list | None = None, fetch: bool = True) -> dict:
    """Adds tf_state / tf_line / inval / inval_line to the signal dict. Never raises."""
    try:
        if not sig.get("time") or not sig.get("_bars"):
            return sig
        if bars15 is None and fetch:
            try:
                bars15 = eng.fetch_bars("15m", 200)
            except Exception as exc:
                print("15m skipped", type(exc).__name__)
        state, line = agreement(sig["signal"], frame_trends(eng, sig, bars15))
        sig["tf_state"], sig["tf_line"] = state, line
        inv = invalidation(eng, sig)
        if inv:
            sig["inval"] = inv[0]
            sig["inval_line"] = invalidation_line(sig["signal"], inv[0], inv[1])
    except Exception as exc:
        print("trust lines skipped", type(exc).__name__)
    return sig


# --------------------------------------------------------------- outcomes

def resolve(side: str, entry: float, sl: float, tp: float, start_ms: int, series: list, now_ms: int) -> dict:
    """series = [(period_ms, bars, name)] finest first. First series that covers start_ms wins.

    Walk closed bars after the signal candle closed. TP touched -> win, SL touched -> loss,
    both inside the same bar -> loss (conservative, we cannot know the order).
    """
    for period, bars, name in series:
        if not bars:
            continue
        closed = [c for c in bars if c.time + period <= now_ms]
        if not closed or closed[0].time > start_ms:
            continue  # this resolution does not reach back far enough
        for c in closed:
            if c.time < start_ms:
                continue
            if side == "BUY":
                hit_sl, hit_tp = c.low <= sl, c.high >= tp
            else:
                hit_sl, hit_tp = c.high >= sl, c.low <= tp
            if hit_sl:
                return {"result": "loss", "pts": -round(abs(entry - sl), 2), "at": c.time + period, "how": name, "both": hit_tp}
            if hit_tp:
                return {"result": "win", "pts": round(abs(tp - entry), 2), "at": c.time + period, "how": name, "both": False}
        return {"result": "open", "pts": 0.0, "at": None, "how": name, "both": False}
    return {"result": "open", "pts": 0.0, "at": None, "how": "none", "both": False}


# ------------------------------------------------------------- rebuild

def replay(eng, hourly: list, start_ms: int, end_ms: int, bars15: list | None) -> dict:
    """Re-run analyze_hour for every hour in [start_ms, end_ms) with only the candles known then."""
    out: dict[str, dict] = {}
    for k, bar in enumerate(hourly):
        if not (start_ms <= bar.time < end_ms) or k < 40:
            continue
        hs = hourly[: k + 1]
        h4 = [c for c in eng.resample(hs, H4) if c.time + H4 <= bar.time + H1]
        try:
            sig = eng.analyze_hour(h4, hs)
        except Exception:
            continue
        if int(sig.get("time") or 0) != bar.time:
            continue
        sig["_bars"] = (h4, hs)
        enrich(eng, sig, bars15=bars15, fetch=False)
        out[str(bar.time)] = _row(sig, "rebuilt")
    return out


# ---------------------------------------------------------------- report

def day_bounds(now_ms: int) -> tuple[int, int, datetime]:
    d = _berlin(now_ms)
    start = datetime(d.year, d.month, d.day, 0, 0, tzinfo=BERLIN)
    end = datetime(d.year, d.month, d.day, REPORT_HOUR, 0, tzinfo=BERLIN)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), d


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as exc:
        print("fetch skipped", type(exc).__name__)
        return []


def collect_day(eng, now_ms: int | None = None, journal: dict | None = None) -> dict:
    now_ms = _now_ms(now_ms)
    start, end, d = day_bounds(now_ms)
    journal = journal if journal is not None else load_journal()
    rows = {k: v for k, v in journal.get("signals", {}).items() if start <= int(k) < end}
    hourly = _safe(eng.fetch_hourly)
    hourly = [c for c in hourly if c.time + H1 <= now_ms]
    bars15 = _safe(eng.fetch_bars, "15m", 200)
    expected = [c.time for c in hourly if start <= c.time < end]
    missing = [t for t in expected if str(t) not in rows]
    if missing:
        rebuilt = replay(eng, hourly, start, end, bars15)
        for t in missing:
            if str(t) in rebuilt:
                rows[str(t)] = rebuilt[str(t)]
    series = [(M1, _safe(eng.fetch_bars, "1m", 300), "1m"), (M5, _safe(eng.fetch_bars, "5m", 300), "5m"), (H1, hourly, "1h")]
    trades = []
    waits = 0
    for key in sorted(rows, key=int):
        r = rows[key]
        if r["signal"] not in ("BUY", "SELL") or r.get("entry") is None or r.get("sl") is None or r.get("tp") is None:
            waits += 1
            continue
        out = resolve(r["signal"], float(r["entry"]), float(r["sl"]), float(r["tp"]), int(key) + H1, series, now_ms)
        trades.append({**r, **out})
    breakouts = []
    try:  # experimental H4 breakout alerts (mizan_breakout.py), a separate category
        for r in journal.get("breakouts", {}).values():
            if start <= int(r.get("sent_at") or 0) < end:
                out = resolve(r["side"], float(r["entry"]), float(r["sl"]), float(r["tp1"]), int(r["close_ms"]), series, now_ms)
                breakouts.append({**r, **out})
    except Exception as exc:
        print("breakout summary skipped", type(exc).__name__)
    return {"date": d, "trades": trades, "waits": waits, "hours": len(rows), "rebuilt": sum(1 for r in rows.values() if r.get("src") == "rebuilt"), "breakouts": breakouts}


def _sign(v: float) -> str:
    return "+" if v > 0 else "-" if v < 0 else ""


def _usd(pts: float) -> str:
    usd = pts * LOT * 100
    return f"{_sign(usd)}{abs(usd):,.0f}$"


def _pts(pts: float) -> str:
    return f"{_sign(pts)}{abs(pts):.2f}"


def format_report(day: dict) -> str:
    d, trades = day["date"], day["trades"]
    wins = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]
    opens = [t for t in trades if t["result"] == "open"]
    net = round(sum(t["pts"] for t in trades), 2)
    done = len(wins) + len(losses)
    rate = f"{round(100 * len(wins) / done)}%" if done else "—"
    buys = sum(1 for t in trades if t["signal"] == "BUY")
    lines = [
        "BARAZZI - XAU",
        f"📊 حصيلة اليوم — {AR_DAYS[d.weekday()]} {d.strftime('%d.%m.%Y')} (00:00–23:00 ألمانيا)",
        "",
        f"الإشارات: {len(trades)} (شراء {buys} • بيع {len(trades) - buys})",
        f"✅ ربح: {len(wins)}",
        f"❌ خسارة: {len(losses)}",
        f"⏳ لسا مفتوحة: {len(opens)}",
        f"⏸️ ساعات انتظار: {day['waits']}",
        "",
        f"الصافي: {_pts(net)}$ بالأونصة ≈ {_usd(net)} على 0.50 لوت",
        f"نسبة النجاح: {rate}",
    ]
    bo = day.get("breakouts") or []
    if bo:
        bw = sum(1 for t in bo if t.get("result") == "win")
        bl = sum(1 for t in bo if t.get("result") == "loss")
        lines.append(f"🧪 كسر القناة على 4 ساعات (تجريبي، منفصل): {len(bo)} — ✅ {bw} • ❌ {bl} • ⏳ {len(bo) - bw - bl} (الهدف 1 = 2R)")
    if trades:
        lines += ["", "التفاصيل:"]
        for t in trades:
            word = "شراء" if t["signal"] == "BUY" else "بيع"
            mark = {"win": f"✅ {_pts(t['pts'])}", "loss": f"❌ {_pts(t['pts'])}", "open": "⏳ مفتوحة"}[t["result"]]
            when = f" ({_hm(t['at'])})" if t.get("at") else ""
            tag = " 💪" if t.get("tf") == "strong" else ""
            lines.append(f"{_hm(int(t['time']) + H1)} {word} {_fmt(float(t['entry']))} • وقف {_fmt(float(t['sl']))} • هدف {_fmt(float(t['tp']))} → {mark}{when}{tag}")
        strong = [t for t in trades if t.get("tf") == "strong" and t["result"] != "open"]
        if strong:
            sw = sum(1 for t in strong if t["result"] == "win")
            lines.append(f"💪 القوية (الفريمات متفقة): {sw} ربح من {len(strong)}")
    lines.append("")
    if not trades:
        lines.append("ما في إشارات اليوم — الميزان استنى، والاستنا كمان قرار.")
    elif not done:
        lines.append("الصفقات لسا مفتوحة، منشوف نتيجتها بكرا.")
    elif net > 0:
        lines.append("يوم منيح 👌 بس ضل ملتزم بالوقف متل ما هو.")
    elif net < 0:
        lines.append("يوم صعب. الخسارة جزء من الشغل، المهم ما نكسر الخطة.")
    else:
        lines.append("يوم تعادل. لا ربح ولا خسارة.")
    how = sorted({t["how"] for t in trades if t["result"] != "open"})
    if trades:
        lines.append(f"ℹ️ النتيجة من شموع {'/'.join(how) or '5m'}: شو انلمس أول، الهدف ولا الوقف. إذا الاتنين بنفس الشمعة منحسبها خسارة.")
    if day.get("rebuilt"):
        lines.append(f"ℹ️ {day['rebuilt']} ساعات انبنت من جديد من الشموع لأن البوت انعاد تشغيله.")
    return "\n".join(lines)


def report_due(now_ms: int, journal: dict) -> str | None:
    """Berlin date string if the report should go out now, else None."""
    d = _berlin(now_ms)
    if d.weekday() >= 5:  # Saturday / Sunday: gold is closed, never report
        return None
    if d.hour != REPORT_HOUR or d.minute >= REPORT_GRACE_MIN:
        return None
    key = d.date().isoformat()
    if key in journal.get("reports", {}):
        return None
    return key


_SENT: set[str] = set()  # in-memory guard: no repeat even if the journal cannot be written


def maybe_daily_report(eng, now_ms: int | None = None, send=None, path: str | None = None) -> bool:
    """Call once per loop iteration. Sends the day report once per weekday at 23:00 Berlin. Never raises."""
    if not enabled():
        return False
    try:
        now_ms = _now_ms(now_ms)
        journal = load_journal(path)
        key = report_due(now_ms, journal)
        if key is None or key in _SENT:
            return False
        text = format_report(collect_day(eng, now_ms, journal))
        (send or eng.telegram_send)(text)
        _SENT.add(key)
        journal["reports"][key] = now_ms
        try:
            save_journal(journal, path, now_ms)
        except OSError as exc:
            print("journal not saved", type(exc).__name__)
        print("daily report sent", key)
        return True
    except Exception as exc:
        print("daily report failed", type(exc).__name__, exc)
        return False
