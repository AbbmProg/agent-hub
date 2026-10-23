"""Хаб целиком (HTTP API + отправка в Telegram + приём сообщений людей) с поддельным Telegram."""
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from hub.server import create_app
from hub.telegram import split, wait_until

ROOT = Path(__file__).resolve().parent.parent
CHAT = -100777


class FakeTelegram:
    def __init__(self):
        self.sent, self.updates, self.next_id = [], [], 1000

    def __call__(self, request: httpx.Request) -> httpx.Response:
        token, method = request.url.path.split("/")[1][3:], request.url.path.split("/")[2]
        body = json.loads(request.content or b"{}")
        if method == "sendMessage":
            self.next_id += 1
            self.sent.append({"token": token, **body, "message_id": self.next_id})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})
        if method == "getUpdates":
            ups = [u for u in self.updates if u["update_id"] >= body.get("offset", 0)]
            if not ups:  # длинный опрос — не крутимся вхолостую
                import time
                time.sleep(0.05)
            return httpx.Response(200, json={"ok": True, "result": ups})
        return httpx.Response(404, json={"ok": False, "description": "unknown"})


@pytest.fixture
def hub(tmp_path):
    cfg = {"db": str(tmp_path / "hub.db"), "chat_id": CHAT, "admin_token": "ADM",
           "agents": {"claude1": {"title": "Claude #1", "api_token": "T1", "bot_token": "B1"},
                      "gpt": {"title": "GPT", "api_token": "T2", "bot_token": "B2"},
                      "claude2": {"title": "Claude #2", "api_token": "T3"}}}
    tg = FakeTelegram()
    with TestClient(create_app(cfg, transport=httpx.MockTransport(tg))) as c:
        yield c, tg


def h(token):
    return {"Authorization": f"Bearer {token}"}


def test_auth(hub):
    c, _ = hub
    assert c.get("/me").status_code == 401
    assert c.get("/me", headers=h("bad")).status_code == 401
    assert c.get("/me", headers=h("T1")).json() == {"agent": "claude1", "title": "Claude #1"}


def test_post_inbox_and_telegram(hub):
    c, tg = hub
    r = c.post("/messages", headers=h("T1"), json={"kind": "report", "text": "A/B: +1.18", "refs": ["run ab1"]})
    assert r.status_code == 201
    mid = r.json()["id"]
    assert c.post("/messages", headers=h("T1"), json={"kind": "bogus", "text": "x"}).status_code == 422
    assert c.post("/messages", headers=h("T1"), json={"kind": "system", "text": "x"}).status_code == 422
    # своё сообщение в непрочитанном не показывается, чужое — да, и только один раз
    assert c.get("/inbox", headers=h("T1")).json()["messages"] == []
    got = c.get("/inbox", headers=h("T2")).json()["messages"]
    assert [m["id"] for m in got] == [mid] and got[0]["refs"] == ["run ab1"]
    assert c.get("/inbox", headers=h("T2")).json()["messages"] == []
    # в Telegram — от бота автора, с заголовком и ссылкой
    assert wait_until(lambda: len(tg.sent) == 1)
    s = tg.sent[0]
    assert s["token"] == "B1" and s["chat_id"] == CHAT
    assert "Claude #1 · отчёт · #1" in s["text"] and "🔗 run ab1" in s["text"]
    # ответ — реплаем на исходное сообщение в Telegram
    r = c.post("/messages", headers=h("T2"), json={"kind": "answer", "text": "проверю", "reply_to": mid})
    assert wait_until(lambda: len(tg.sent) == 2)
    assert tg.sent[1]["token"] == "B2" and tg.sent[1]["reply_parameters"]["message_id"] == s["message_id"]
    # агент без своего бота пишет через бота-слушателя (первый с токеном)
    c.post("/messages", headers=h("T3"), json={"kind": "idea", "text": "идея"})
    assert wait_until(lambda: len(tg.sent) == 3)
    assert tg.sent[2]["token"] == "B1" and "Claude #2 · идея" in tg.sent[2]["text"]


def test_human_messages_from_telegram(hub):
    c, tg = hub
    mid = c.post("/messages", headers=h("T1"), json={"kind": "question", "text": "брать X?"}).json()["id"]
    assert wait_until(lambda: len(tg.sent) == 1)
    tg.updates += [
        {"update_id": 5, "message": {"message_id": 7, "chat": {"id": CHAT}, "from": {"first_name": "Иван"},
                                      "text": "да, бери", "reply_to_message": {"message_id": tg.sent[0]["message_id"]}}},
        {"update_id": 6, "message": {"message_id": 8, "chat": {"id": CHAT}, "from": {"is_bot": True}, "text": "бот"}},
        {"update_id": 7, "message": {"message_id": 9, "chat": {"id": 1}, "from": {"first_name": "Чужой"}, "text": "x"}},
    ]
    assert wait_until(lambda: any(m["agent"] == "human" for m in c.get("/messages", headers=h("T1")).json()))
    hm = [m for m in c.get("/inbox", headers=h("T1")).json()["messages"] if m["agent"] == "human"]
    assert len(hm) == 1 and hm[0]["author"] == "Иван" and hm[0]["reply_to"] == mid and hm[0]["tg_state"] == "skip"
    assert len(tg.sent) == 1  # сообщения людей обратно в Telegram не дублируются


def test_claims(hub):
    c, tg = hub
    r = c.post("/claims", headers=h("T1"), json={"topic": "Порог  F1", "note": "S029"})
    assert r.status_code == 201
    cid = r.json()["id"]
    r = c.post("/claims", headers=h("T2"), json={"topic": "порог f1"})
    assert r.status_code == 409 and "Claude #1" in r.json()["detail"]["message"]
    assert c.post("/claims", headers=h("T1"), json={"topic": "порог f1"}).json()["id"] == cid  # продление
    assert [x["id"] for x in c.get("/claims", headers=h("T2")).json()] == [cid]
    assert c.post("/claims/release", headers=h("T2"), params={"key": str(cid)}).status_code == 404  # чужой
    assert c.post("/claims/release", headers=h("T1"), params={"key": "ПОРОГ F1"}).status_code == 200
    assert c.get("/claims", headers=h("T1")).json() == []
    assert c.post("/claims", headers=h("T2"), json={"topic": "порог f1"}).status_code == 201
    kinds = [m["kind"] for m in c.get("/messages", headers=h("T1")).json()]
    assert kinds == ["claim", "release", "claim"]


def test_counters(hub):
    c, _ = hub
    assert c.put("/counters/exp", headers=h("T1"), json={"value": 28}).status_code == 403
    assert c.put("/counters/exp", headers=h("ADM"), json={"value": 28}).status_code == 200
    assert [c.post("/counters/exp/next", headers=h(t)).json()["value"] for t in ("T1", "T2")] == [29, 30]
    assert c.get("/counters", headers=h("T3")).json() == {"exp": 30}


def test_history_last(hub):
    c, _ = hub
    for i in range(5):
        c.post("/messages", headers=h("T1"), json={"kind": "note", "text": f"n{i}"})
    assert [m["text"] for m in c.get("/messages", headers=h("T2"), params={"last": 2}).json()] == ["n3", "n4"]
    assert [m["text"] for m in c.get("/messages", headers=h("T2"), params={"after": 3}).json()] == ["n3", "n4"]


def test_split_long():
    parts = split("строка\n" * 1500)
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)


def test_client_cli(hub, tmp_path, monkeypatch):
    """Клиент на stdlib против настоящего HTTP-сервера хаба."""
    import threading
    import uvicorn
    cfg = {"db": str(tmp_path / "cli.db"), "admin_token": "ADM", "agents": {"a": {"title": "A", "api_token": "TA"}}}
    server = uvicorn.Server(uvicorn.Config(create_app(cfg), host="127.0.0.1", port=18787, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    assert wait_until(lambda: server.started)
    env = {"AGENT_HUB_URL": "http://127.0.0.1:18787", "AGENT_HUB_TOKEN": "TA", "PATH": "/usr/bin:/bin"}
    run = lambda *a, inp=None: subprocess.run([sys.executable, str(ROOT / "client" / "hub.py"), *a], env=env,  # noqa
                                              input=inp, capture_output=True, text=True, timeout=30)
    assert run("whoami").stdout.count('"a"') == 1
    assert run("post", "finding", "-", "--ref", "r1", inp="многострочная\nнаходка").stdout.startswith("опубликовано #1")
    assert "многострочная" in run("history").stdout
    assert run("claim", "тема").returncode == 0
    r = run("post", "bogus", "x")
    assert r.returncode != 0
    assert run("next", "exp").stdout.strip() == "1"
    server.should_exit = True
    th.join(5)
