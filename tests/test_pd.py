import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.pop("MIZAN_LOG_GH_TOKEN", None)
import mizan_engine as e, mizan_pd as pd
cfg = e._load_levels()
conf = pd.settings(cfg)
assert conf == {"enabled": True, "lookback": 48, "pivot": 3, "deep_premium": 75.0, "deep_discount": 25.0}
H = pd.H4_MS
T0 = 1792000000000 // H * H

def bars(closes):
    return [e.Candle(T0 + i * H, c, c + 1, c - 1, c, 1) for i, c in enumerate(closes)]

# rise 4000->4200 (peak idx 20), fall to 4100 (trough idx 30), rise back
cl = [4000 + 10 * i for i in range(21)] + [4200 - 10 * i for i in range(1, 11)] + [4100 + 4 * i for i in range(1, 11)]
h4 = bars(cl)
lo, hi = pd.dealing_range(e, h4, conf)
assert hi == 4201 and lo == 4099, (lo, hi)
r = pd.reading(e, h4, conf)
assert abs(r["eq"] - 4150) < 1e-9 and r["zone"] == "ديسكاونت" and 25 < r["pct"] < 50
m = pd.message(r)
assert "ديسكاونت" in m and "مش إشارة" in m and "التوازن 4150" in m and "4099" in m

# zones
def z(price):
    b = h4[:-1] + [e.Candle(h4[-1].time, price, price, price, price, 1)]
    return pd.reading(e, b, conf)
assert z(4110)["zone"].startswith("ديسكاونت عميق")
assert z(4160)["zone"] == "بريميوم"
assert z(4195)["zone"].startswith("بريميوم عميق")

# break above swing high -> range extends to new extreme
up = bars(cl + [4300])
lo2, hi2 = pd.dealing_range(e, up, conf)
assert hi2 == 4301

# fallback no pivots (monotonic)
mono = bars([4000 + i for i in range(20)])
assert pd.dealing_range(e, mono, conf) == (3999, 4020)
assert pd.dealing_range(e, bars([1, 2, 3]), conf) is None

# once per bar, state survives restart
st = {}
m, ch = pd.step(e, st, h4, conf)
assert len(m) == 1 and ch
st = json.loads(json.dumps(st))
assert pd.step(e, st, h4, conf) == ([], False)
m, ch = pd.step(e, st, bars(cl + [4120]), conf)
assert len(m) == 1 and ch
# disabled
assert pd.step(e, {}, h4, dict(conf, enabled=False)) == ([], False)

# tick: skips fetch when already sent for last closed bar; sends once otherwise
sent = []
e.telegram_send = lambda t, *a, **k: sent.append(t)
import time as _t
lc = int(_t.time() * 1000) // H * H - H
pd._STATE = {"bar": lc}
e.fetch_hourly = lambda: (_ for _ in ()).throw(AssertionError("should not fetch"))
pd.tick(e); assert sent == []
hourly = [e.Candle(lc - 48 * H + i * 3600000, c, c + 1, c - 1, c, 1)
          for i, c in enumerate([4100 + (i % 37) for i in range(48 * 4 + 4)])]
e.fetch_hourly = lambda: hourly
pd._STATE = {}
pd.tick(e); pd.tick(e)
assert len(sent) == 1 and pd._STATE["bar"] == lc, (sent, pd._STATE)
print("test_pd OK")
