import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.pop("MIZAN_LOG_GH_TOKEN", None)
import mizan_engine as e, mizan_pd as pd
cfg = e._load_levels()
conf = pd.settings(cfg)
assert conf == {"enabled": True, "swing_length": 50, "min_bars": 150, "fetch_limit": 500,
                "premium_zone": 95.0, "discount_zone": 5.0, "eq_band": 2.5}
H = pd.H4_MS
T0 = 1792000000000 // H * H

def bars(closes):
    return [e.Candle(T0 + i * H, c, c + 1, c - 1, c, 1) for i, c in enumerate(closes)]

# rise 4000->4600 (peak idx 60), fall to 4300 (trough idx 120), rise to 4420 (idx 160)
cl = [4000 + 10 * i for i in range(61)] + [4600 - 5 * i for i in range(1, 61)] + [4300 + 3 * i for i in range(1, 41)]
h4 = bars(cl)
assert len(h4) == 161
# swing high at 60 confirmed at bar 110; swing low at 120 not yet confirmed (needs bar 170),
# but trailing bottom: first swing low = bar 0? low[0] is never pivot (leg starts 0, no change) ->
# so bottom stays None until a bullish leg begins -> no range yet
# first leg (bearish at bar 110 for the idx-60 high) registers top; bottom has no pivot -> min low
assert pd.trailing_extremes(h4, 50) == (3999, 4601)
# make an early swing low: dip at idx 0..; prepend 60 falling bars
cl2 = [4300 - 5 * i for i in range(60)] + cl  # low at idx 59 (4005 vs 4000 at 60? ensure lowest)
cl2 = [4300 - 5 * i for i in range(60)] + [4000 - 50] + cl
h = bars(cl2)
lo, hi = pd.trailing_extremes(h, 50)
assert hi == 4601 and lo == 3949, (lo, hi)
r = pd.reading(e, h, conf)
assert r["zone"] == "بريميوم" and 50 < r["pct"] < 95, r
m = pd.message(r)
assert "(حسب مؤشر SMC)" in m and "بريميوم" in m and "مش إشارة" in m and "3949" in m and "4601" in m

def z(price):
    b = h[:-1] + [e.Candle(h[-1].time, price, price, price, price, 1)]
    return pd.reading(e, b, conf)
span = 4601 - 3949
assert z(3949 + 0.96 * span)["zone"] == "بريميوم عميق"
assert z(3949 + 0.94 * span)["zone"] == "بريميوم"
assert z(3949 + 0.51 * span)["zone"] == "التوازن"
assert z(3949 + 0.49 * span)["zone"] == "التوازن"
assert z(3949 + 0.40 * span)["zone"] == "ديسكاونت"
assert z(3949 + 0.04 * span)["zone"] == "ديسكاونت عميق"
assert "عميق" in pd.message(z(3949 + 0.04 * span))
# new high after last swing top extends trailing top
up = h + bars([0] * 0) + [e.Candle(h[-1].time + H, 4700, 4800, 4690, 4790, 1)]
assert pd.trailing_extremes(up, 50) == (3949, 4800)
# too little history
assert pd.dealing_range(e, h[-149:], conf) is None
# configurable length
assert pd.trailing_extremes(h, 5) is not None

# once per bar, state survives restart
st = {}
m, ch = pd.step(e, st, h, conf)
assert len(m) == 1 and ch
st = json.loads(json.dumps(st))
assert pd.step(e, st, h, conf) == ([], False)
assert pd.step(e, {}, h, dict(conf, enabled=False)) == ([], False)

# tick: native 4h fetch, skip when already sent
sent = []
e.telegram_send = lambda t, *a, **k: sent.append(t)
import time as _t
lc = int(_t.time() * 1000) // H * H - H
pd._STATE = {"bar": lc}
e.fetch_bars = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not fetch"))
pd.tick(e); assert sent == []
feed = [e.Candle(lc - (len(cl2) - 1) * H + i * H, c, c + 1, c - 1, c, 1) for i, c in enumerate(cl2)]
calls = []
e.fetch_bars = lambda iv, lim: calls.append((iv, lim)) or list(feed)
pd._STATE = {}
pd.tick(e); pd.tick(e)
assert calls == [("4h", 500)] and len(sent) == 1 and pd._STATE["bar"] == lc, (calls, sent, pd._STATE)
print("test_pd OK")
