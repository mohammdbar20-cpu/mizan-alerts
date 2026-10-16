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
DAYS_KEEP_LOG = 40  # local daily records, enough for one month
LOG_LOT = 0.01  # durable log uses $ at 0.01 lot
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


def _journal_store():
    """TradeLog pointed at mizan_journal.json on trade-log, or None (no token)."""
    if not TradeLog._tok():
        return None
    log = TradeLog()
    log.path = os.environ.get("MIZAN_JOURNAL_GH_PATH", "mizan_journal.json").strip()
    return log


_REMOTE_TRIED: set[str] = set()  # restore from trade-log at most once per journal path per process
_REMOTE_PUSHED: dict[str, str] = {}  # path -> fingerprint of the last journal content pushed


def _fingerprint(data: dict) -> str:
    keys = ("signals", "reports", "days", "breakouts")
    return json.dumps({k: data.get(k) for k in keys}, sort_keys=True, ensure_ascii=False)


def _remote_journal() -> dict | None:
    log = _journal_store()
    if not log:
        return None
    try:
        txt = log._gh_get()[0]
        data = json.loads(txt) if txt.strip() else None
        return data if isinstance(data, dict) else None
    except Exception as exc:
        print("journal GitHub read failed", type(exc).__name__)
        return None


def _merge(local: dict, remote: dict) -> dict:
    for k in ("signals", "reports", "days", "breakouts"):
        r = remote.get(k)
        if isinstance(r, dict):
            local[k] = {**r, **local.get(k, {})}
    return local


def _has_today(data: dict) -> bool:
    start, end, _ = day_bounds(_now_ms())
    return any(start <= int(k) < end for k in data.get("signals", {}))


def _push_remote(data: dict, path: str) -> None:
    """Mirror the journal to trade-log, only when its content changed. Never raises."""
    fp = _fingerprint(data)
    if _REMOTE_PUSHED.get(path) == fp:
        return
    log = _journal_store()
    if not log:
        return
    import base64
    for _ in range(2):  # 409 sha race -> retry once
        try:
            _, sha = log._gh_get()
            body = {"message": f"mizan journal {_berlin(_now_ms()).date().isoformat()}", "branch": log.branch,
                    "content": base64.b64encode(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()).decode()}
            if sha:
                body["sha"] = sha
            log._gh("PUT", f"https://api.github.com/repos/{log.repo}/contents/{log.path}", body)
            _REMOTE_PUSHED[path] = fp
            return
        except Exception as exc:
            print("journal GitHub write failed", type(exc).__name__)


def _load_local(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("signals", {})
            data.setdefault("reports", {})
            return data
    except (OSError, ValueError):
        pass
    return None


def load_journal(path: str | None = None) -> dict:
    path = path or journal_path()
    data = _load_local(path)
    if path not in _REMOTE_TRIED and (data is None or not _has_today(data)):
        _REMOTE_TRIED.add(path)  # local missing / other day (fresh Render instance): restore from trade-log
        remote = _remote_journal()
        if remote:
            data = _merge(data or {"version": 1, "signals": {}, "reports": {}}, remote)
            data.setdefault("signals", {})
            data.setdefault("reports", {})
            _REMOTE_PUSHED[path] = _fingerprint(data)
    if data is not None:
        return data
    return {"version": 1, "signals": {}, "reports": {}}


def save_journal(data: dict, path: str | None = None, now_ms: int | None = None) -> None:
    path = path or journal_path()
    cutoff = _now_ms(now_ms) - KEEP_DAYS * 86_400_000
    data["signals"] = {k: v for k, v in data.get("signals", {}).items() if int(k) >= cutoff}
    keep_dates = {(_berlin(_now_ms(now_ms)) - timedelta(days=d)).date().isoformat() for d in range(KEEP_DAYS)}
    data["reports"] = {k: v for k, v in data.get("reports", {}).items() if k in keep_dates}
    month_dates = {(_berlin(_now_ms(now_ms)) - timedelta(days=d)).date().isoformat() for d in range(DAYS_KEEP_LOG)}
    if "days" in data:  # daily result records (fallback for the monthly summary)
        data["days"] = {k: v for k, v in data["days"].items() if k in month_dates}
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
    _push_remote(data, path)


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
    ] if getattr(eng, "CHANNEL_ON", True) else []
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
    try:  # line omitted while the breakout alert is switched off
        import mizan_breakout
        bo_on = mizan_breakout.enabled()
    except Exception:
        bo_on = False
    if bo and bo_on:
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


# ------------------------------------------------------- durable daily log

def day_record(day: dict) -> dict:
    """One JSON line per day: every signal + totals (R and $ at 0.01 lot)."""
    trades = day["trades"]
    sigs = []
    for t in trades:
        risk = abs(float(t["entry"]) - float(t["sl"])) or None
        r = round(t["pts"] / risk, 2) if risk else 0.0
        sigs.append({"time": _hm(int(t["time"]) + H1), "dir": t["signal"], "entry": float(t["entry"]),
                     "sl": float(t["sl"]), "tp": float(t["tp"]), "result": t["result"], "r": r,
                     "usd": round(t["pts"] * LOG_LOT * 100, 2), "tf": t.get("tf")})
    wins = sum(1 for t in trades if t["result"] == "win")
    losses = sum(1 for t in trades if t["result"] == "loss")
    return {"date": day["date"].date().isoformat(), "signals": sigs, "trades": len(trades), "wins": wins,
            "losses": losses, "open": len(trades) - wins - losses, "waits": day.get("waits", 0),
            "net_pts": round(sum(t["pts"] for t in trades), 2), "total_r": round(sum(x["r"] for x in sigs), 2),
            "usd_001": round(sum(x["usd"] for x in sigs), 2)}


class TradeLog:
    """JSON lines on GitHub branch 'trade-log' (contents API) when MIZAN_LOG_GH_TOKEN is set.
    That branch is not main, so Render (autodeploy from main) never redeploys because of it."""

    def __init__(self) -> None:
        self.repo = os.environ.get("MIZAN_LOG_REPO", "mohammdbar20-cpu/mizan-alerts").strip()
        self.branch = os.environ.get("MIZAN_LOG_BRANCH", "trade-log").strip()
        self.path = os.environ.get("MIZAN_LOG_PATH", "mizan_trades.jsonl").strip()

    @staticmethod
    def _tok() -> str:
        return os.environ.get("MIZAN_LOG_GH_TOKEN", "").strip()

    def _gh(self, method: str, url: str, body: dict | None = None) -> dict:
        import urllib.request
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": f"Bearer {self._tok()}", "User-Agent": "mizan-bot",
                                              "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")

    def _gh_get(self) -> tuple[str, str | None]:
        import base64, urllib.error
        api = f"https://api.github.com/repos/{self.repo}"
        try:
            j = self._gh("GET", f"{api}/contents/{self.path}?ref={self.branch}")
            return base64.b64decode(j.get("content", "")).decode(), j.get("sha")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        try:
            self._gh("GET", f"{api}/branches/{self.branch}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            # branch missing -> orphan commit (no code, no parent) so it can never be deployed
            tree = self._gh("POST", f"{api}/git/trees", {"tree": [{"path": "README.md", "mode": "100644", "type": "blob",
                                                                   "content": "Mizan daily results log (data only).\n"}]})
            commit = self._gh("POST", f"{api}/git/commits", {"message": "trade-log: start", "tree": tree["sha"], "parents": []})
            self._gh("POST", f"{api}/git/refs", {"ref": f"refs/heads/{self.branch}", "sha": commit["sha"]})
        return "", None

    def read(self) -> list[dict] | None:
        """All records, or None when GitHub is not available."""
        if not self._tok():
            return None
        try:
            txt, _ = self._gh_get()
            return [json.loads(x) for x in txt.splitlines() if x.strip()]
        except Exception as e:
            print("trade log GitHub read failed", type(e).__name__)
            return None

    def append(self, rec: dict) -> bool:
        """One record per date (reads the file first). Never raises."""
        if not self._tok():
            print("WARNING: MIZAN_LOG_GH_TOKEN not set, GitHub trade log skipped")
            return False
        import base64
        for _ in range(2):  # 409 sha race -> retry once
            try:
                txt, sha = self._gh_get()
                if any(json.loads(x).get("date") == rec["date"] for x in txt.splitlines() if x.strip()):
                    return True
                txt += json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
                body = {"message": f"mizan log {rec['date']}", "branch": self.branch,
                        "content": base64.b64encode(txt.encode()).decode()}
                if sha:
                    body["sha"] = sha
                self._gh("PUT", f"https://api.github.com/repos/{self.repo}/contents/{self.path}", body)
                return True
            except Exception as e:
                print("trade log GitHub write failed", type(e).__name__)
        return False


def last_weekday_of_month(d: datetime) -> bool:
    x = d.date() + timedelta(days=1)
    while x.month == d.month:
        if x.weekday() < 5:
            return False
        x += timedelta(days=1)
    return d.weekday() < 5


def monthly_summary(rec: dict, remote: list[dict] | None, local_days: dict) -> str:
    month = rec["date"][:7]
    by_date = {r["date"]: r for r in (remote if remote is not None else local_days.values()) if str(r.get("date", "")).startswith(month)}
    by_date[rec["date"]] = rec
    rows = list(by_date.values())
    t = sum(int(r.get("trades", 0)) for r in rows)
    w = sum(int(r.get("wins", 0)) for r in rows)
    lo = sum(int(r.get("losses", 0)) for r in rows)
    tr = round(sum(float(r.get("total_r", 0)) for r in rows), 2)
    usd = round(sum(float(r.get("usd_001", 0)) for r in rows), 2)
    src = "" if remote is not None else " (من السجل المحلي)"
    return (f"🗓️ حصيلة الشهر {month}{src}: {t} صفقة • ✅ {w} • ❌ {lo}\n"
            f"المجموع: {tr:+.2f}R ≈ {usd:+.2f}$ على 0.01 لوت ({len(rows)} يوم)")


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


def _sent_store():
    """TradeLog pointed at mizan_report_sent.txt (last Berlin date the report went out), or None."""
    if not TradeLog._tok():
        return None
    log = TradeLog()
    log.path = os.environ.get("MIZAN_REPORT_SENT_PATH", "mizan_report_sent.txt").strip()
    return log


def remote_sent_date() -> str:
    """Last report date persisted on trade-log ('' if unknown). Survives Render restarts/redeploys."""
    log = _sent_store()
    if not log:
        return ""
    try:
        return log._gh_get()[0].strip()
    except Exception as exc:
        print("report sent-date read failed", type(exc).__name__)
        return ""


def save_remote_sent_date(key: str) -> None:
    log = _sent_store()
    if not log:
        return
    import base64
    for _ in range(2):
        try:
            _, sha = log._gh_get()
            body = {"message": f"mizan report sent {key}", "branch": log.branch,
                    "content": base64.b64encode((key + "\n").encode()).decode()}
            if sha:
                body["sha"] = sha
            log._gh("PUT", f"https://api.github.com/repos/{log.repo}/contents/{log.path}", body)
            return
        except Exception as exc:
            print("report sent-date write failed", type(exc).__name__)


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
        if remote_sent_date() == key:  # already sent before a restart/redeploy
            _SENT.add(key)
            journal.setdefault("reports", {})[key] = now_ms
            return False
        day = collect_day(eng, now_ms, journal)
        text = format_report(day)
        try:  # durable log + monthly summary must never block the report
            rec = day_record(day)
            journal.setdefault("days", {})[rec["date"]] = rec
            log = TradeLog()
            log.append(rec)
            if last_weekday_of_month(day["date"]):
                text += "\n\n" + monthly_summary(rec, log.read(), journal["days"])
        except Exception as exc:
            print("trade log skipped", type(exc).__name__)
        (send or eng.telegram_send)(text)
        _SENT.add(key)
        save_remote_sent_date(key)
        journal.setdefault("reports", {})[key] = now_ms
        try:
            save_journal(journal, path, now_ms)
        except OSError as exc:
            print("journal not saved", type(exc).__name__)
        print("daily report sent", key)
        return True
    except Exception as exc:
        print("daily report failed", type(exc).__name__, exc)
        return False


# --------------------------------------------------------------- selftest

def selftest() -> None:
    os.environ.pop("MIZAN_LOG_GH_TOKEN", None)
    d = datetime(2026, 10, 30, 23, 0, tzinfo=BERLIN)
    t0 = int(datetime(2026, 10, 30, 9, 0, tzinfo=BERLIN).timestamp() * 1000)
    day = {"date": d, "waits": 3, "trades": [
        {"time": t0, "signal": "BUY", "entry": 4000.0, "sl": 3990.0, "tp": 4020.0, "result": "win", "pts": 20.0},
        {"time": t0 + H1, "signal": "SELL", "entry": 4010.0, "sl": 4015.0, "tp": 4000.0, "result": "loss", "pts": -5.0}]}
    rec = day_record(day)
    assert rec["date"] == "2026-10-30" and rec["wins"] == 1 and rec["losses"] == 1
    assert rec["total_r"] == 1.0 and rec["usd_001"] == 15.0 and rec["signals"][0]["time"] == "10:00"
    assert TradeLog().append(rec) is False and TradeLog().read() is None  # no token -> skip
    assert last_weekday_of_month(d) and not last_weekday_of_month(d - timedelta(days=1))
    assert last_weekday_of_month(datetime(2026, 1, 30, tzinfo=BERLIN))  # Fri; 31st is Saturday
    old = {"date": "2026-10-29", "trades": 2, "wins": 2, "losses": 0, "total_r": 4.0, "usd_001": 40.0}
    m = monthly_summary(rec, None, {"2026-10-29": old, "2026-09-30": {**old, "date": "2026-09-30"}})
    assert "4 صفقة" in m and "+5.00R" in m and "+55.00$" in m and "المحلي" in m, m
    m2 = monthly_summary(rec, [old, rec], {})
    assert "المحلي" not in m2 and "+5.00R" in m2
    print("mizan_trust selftest ok")


if __name__ == "__main__":
    selftest()
