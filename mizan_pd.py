"""mizan_pd — INFO-ONLY H4 Premium/Discount alert. Not a trade: no buy/sell, not logged,
not in results, does not touch decision logic.

Method: reproduces the "Premium & Discount Zones" of LuxAlgo's open-source TradingView
indicator "Smart Money Concepts [LuxAlgo]" (Pine v5) on H4:
  * H4 bars = native 4h XAUUSD bars from Mizan's feed (e.fetch_bars("4h", fetch_limit)),
    epoch-aligned 4h buckets (same boundaries as e.resample), incomplete bar dropped.
    The hourly feed only returns ~170 bars (~42 H4), too short; the 4h feed gives ~180.
  * leg(size): at bar t, if high[t-size] > highest(high[t-size+1..t]) -> bearish leg (0);
    elif low[t-size] < lowest(low[t-size+1..t]) -> bullish leg (1); else unchanged.
    size = swing_length (LuxAlgo "Swings" length, default 50).
  * leg change to bearish -> swing high at bar t-size: trailing.top = high[t-size];
    leg change to bullish -> swing low: trailing.bottom = low[t-size].
  * updateTrailingExtremes (run each bar before structure, as in Pine):
    trailing.top = max(high, top), trailing.bottom = min(low, bottom).
  * Limited history (~180 H4 bars vs TradingView's full chart): the first detected leg counts
    as a new leg (registers its pivot); an extreme with no pivot inside our history falls back
    to the max high / min low of all fetched bars (= Pine's value when the older pivot is
    beyond that extreme, which is the case whenever price has since traded past it).
  * Zones (Pine drawPremiumDiscountZones): premium = top .. 0.95*top+0.05*bottom,
    equilibrium = 0.525*top+0.475*bottom .. 0.475*top+0.525*bottom, discount = 0.05 band at bottom.
  * pct = (close-bottom)/(top-bottom)*100 on the last closed H4 bar.
    pct >= premium_zone (95) -> بريميوم عميق; <= discount_zone (5) -> ديسكاونت عميق;
    within eq band (47.5..52.5) -> التوازن; else > 50 بريميوم / < 50 ديسكاونت.
Sent once per closed H4 bar; last-sent bar persisted on trade-log branch
(mizan_pd_state.json, env MIZAN_PD_STATE_PATH). Never raises.

Config (mizan_levels.json) "premium_discount": {"enabled", "swing_length", "min_bars",
"fetch_limit", "premium_zone", "discount_zone", "eq_band"}
"""
from __future__ import annotations

import json
import time

H4_MS = 4 * 3600 * 1000
_STATE: dict | None = None


def settings(cfg: dict) -> dict:
    t = (cfg or {}).get("premium_discount") or {}
    return {
        "enabled": bool(t.get("enabled", False)),
        "swing_length": int(t.get("swing_length", 50)),
        "min_bars": int(t.get("min_bars", 150)),
        "fetch_limit": int(t.get("fetch_limit", 500)),
        "premium_zone": float(t.get("premium_zone", 95.0)),
        "discount_zone": float(t.get("discount_zone", 5.0)),
        "eq_band": float(t.get("eq_band", 2.5)),
    }


def trailing_extremes(h4: list, size: int):
    """LuxAlgo SMC swing structure + trailing extremes. Returns (bottom, top) or None."""
    top = bottom = None
    leg = None  # unknown until first leg (Pine has long history; we only have ~180 bars)
    for t, c in enumerate(h4):
        if top is not None:
            top = max(c.high, top)
        if bottom is not None:
            bottom = min(c.low, bottom)
        if t < size:
            continue
        p = h4[t - size]
        nxt = h4[t - size + 1:t + 1]
        new = leg
        if p.high > max(x.high for x in nxt):
            new = 0
        elif p.low < min(x.low for x in nxt):
            new = 1
        if new is not None and new != leg:
            if new == 0:
                top = p.high
            else:
                bottom = p.low
        leg = new
    # Extreme whose swing pivot lies before our history: Pine's value = min/max of that older
    # pivot and every bar since, approximated by the extreme of all available bars.
    if top is None:
        top = max(c.high for c in h4)
    if bottom is None:
        bottom = min(c.low for c in h4)
    if top - bottom <= 0:
        return None
    return bottom, top


def dealing_range(e, h4: list, conf: dict):
    """h4 = closed H4 candles. Returns (low, high) or None."""
    if len(h4) < max(conf["min_bars"], conf["swing_length"] + 2):
        return None
    return trailing_extremes(h4, conf["swing_length"])


def reading(e, h4: list, conf: dict) -> dict | None:
    r = dealing_range(e, h4, conf)
    if not r:
        return None
    lo, hi = r
    price = h4[-1].close
    pct = (price - lo) / (hi - lo) * 100.0
    if pct >= conf["premium_zone"]:
        zone = "بريميوم عميق"
    elif pct <= conf["discount_zone"]:
        zone = "ديسكاونت عميق"
    elif abs(pct - 50) <= conf["eq_band"]:
        zone = "التوازن"
    elif pct > 50:
        zone = "بريميوم"
    else:
        zone = "ديسكاونت"
    return {"bar": h4[-1].time, "price": price, "low": lo, "high": hi,
            "eq": (lo + hi) / 2, "pct": pct, "zone": zone}


def message(r: dict) -> str:
    return (f"🕐 معلومة 4 ساعات (حسب مؤشر SMC): السعر {r['price']:.0f} بمنطقة {r['zone']} ({r['pct']:.0f}%) — "
            f"المدى من {r['low']:.0f} لـ {r['high']:.0f}، التوازن {r['eq']:.0f}. "
            "هاد تنبيه للمتابعة مش إشارة شراء/بيع.")


def step(e, state: dict, h4: list, conf: dict) -> tuple[list[str], bool]:
    """Pure decision on closed H4 bars. Mutates state."""
    if not conf["enabled"] or not h4:
        return [], False
    bar = h4[-1].time
    if state.get("bar") is not None and state["bar"] >= bar:
        return [], False
    r = reading(e, h4, conf)
    if not r:
        return [], False
    state.update({"bar": bar, "pct": round(r["pct"], 1), "at": int(time.time() * 1000)})
    return [message(r)], True


def _store(e):
    try:
        trust = e._trust()
        if not trust or not trust.TradeLog._tok():
            return None
        import os
        log = trust.TradeLog()
        log.path = os.environ.get("MIZAN_PD_STATE_PATH", "mizan_pd_state.json").strip()
        return log
    except Exception:
        return None


def load_state(e) -> dict:
    log = _store(e)
    if not log:
        return {}
    try:
        txt = log._gh_get()[0].strip()
        return json.loads(txt) if txt else {}
    except Exception as exc:
        print("pd state read failed", type(exc).__name__)
        return {}


def save_state(e, state: dict) -> None:
    log = _store(e)
    if not log:
        return
    import base64
    try:
        _, sha = log._gh_get()
        body = {"message": "mizan pd state", "branch": log.branch,
                "content": base64.b64encode((json.dumps(state, ensure_ascii=False) + "\n").encode()).decode()}
        if sha:
            body["sha"] = sha
        log._gh("PUT", f"https://api.github.com/repos/{log.repo}/contents/{log.path}", body)
    except Exception as exc:
        print("pd state write failed", type(exc).__name__)


def send(e, text: str) -> str:
    """Send via the SMC bot (SMC_BOT_TOKEN) to Mizan's chat ids; fall back to Mizan's bot if unset."""
    import os
    tok = os.environ.get("SMC_BOT_TOKEN", "").strip()
    if not tok:
        e.telegram_send(text)
        return "mizan"
    chats = [e.chat_id()] + [c for c in e.extra_chat_ids() if c]
    ok = 0
    for c in [c for c in chats if c]:
        try:
            e._tg_post(tok, c, text, None)
            ok += 1
        except Exception as exc:
            print("pd smc send failed", type(exc).__name__)
    if not ok:
        raise RuntimeError("SMC bot send failed")
    return "smc"


def tick(e) -> None:
    """Called from Mizan's loop (every ~30s). Fetches bars only when a new H4 bar has closed."""
    global _STATE
    try:
        conf = settings(e._load_levels())
        if not conf["enabled"]:
            return
        if _STATE is None:
            _STATE = load_state(e)
        last_closed = int(time.time() * 1000) // H4_MS * H4_MS - H4_MS
        if _STATE.get("bar") is not None and _STATE["bar"] >= last_closed:
            return
        h4 = e.drop_incomplete(e.fetch_bars("4h", conf["fetch_limit"]), H4_MS)
        if not h4 or h4[-1].time < last_closed:
            return  # data for the just-closed bar not in yet; retry next tick
        msgs, changed = step(e, _STATE, h4, conf)
        for m in msgs:
            try:
                print("pd info sent via", send(e, m))
            except Exception as exc:
                print("pd send failed", type(exc).__name__)
        if changed:
            save_state(e, _STATE)
    except Exception as exc:
        print("pd skipped", type(exc).__name__)
