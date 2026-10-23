"""Хаб целиком (HTTP API, очередь Telegram, команды и админка в Telegram, CLI) с поддельным Telegram."""
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from hub.server import create_app
from hub.telegram import split, wait_until

ROOT = Path(__file__).resolve().parent.parent
CHAT, ADMIN_UID = -100777, 4242


class FakeTelegram:
    def __init__(self):
        self.sent, self.updates, self.deleted, self.next_id, self.uid = [], [], [], 1000, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        token, method = request.url.path.split("/")[1][3:], request.url.path.split("/")[2]
        body = json.loads(request.content or b"{}")
        if method == "sendMessage":
            self.next_id += 1
            self.sent.append({"token": token, **body, "message_id": self.next_id})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})
        if method == "getUpdates":
            ups = [u for u in self.updates if u["update_id"] >= body.get("offset", 0)]
            if not ups:
                time.sleep(0.05)
            return httpx.Response(200, json={"ok": True, "result": ups})
        if method == "getMe":
            if token == "BAD":
                return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
            return httpx.Response(200, json={"ok": True, "result": {"username": f"bot_{token}"}})
        if method == "editMessageText":
            self.sent.append({"token": token, **body, "edited": True})
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "answerCallbackQuery":
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "deleteMessage":
            self.deleted.append(body["message_id"])
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(404, json={"ok": False, "description": "unknown"})

    def user_says(self, text, chat=CHAT, uid=1, name="Иван", reply_to=None):
        self.uid += 1
        m = {"message_id": 5000 + self.uid, "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup"},
             "from": {"id": uid, "first_name": name}, "text": text}
        if reply_to:
            m["reply_to_message"] = {"message_id": reply_to}
        self.updates.append({"update_id": self.uid, "message": m})
        return m["message_id"]

    def press(self, data, chat=None, uid=None):
        chat = chat or ADMIN_UID
        self.uid += 1
        q = {"id": f"q{self.uid}", "from": {"id": uid or chat}, "data": data,
             "message": {"message_id": 9000 + self.uid,
                         "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup"}}}
        self.updates.append({"update_id": self.uid, "callback_query": q})

    def last_markup(self, chat):
        return [s for s in self.sent if s["chat_id"] == chat and s.get("reply_markup")][-1]["reply_markup"]

    def buttons(self, chat):
        return [b["callback_data"] for row in self.last_markup(chat)["inline_keyboard"] for b in row]

    def replies_to(self, chat):
        return [s["text"] for s in self.sent if s["chat_id"] == chat]


@pytest.fixture
def hub(tmp_path):
    cfg = {"db": str(tmp_path / "hub.db"), "chat_id": CHAT, "admin_token": "ADM", "admin_code": "c0de",
           "public_url": "https://hub.test", "digest_hour": None,
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
    assert c.get("/me", headers=h("T1")).json() == {"agent": "claude1", "title": "Claude #1", "role": ""}


def test_post_inbox_and_telegram(hub):
    c, tg = hub
    assert c.post("/messages", headers=h("T1"), json={"kind": "report", "text": "без ссылки"}).status_code == 422
    r = c.post("/messages", headers=h("T1"), json={"kind": "report", "text": "A/B: +1.18", "refs": ["run ab1"]})
    mid = r.json()["id"]
    assert c.post("/messages", headers=h("T1"), json={"kind": "system", "text": "x"}).status_code == 422
    assert c.get("/inbox", headers=h("T1")).json()["messages"] == []  # своё не показывается
    got = c.get("/inbox", headers=h("T2")).json()["messages"]
    assert [m["id"] for m in got] == [mid] and got[0]["refs"] == ["run ab1"] and got[0]["status"] == "unreviewed"
    assert c.get("/inbox", headers=h("T2")).json()["messages"] == []
    assert wait_until(lambda: len(tg.sent) == 1)
    s = tg.sent[0]
    assert s["token"] == "B1" and s["chat_id"] == CHAT and "Claude #1 · отчёт · #1" in s["text"]
    c.post("/messages", headers=h("T2"), json={"kind": "answer", "text": "проверю", "reply_to": mid})
    assert wait_until(lambda: len(tg.sent) == 2)
    assert tg.sent[1]["token"] == "B2" and tg.sent[1]["reply_parameters"]["message_id"] == s["message_id"]
    c.post("/messages", headers=h("T3"), json={"kind": "idea", "text": "идея"})  # агент без бота — через слушателя
    assert wait_until(lambda: len(tg.sent) == 3)
    assert tg.sent[2]["token"] == "B1" and "Claude #2 · идея" in tg.sent[2]["text"]


def test_human_messages_and_group_commands(hub):
    c, tg = hub
    mid = c.post("/messages", headers=h("T1"), json={"kind": "question", "text": "брать X?"}).json()["id"]
    assert wait_until(lambda: len(tg.sent) == 1)
    tg.user_says("да, бери", reply_to=tg.sent[0]["message_id"])
    tg.user_says("чужой чат", chat=-1)
    tg.user_says("/status@hub_bot")
    tg.user_says("/add evil Злой")  # админ-команда в группе не выполняется
    assert wait_until(lambda: len(tg.replies_to(CHAT)) >= 3)
    hm = [m for m in c.get("/inbox", headers=h("T1")).json()["messages"] if m["agent"] == "human"]
    assert len(hm) == 1 and hm[0]["author"] == "Иван" and hm[0]["reply_to"] == mid
    texts = tg.replies_to(CHAT)
    assert any(t.startswith("Агенты:") for t in texts) and any("только в личке" for t in texts)
    assert c.get("/agents", headers=h("T1")).json()[-1]["agent"] == "claude2"  # evil не создан


def test_telegram_admin(hub):
    c, tg = hub
    tg.user_says("/add newbie Новичок", chat=ADMIN_UID, uid=ADMIN_UID)
    assert wait_until(lambda: tg.replies_to(ADMIN_UID))
    assert "/admin <код" in tg.replies_to(ADMIN_UID)[0]  # не админ — отказ
    tg.user_says("/admin wrong", chat=ADMIN_UID, uid=ADMIN_UID)
    tg.user_says("/admin c0de", chat=ADMIN_UID, uid=ADMIN_UID)
    tg.user_says("/add newbie Новичок агент", chat=ADMIN_UID, uid=ADMIN_UID)
    assert wait_until(lambda: any("создан" in t for t in tg.replies_to(ADMIN_UID)))
    txt = next(t for t in tg.replies_to(ADMIN_UID) if "создан" in t)
    token = txt.split("один раз):\n")[1].split("\n")[0]
    assert '"url": "https://hub.test"' in txt
    assert c.get("/me", headers=h(token)).json()["title"] == "Новичок агент"
    # бот: проверка getMe, сообщение с токеном удаляется
    mid = tg.user_says("/bot newbie NEWBOT", chat=ADMIN_UID, uid=ADMIN_UID)
    tg.user_says("/bot newbie BAD", chat=ADMIN_UID, uid=ADMIN_UID)
    tg.user_says("/role newbie валидация и утечки", chat=ADMIN_UID, uid=ADMIN_UID)
    assert wait_until(lambda: any("Роль: валидация и утечки" in t for t in tg.replies_to(ADMIN_UID)))
    assert mid in tg.deleted and any("@bot_NEWBOT привязан" in t for t in tg.replies_to(ADMIN_UID))
    assert any("Telegram не принял токен" in t for t in tg.replies_to(ADMIN_UID))
    assert c.get("/me", headers=h(token)).json()["role"] == "валидация и утечки"
    c.post("/messages", headers=h(token), json={"kind": "note", "text": "привет"})
    assert wait_until(lambda: any(s["token"] == "NEWBOT" and "привет" in s["text"] for s in tg.sent))
    # новый токен, выключение
    tg.user_says("/token newbie", chat=ADMIN_UID, uid=ADMIN_UID)
    assert wait_until(lambda: any("Новый токен" in t for t in tg.replies_to(ADMIN_UID)))
    assert c.get("/me", headers=h(token)).status_code == 401
    new = next(t for t in tg.replies_to(ADMIN_UID) if "Новый токен" in t).split("\n")[1]
    assert c.get("/me", headers=h(new)).status_code == 200
    tg.user_says("/disable newbie", chat=ADMIN_UID, uid=ADMIN_UID)
    assert wait_until(lambda: c.get("/me", headers=h(new)).status_code == 401)
    tg.user_says("/counter exp 28", chat=ADMIN_UID, uid=ADMIN_UID)
    assert wait_until(lambda: c.get("/counters", headers=h("T1")).json().get("exp") == 28)


def test_buttons(hub):
    c, tg = hub
    tg.user_says("/admin c0de", chat=ADMIN_UID, uid=ADMIN_UID)
    assert wait_until(lambda: "m:agents" in (tg.buttons(ADMIN_UID) if tg.replies_to(ADMIN_UID) else []))
    tg.press("m:agents")
    assert wait_until(lambda: "a:gpt" in tg.buttons(ADMIN_UID))
    tg.press("a:gpt")
    assert wait_until(lambda: "a:gpt:role" in tg.buttons(ADMIN_UID))
    # роль: кнопка -> бот спрашивает -> ответ текстом
    tg.press("a:gpt:role")
    assert wait_until(lambda: any("Пришлите роль" in t for t in tg.replies_to(ADMIN_UID)))
    tg.user_says("модели и признаки", chat=ADMIN_UID, uid=ADMIN_UID)
    assert wait_until(lambda: c.get("/me", headers=h("T2")).json()["role"] == "модели и признаки")
    # добавить агента кнопкой
    tg.press("m:add")
    assert wait_until(lambda: any("имя агента латиницей" in t for t in tg.replies_to(ADMIN_UID)))
    tg.user_says("Bob Боб · проверки", chat=ADMIN_UID, uid=ADMIN_UID)
    assert wait_until(lambda: any("Агент bob создан" in t for t in tg.replies_to(ADMIN_UID)))
    token = next(t for t in tg.replies_to(ADMIN_UID) if "Агент bob создан" in t).split("один раз):\n")[1].split("\n")[0]
    assert c.get("/me", headers=h(token)).json()["title"] == "Боб · проверки"
    # новый токен — только после подтверждения
    tg.press("a:gpt:tok")
    assert wait_until(lambda: "a:gpt:tok!" in tg.buttons(ADMIN_UID))
    assert c.get("/me", headers=h("T2")).status_code == 200
    tg.press("a:gpt:tok!")
    assert wait_until(lambda: c.get("/me", headers=h("T2")).status_code == 401)
    # выключить кнопкой; /cancel сбрасывает ожидание ввода
    tg.press("a:bob:off")
    assert wait_until(lambda: c.get("/me", headers=h(token)).status_code == 401)
    tg.press("a:bob:title")
    tg.user_says("/cancel", chat=ADMIN_UID, uid=ADMIN_UID)
    tg.user_says("не подпись", chat=ADMIN_UID, uid=ADMIN_UID)
    assert wait_until(lambda: tg.replies_to(ADMIN_UID)[-1].startswith("Хаб агентов"))
    assert [a["title"] for a in c.get("/agents", headers=h("T1")).json() if a["agent"] == "bob"] == ["Боб · проверки"]
    # не-админ кнопками ничего не меняет; кнопки в группе — всем
    n = len(tg.sent)
    tg.press("a:claude1:off", chat=777, uid=777)
    tg.press("g:status", chat=CHAT, uid=5)
    assert wait_until(lambda: any(s["chat_id"] == CHAT and s["text"].startswith("Агенты:") for s in tg.sent[n:]))
    assert c.get("/me", headers=h("T1")).status_code == 200


def test_group_selection(tmp_path):
    """Группа не задана при установке: бота добавили в группу -> админ выбирает её кнопкой."""
    cfg = {"db": str(tmp_path / "g.db"), "admin_token": "ADM", "admin_code": "c0de", "digest_hour": None,
           "agents": {"main": {"title": "Хаб", "api_token": "TM", "bot_token": "BM"}}}
    tg = FakeTelegram()
    with TestClient(create_app(cfg, transport=httpx.MockTransport(tg))) as c:
        c.post("/messages", headers=h("TM"), json={"kind": "note", "text": "до группы"})
        tg.uid += 1
        tg.updates.append({"update_id": tg.uid, "my_chat_member": {"chat": {"id": -555, "type": "supergroup",
                                                                             "title": "Команда"}}})
        tg.user_says("/admin c0de", chat=ADMIN_UID, uid=ADMIN_UID)
        assert wait_until(lambda: any("Группа не выбрана" in t for t in tg.replies_to(ADMIN_UID)))
        tg.press("m:group")
        assert wait_until(lambda: "grp:-555" in tg.buttons(ADMIN_UID))
        tg.press("grp:-555")
        assert wait_until(lambda: any("подключён к этой группе" in t for t in tg.replies_to(-555)))
        c.post("/messages", headers=h("TM"), json={"kind": "note", "text": "после группы"})
        assert wait_until(lambda: any("после группы" in t for t in tg.replies_to(-555)))
        assert not any("до группы" in t for t in tg.replies_to(-555))  # до выбора группы — только в ленте
        tg.user_says("люди пишут", chat=-555, uid=9)
        assert wait_until(lambda: any(m["agent"] == "human" for m in c.get("/messages", headers=h("TM")).json()))


def test_claims(hub):
    c, _ = hub
    cid = c.post("/claims", headers=h("T1"), json={"topic": "Порог  F1", "note": "S029"}).json()["id"]
    r = c.post("/claims", headers=h("T2"), json={"topic": "порог f1"})
    assert r.status_code == 409 and "Claude #1" in r.json()["detail"]["message"]
    assert c.post("/claims", headers=h("T1"), json={"topic": "порог f1"}).json()["id"] == cid  # продление
    assert c.post("/claims/release", headers=h("T2"), params={"key": str(cid)}).status_code == 404  # чужой
    assert c.post("/claims/release", headers=h("T1"), params={"key": "ПОРОГ F1"}).status_code == 200
    assert c.post("/claims", headers=h("T2"), json={"topic": "порог f1"}).status_code == 201
    assert [m["kind"] for m in c.get("/messages", headers=h("T1")).json()] == ["claim", "release", "claim"]


def test_ideas_queue(hub):
    c, _ = hub
    iid = c.post("/messages", headers=h("T1"), json={"kind": "idea", "text": "Квантильная цель\nпочему"}).json()["id"]
    assert [i["id"] for i in c.get("/ideas", headers=h("T2")).json()] == [iid]
    assert c.post(f"/ideas/{iid}/take", headers=h("T2"), json={}).status_code == 201
    assert c.post(f"/ideas/{iid}/take", headers=h("T3"), json={}).status_code == 409
    assert c.get("/ideas", headers=h("T2")).json() == []
    cl = c.get("/claims", headers=h("T1")).json()
    assert cl[0]["agent"] == "gpt" and cl[0]["topic"].startswith(f"идея #{iid}: Квантильная цель")
    assert c.post(f"/ideas/{iid}/close", headers=h("T3"), json={"outcome": "done"}).status_code == 403
    r = c.post(f"/ideas/{iid}/close", headers=h("T2"), json={"outcome": "done", "text": "+17 S"})
    assert r.json()["status"] == "done" and c.get("/claims", headers=h("T1")).json() == []


def test_reviews(hub):
    c, _ = hub
    rid = c.post("/messages", headers=h("T1"), json={"kind": "report", "text": "+1.18", "refs": ["r"]}).json()["id"]
    assert [r["id"] for r in c.get("/reviews/pending", headers=h("T2")).json()] == [rid]
    assert c.get("/reviews/pending", headers=h("T1")).json() == []  # свой отчёт себе не показывается
    assert c.post(f"/reports/{rid}/review", headers=h("T1"), json={"verdict": "confirm", "text": "x"}).status_code == 403
    r = c.post(f"/reports/{rid}/review", headers=h("T2"), json={"verdict": "confirm", "text": "повторил: +1.1"})
    assert r.json()["status"] == "confirmed"
    assert c.get("/reviews/pending", headers=h("T2")).json() == []
    r = c.post(f"/reports/{rid}/review", headers=h("T3"), json={"verdict": "refute", "text": "в A/A шум 2.0"})
    assert r.json()["status"] == "disputed"
    full = c.get(f"/messages/{rid}", headers=h("T3")).json()
    assert [x["status"] for x in full["replies"]] == ["confirm", "refute"]
    st = c.get("/status", headers=h("T1")).json()["text"]
    assert "Ждут проверки (1)" in st and "оспорен" in st


def test_digest_and_status_text(hub):
    c, _ = hub
    c.post("/messages", headers=h("T1"), json={"kind": "report", "text": "сделал X", "refs": ["r1"]})
    c.post("/messages", headers=h("T2"), json={"kind": "finding", "text": "нашёл Y"})
    text = c.get("/status", headers=h("T3")).json()["text"]
    assert "Claude #1 (claude1)" in text and "Свободные идеи (0)" in text


def test_digest(tmp_path):
    from hub.core import Hub
    from hub.store import Store
    hub = Hub({"agents": {"aa": {"title": "A", "api_token": "x"}, "bb": {"title": "B", "api_token": "y"}}},
              Store(str(tmp_path / "d.db")))
    assert hub.digest_text(time.time() - 60) is None
    rid = hub.post("aa", "report", "A/B +1.18\nподробности", ["run"])
    hub.post("bb", "finding", "в A/A шум 2 S")
    hub.review("bb", rid, "confirm", "повторил")
    d = hub.digest_text(time.time() - 86400)
    assert "отчётов 1, находок 1" in d and f"✔️ #{rid} A: A/B +1.18" in d and "в A/A шум 2 S" in d


def test_split_long():
    parts = split("строка\n" * 1500)
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)


def test_client_cli(tmp_path):
    """Клиент на stdlib против настоящего HTTP-сервера хаба."""
    import threading

    import uvicorn
    cfg = {"db": str(tmp_path / "cli.db"), "admin_token": "ADM", "digest_hour": None,
           "agents": {"aa": {"title": "A", "api_token": "TA"}, "bb": {"title": "B", "api_token": "TB"}}}
    server = uvicorn.Server(uvicorn.Config(create_app(cfg), host="127.0.0.1", port=18787, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    assert wait_until(lambda: server.started)

    def run(*a, inp=None, token="TA"):
        env = {"AGENT_HUB_URL": "http://127.0.0.1:18787", "AGENT_HUB_TOKEN": token, "PATH": "/usr/bin:/bin"}
        return subprocess.run([sys.executable, str(ROOT / "client" / "hub.py"), *a], env=env, input=inp,
                              capture_output=True, text=True, timeout=30)

    assert run("whoami").stdout.startswith("aa — «A»")
    assert run("post", "finding", "-", "--ref", "r1", inp="многострочная\nнаходка").stdout.startswith("опубликовано #1")
    assert "отчёт без --ref" in run("post", "report", "x").stderr
    assert run("post", "report", "сделал", "--ref", "c1").returncode == 0
    assert run("post", "idea", "попробовать Z").returncode == 0
    assert "взята" in run("take", "3", token="TB").stdout
    assert "#2" in run("reviews", token="TB").stdout
    assert "confirmed" in run("review", "2", "confirm", "повторил", token="TB").stdout
    assert "done" in run("close", "3", "done", "вышло", token="TB").stdout
    assert "Агенты:" in run("status").stdout
    assert run("next", "exp").stdout.strip() == "1"
    server.should_exit = True
    th.join(5)
