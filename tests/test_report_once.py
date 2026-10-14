import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mizan_trust as t
from datetime import datetime
from zoneinfo import ZoneInfo

def _ms():
    return int(datetime(2026, 10, 14, 23, 10, tzinfo=ZoneInfo("Europe/Berlin")).timestamp() * 1000)

def test_skips_when_remote_says_sent(monkeypatch):
    monkeypatch.setattr(t, "enabled", lambda: True)
    monkeypatch.setattr(t, "remote_sent_date", lambda: "2026-10-14")
    t._SENT.clear()
    sent = []
    p = os.path.join(tempfile.mkdtemp(), "j.json")
    assert t.maybe_daily_report(None, _ms(), send=sent.append, path=p) is False
    assert sent == []

def test_sends_once_and_persists(monkeypatch):
    monkeypatch.setattr(t, "enabled", lambda: True)
    store = {"d": ""}
    monkeypatch.setattr(t, "remote_sent_date", lambda: store["d"])
    monkeypatch.setattr(t, "save_remote_sent_date", lambda k: store.__setitem__("d", k))
    monkeypatch.setattr(t, "collect_day", lambda e, n, j: {"x": 1})
    monkeypatch.setattr(t, "format_report", lambda d: "r")
    monkeypatch.setattr(t, "day_record", lambda d: (_ for _ in ()).throw(ValueError()))
    sent = []
    for _ in range(2):  # second run = fresh process after redeploy
        t._SENT.clear()
        p = os.path.join(tempfile.mkdtemp(), "j.json")
        t.maybe_daily_report(None, _ms(), send=sent.append, path=p)
    assert sent == ["r"] and store["d"] == "2026-10-14"
