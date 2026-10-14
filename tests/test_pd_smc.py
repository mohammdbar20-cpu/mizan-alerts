import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mizan_pd as pd

class E:
    def __init__(s): s.posts, s.mizan = [], []
    def chat_id(s): return "1"
    def extra_chat_ids(s): return ["2"]
    def _tg_post(s, t, c, x, m): s.posts.append((t, c, x))
    def telegram_send(s, x): s.mizan.append(x)

def test_smc_and_fallback(monkeypatch):
    e = E(); monkeypatch.setenv("SMC_BOT_TOKEN", "T")
    assert pd.send(e, "عميق") == "smc" and e.posts == [("T","1","عميق"),("T","2","عميق")] and not e.mizan
    e = E(); monkeypatch.delenv("SMC_BOT_TOKEN")
    assert pd.send(e, "x") == "mizan" and e.mizan == ["x"] and not e.posts
