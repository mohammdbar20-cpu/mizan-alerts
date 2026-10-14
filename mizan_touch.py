"""mizan_touch — INFO-ONLY alert when live price touches the lower/upper line of the
configured channel (mizan_levels.json). Not a trade: no buy/sell, not logged, not in results.

Config (mizan_levels.json, top level):
  "touch_alert": {"enabled": true, "threshold": 2.5, "rearm_distance": 8.0}
Once per touch per line; re-arms when price moves >= rearm_distance away from that line
or a new H4 bar starts. State persisted on trade-log branch (mizan_touch_state.json,
env MIZAN_TOUCH_STATE_PATH) so restarts don't resend. Never raises.
"""
from __future__ import annotations

import json
import time

H4_MS = 4 * 3600 * 1000
LINES = (("lower", "أسفل"), ("upper", "أعلى"))
_STATE: dict | None = None


def settings(cfg: dict) -> dict:
    t = (cfg or {}).get("touch_alert") or {}
    return {
        "enabled": bool(t.get("enabled", False)),
        "threshold": float(t.get("threshold", 2.5)),
        "rearm_distance": float(t.get("rearm_distance", 8.0)),
    }


def h4_bar(now_ms: int) -> int:
    return now_ms // H4_MS * H4_MS


def line_values(e, now_ms: int) -> dict:
    return {"lower": e._plan_at(e.PLAN_LOWER, now_ms), "upper": e._plan_at(e.PLAN_UPPER, now_ms)}


def message(name: str, side_ar: str, line: float, price: float) -> str:
    return (f"⚠️ معلومة: السعر وصل ل{side_ar} {name} ({line:.1f}) — السعر الحالي {price:.2f}. "
            "هاد تنبيه للمتابعة مش إشارة شراء/بيع.")


def step(e, state: dict, price: float, now_ms: int, conf: dict) -> tuple[list[str], bool]:
    """Pure decision. Returns (messages, state_changed). Mutates state."""
    out: list[str] = []
    changed = False
    if not conf["enabled"] or not e.CHANNEL_ON or price is None:
        return out, False
    bar = h4_bar(now_ms)
    vals = line_values(e, now_ms)
    for key, side_ar in LINES:
        line = vals[key]
        dist = abs(price - line)
        st = state.get(key) or {"armed": True, "bar": 0}
        if not st.get("armed", True) and (dist >= conf["rearm_distance"] or st.get("bar") != bar):
            st = {"armed": True, "bar": st.get("bar", 0)}
            changed = True
        if st.get("armed", True) and dist <= conf["threshold"]:
            out.append(message(e.PLAN_NAME, side_ar, line, price))
            st = {"armed": False, "bar": bar, "line": round(line, 2), "price": round(price, 2), "at": now_ms}
            changed = True
        state[key] = st
    return out, changed


def _store(e):
    try:
        trust = e._trust()
        if not trust or not trust.TradeLog._tok():
            return None
        import os
        log = trust.TradeLog()
        log.path = os.environ.get("MIZAN_TOUCH_STATE_PATH", "mizan_touch_state.json").strip()
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
        print("touch state read failed", type(exc).__name__)
        return {}


def save_state(e, state: dict) -> None:
    log = _store(e)
    if not log:
        return
    import base64
    try:
        _, sha = log._gh_get()
        body = {"message": "mizan touch state", "branch": log.branch,
                "content": base64.b64encode((json.dumps(state, ensure_ascii=False) + "\n").encode()).decode()}
        if sha:
            body["sha"] = sha
        log._gh("PUT", f"https://api.github.com/repos/{log.repo}/contents/{log.path}", body)
    except Exception as exc:
        print("touch state write failed", type(exc).__name__)


def tick(e, price: float | None = None) -> None:
    """Called from Mizan's loop. Never raises."""
    global _STATE
    try:
        conf = settings(e._load_levels())
        if not conf["enabled"] or not e.CHANNEL_ON:
            return
        if _STATE is None:
            _STATE = load_state(e)
        if price is None:
            price = e.live_spot()
        if price is None:
            return
        msgs, changed = step(e, _STATE, float(price), int(time.time() * 1000), conf)
        for m in msgs:
            try:
                e.telegram_send(m)
                print("touch info sent")
            except Exception as exc:
                print("touch send failed", type(exc).__name__)
        if changed:
            save_state(e, _STATE)
    except Exception as exc:
        print("touch skipped", type(exc).__name__)
