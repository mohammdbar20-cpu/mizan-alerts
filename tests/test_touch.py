import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.pop("MIZAN_LOG_GH_TOKEN", None)
import mizan_engine as e, mizan_touch as mt
cfg = e._load_levels()
e.apply_levels(cfg)
conf = mt.settings(cfg)
assert conf == {"enabled": True, "threshold": 2.5, "rearm_distance": 8.0}
now = 1792000000000
lo = mt.line_values(e, now)["lower"]; up = mt.line_values(e, now)["upper"]
st = {}
m, ch = mt.step(e, st, lo + 1.0, now, conf)
assert len(m) == 1 and ch and e.PLAN_NAME in m[0] and "أسفل" in m[0] and "مش إشارة" in m[0]
assert f"({lo:.1f})" in m[0]
# no resend while still near / small move
assert mt.step(e, st, lo - 2.0, now + 60000, conf)[0] == []
assert mt.step(e, st, lo + 7.0, now + 120000, conf)[0] == []
# move away >= 8 re-arms, then touch again sends
assert mt.step(e, st, lo + 9.0, now + 180000, conf)[0] == []
assert len(mt.step(e, st, lo + 0.5, now + 240000, conf)[0]) == 1
# new H4 bar re-arms
nb = mt.h4_bar(now) + mt.H4_MS + 1000
lo2 = mt.line_values(e, nb)["lower"]
assert len(mt.step(e, st, lo2, nb, conf)[0]) == 1
# upper line
m = mt.step(e, {}, up - 2.4, now, conf)[0]
assert len(m) == 1 and "أعلى" in m[0]
# middle: nothing
assert mt.step(e, {}, (lo + up) / 2, now, conf)[0] == []
# state survives "restart" (json roundtrip)
import json
st2 = json.loads(json.dumps(st)); assert mt.step(e, st2, lo2 + 1, nb + 1000, conf)[0] == []
# disabled flag / no channel
assert mt.step(e, {}, lo, now, dict(conf, enabled=False))[0] == []
e.apply_levels({"channel": None}); assert mt.step(e, {}, lo, now, conf)[0] == []
e.apply_levels(cfg)
# tick: sends via telegram_send, never raises, no persistence without token
sent = []
orig = e.telegram_send; e.telegram_send = lambda t, **k: sent.append(t)
mt._STATE = None
p = mt.line_values(e, int(__import__("time").time() * 1000))["lower"]
mt.tick(e, p); mt.tick(e, p)
assert len(sent) == 1, sent
e.telegram_send = orig
print("ALL TOUCH TESTS PASSED")
