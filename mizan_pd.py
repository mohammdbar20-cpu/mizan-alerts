"""mizan_pd — INFO-ONLY H4 Premium/Discount alert (ICT). Not a trade: no buy/sell, not logged,
not in results, does not touch decision logic.

Method (documented):
  * H4 bars = Mizan's own H4: hourly bars resampled on epoch-aligned 4h buckets
    (e.resample + e.drop_incomplete), i.e. the same boundaries as the engine/touch alert.
  * Window = last `lookback` CLOSED H4 bars (default 48 ≈ 8 trading days).
  * Swings = fractal pivots via e.confirmed_swings(left=pivot, right=pivot), pivot default 3
    (a bar whose high/low is strictly beyond the 3 bars each side; confirmed 3 bars later).
  * Dealing range high = most recent confirmed swing high, extended to the highest high made
    after it (so a break above re-anchors the range to the new extreme); low likewise.
    Fallback when no pivot exists in the window: window max high / min low.
  * Equilibrium = 50%. pct = (close-low)/(high-low)*100 using the closed bar's close.
    pct >= deep_premium -> deep premium (OTE), > 50 premium, < 50 discount,
    <= deep_discount -> deep discount.
Sent once per closed H4 bar; last-sent bar persisted on trade-log branch
(mizan_pd_state.json, env MIZAN_PD_STATE_PATH). Night sending allowed. Never raises.

Config (mizan_levels.json): "premium_discount": {"enabled", "lookback", "pivot",
"deep_premium", "deep_discount"}
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
        "lookback": int(t.get("lookback", 48)),
        "pivot": int(t.get("pivot", 3)),
        "deep_premium": float(t.get("deep_premium", 75.0)),
        "deep_discount": float(t.get("deep_discount", 25.0)),
    }


def dealing_range(e, h4: list, conf: dict):
    """h4 = closed H4 candles. Returns (low, high) or None."""
    win = h4[-conf["lookback"]:]
    if len(win) < 2 * conf["pivot"] + 2:
        return None
    highs, lows = e.confirmed_swings(win, conf["pivot"], conf["pivot"])
    if highs:
        s = highs[-1]
        hi = max(c.high for c in win[s.index:])
    else:
        hi = max(c.high for c in win)
    if lows:
        s = lows[-1]
        lo = min(c.low for c in win[s.index:])
    else:
        lo = min(c.low for c in win)
    if hi - lo <= 0:
        return None
    return lo, hi


def reading(e, h4: list, conf: dict) -> dict | None:
    r = dealing_range(e, h4, conf)
    if not r:
        return None
    lo, hi = r
    price = h4[-1].close
    pct = (price - lo) / (hi - lo) * 100.0
    if pct >= conf["deep_premium"]:
        zone = "بريميوم عميق (OTE)"
    elif pct > 50:
        zone = "بريميوم"
    elif pct <= conf["deep_discount"]:
        zone = "ديسكاونت عميق (OTE)"
    elif pct < 50:
        zone = "ديسكاونت"
    else:
        zone = "التوازن"
    return {"bar": h4[-1].time, "price": price, "low": lo, "high": hi,
            "eq": (lo + hi) / 2, "pct": pct, "zone": zone}


def message(r: dict) -> str:
    return (f"🕐 معلومة 4 ساعات: السعر {r['price']:.0f} بمنطقة {r['zone']} ({r['pct']:.0f}%) — "
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
        h4 = e.drop_incomplete(e.resample(e.fetch_hourly(), H4_MS), H4_MS)
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
