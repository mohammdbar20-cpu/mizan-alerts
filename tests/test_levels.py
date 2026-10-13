"""Offline tests for user levels config, S/R zone and sweep rejection.
Run from repo root:  python3 tests/test_levels.py
"""
import json, os, sys, time
from datetime import datetime
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("MIZAN_LEVELS_JSON", None)
import mizan_engine as e

H1 = 3_600_000

def load(path):
    out = []
    for b in json.load(open(path))["bars"]:
        t = int(datetime.fromisoformat(b["openTime"].replace("Z", "+00:00")).timestamp() * 1000)
        out.append(e.Candle(t, float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"]), 0.0))
    return sorted(out, key=lambda c: c.time)

def run_at(hourly, open_iso):
    t = int(datetime.fromisoformat(open_iso).timestamp() * 1000)
    hs = [c for c in hourly if c.time <= t]
    h4 = [c for c in e.resample(hs, 4 * H1) if c.time + 4 * H1 <= t + H1]
    real = time.time
    time.time = lambda: (t + H1 + 180_000) / 1000
    try:
        return e.analyze_hour(h4, hs)
    finally:
        time.time = real

def C(o, h, l, c, t=0):
    return e.Candle(t, o, h, l, c, 0)

# 1) config
assert e.PLAN_BLUES == (4150.0, 4182.62, 4110.78), e.PLAN_BLUES
assert e.CHANNEL_ON is False
assert "قناة" not in e.channel_where(4157.79, 0)
assert "4150.00" in e.channel_where(4157.79, 0)
assert e.midline_lean(4160, 4120, 4122, 0) is None
assert e.plan_veto("SELL", 4300.0, 0, "neutral") is None  # no stale channel ceiling
assert e.plan_veto("SELL", 4151.0, 0, "neutral")  # blue line rule still works

# 2) channel path still works when configured
saved = (e.PLAN_ANCHOR_MS, e.PLAN_MARGIN, e.PLAN_UPPER, e.PLAN_MID, e.PLAN_LOWER, e.PLAN_BLUES, e.CHANNEL_ON)
e.apply_levels({"horizontals": [4150.0], "channel": {"anchor_ms": 0, "margin": 2.0, "upper": [4200, 0], "lower": [4100, 0]}})
assert e.CHANNEL_ON and e.PLAN_MID == (4150.0, 0.0)
assert e.plan_veto("SELL", 4210.0, 0, "bearish")            # above ceiling + margin
assert e.plan_veto("SELL", 4210.0, 0, "bearish", 4190.0) is None  # H4 back inside
assert e.plan_veto("BUY", 4090.0, 0, "bullish")
assert "قناة" in e.channel_where(4170.0, 0)
(e.PLAN_ANCHOR_MS, e.PLAN_MARGIN, e.PLAN_UPPER, e.PLAN_MID, e.PLAN_LOWER, e.PLAN_BLUES, e.CHANNEL_ON) = saved

# 3) env override
os.environ["MIZAN_LEVELS_JSON"] = '{"horizontals": [1, 2], "channel": null}'
assert e._load_levels()["horizontals"] == [1, 2]
os.environ.pop("MIZAN_LEVELS_JSON")

# 4) sr_guard unit
side, why = e.sr_guard("SELL", C(4165, 4166, 4156, 4157.79), [], None, 10.0, False, True)
assert side is None and "4150" in why, why
side, why = e.sr_guard("SELL", C(4165, 4166, 4156, 4157.79), [], 4148.0, 10.0, False, True)  # H4 closed below 4149
assert side == "SELL", why
side, why = e.sr_guard("SELL", C(4160, 4161, 4148, 4151), [], None, 10.0, True, True)  # sweep 4150 + buy ok
assert side == "BUY" and why is None, (side, why)
side, why = e.sr_guard("BUY", C(4170, 4176, 4170, 4175), [], None, 10.0, True, False)  # resistance 4182.62
assert side is None and "4182.62" in why, why
side, why = e.sr_guard("SELL", C(4200, 4201, 4195, 4196), [], None, 10.0, False, True)  # free space
assert side == "SELL" and why is None

# 4b) touch tolerance (FXCM low 4150.68 touched 4150 within $1) and wick-based zone
side, why = e.sr_guard("SELL", C(4170, 4171, 4150.68, 4157.79), [], None, 10.0, False, True)
assert side is None and "4150.00" in why and "لمسته" in why, why
side, why = e.sr_guard("SELL", C(4170, 4171, 4150.68, 4157.79), [], None, 10.0, True, True)
assert side == "BUY", (side, why)
side, why = e.sr_guard("BUY", C(4170, 4180.5, 4169, 4175), [], None, 10.0, False, True)  # high touched 4182.62-3
assert side == "SELL", (side, why)
# close 12 above 4150 (outside zone by close) but wick at 4156 -> blocked by zone (no touch)
side, why = e.sr_guard("SELL", C(4170, 4171, 4156, 4162), [], None, 10.0, False, True)
assert side is None and "الدعم 4150.00" in why, why
side, why = e.sr_guard("BUY", C(4160, 4176, 4159, 4168), [], None, 10.0, True, False)
assert side is None and "4182.62" in why, why

# 5) replay 2026-10-13 03:00 Berlin (H1 opened 00:00Z): swept 4155, closed 4157.79 -> must NOT sell
hourly = load(os.path.join(ROOT, "tests", "fixtures", "h1_20261013.json"))
sig = run_at(hourly, "2026-10-13T00:00:00+00:00")
print("replay 03:00:", sig["signal"], "|", sig["reason"], "|", sig["d2"])
assert abs(sig["close"] - 4157.79) < 0.01, sig["close"]
assert sig["signal"] != "SELL", sig
assert "قناة" not in sig["d2"]
# same case on FXCM-like feed (low 4150.68)
fx = [e.Candle(c.time, c.open, c.high, 4150.68, c.close, 0) if c.time == int(datetime.fromisoformat("2026-10-13T00:00:00+00:00").timestamp() * 1000) else c for c in hourly]
sig = run_at(fx, "2026-10-13T00:00:00+00:00")
print("replay FXCM low:", sig["signal"], "|", sig["reason"])
assert sig["signal"] != "SELL"
print("ALL LEVEL TESTS PASSED")
