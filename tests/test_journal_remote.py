import base64, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mizan_trust as t
from datetime import datetime
from zoneinfo import ZoneInfo


class FakeGH:
    def __init__(self):
        self.files, self.puts = {}, 0

    def install(self, mp):
        fake = self
        def _gh_get(self):
            return fake.files.get(self.path, ("", None))
        def _gh(self, method, url, body=None):
            assert method == "PUT"
            fake.puts += 1
            fake.files[self.path] = (base64.b64decode(body["content"]).decode(), "sha%d" % fake.puts)
            return {}
        mp.setattr(t.TradeLog, "_gh_get", _gh_get)
        mp.setattr(t.TradeLog, "_gh", _gh)


class Eng:
    def __init__(self, hours):
        self.hours = hours
    def fetch_hourly(self):
        class C:  # minimal candle
            def __init__(s, tm): s.time = tm
        return [C(x) for x in self.hours]
    def fetch_bars(self, *a):
        return []


def test_restart_restores_journal_no_rebuilt_note(monkeypatch):
    monkeypatch.setenv("MIZAN_LOG_GH_TOKEN", "x")
    gh = FakeGH(); gh.install(monkeypatch)
    t._REMOTE_TRIED.clear(); t._REMOTE_PUSHED.clear()
    now = int(datetime(2026, 10, 14, 15, 5, tzinfo=ZoneInfo("Europe/Berlin")).timestamp() * 1000)
    monkeypatch.setattr(t, "_now_ms", lambda n=None: n or now)
    start, end, _ = t.day_bounds(now)
    hours = [start + i * t.H1 for i in range(3) if start + (i + 1) * t.H1 <= now]
    p = os.path.join(tempfile.mkdtemp(), "j.json")
    for h in hours:
        t.record({"time": h, "signal": "WAIT"}, p)
    assert gh.puts == len(hours)
    t.save_journal(t.load_journal(p), p)  # unchanged -> throttled, no extra PUT
    assert gh.puts == len(hours)
    # Render restart: local disk wiped, fresh process
    os.remove(p); t._REMOTE_TRIED.clear(); t._REMOTE_PUSHED.clear()
    j = t.load_journal(p)
    assert set(j["signals"]) == {str(h) for h in hours}
    monkeypatch.setattr(t, "replay", lambda *a: (_ for _ in ()).throw(AssertionError("rebuilt")))
    day = t.collect_day(Eng(hours), now, j)
    assert day["rebuilt"] == 0 and day["hours"] == len(hours)


def test_github_error_falls_back(monkeypatch):
    monkeypatch.setenv("MIZAN_LOG_GH_TOKEN", "x")
    def boom(self, *a, **k): raise OSError("down")
    monkeypatch.setattr(t.TradeLog, "_gh_get", boom)
    t._REMOTE_TRIED.clear(); t._REMOTE_PUSHED.clear()
    p = os.path.join(tempfile.mkdtemp(), "j.json")
    assert t.load_journal(p)["signals"] == {}
    ts = int(datetime.now().timestamp() * 1000)
    t.record({"time": ts, "signal": "WAIT"}, p)  # local write still works
    assert str(ts) in json.load(open(p))["signals"]
