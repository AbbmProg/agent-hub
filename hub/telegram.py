"""Telegram: зеркало ленты (каждый агент пишет от своего бота), сообщения людей из группы — в ленту, команды.

Бот-слушатель (по умолчанию первый агент с ботом) читает группу и личку:
- в группе любой может спросить /status, /ideas, /digest, /help;
- в личке админы управляют агентами (/add, /bot, /role, ...). Токены выдаются только в личке; сообщение с токеном
  бота удаляется сразу после обработки.

Боты не получают историю чата — только новые сообщения (недоставленные Telegram хранит ~24 ч). Поэтому память —
лента хаба, а Telegram — витрина и канал для людей.
"""
import logging
import threading
import time

import httpx

from .core import Hub
from .tgadmin import GROUP_KB, AdminUI

log = logging.getLogger("agent-hub.telegram")

KIND_LABEL = {
    "report": ("📊", "отчёт"), "finding": ("🔎", "находка"), "idea": ("💡", "идея"), "question": ("❓", "вопрос"),
    "answer": ("💬", "ответ"), "note": ("📝", "заметка"), "claim": ("📌", "беру в работу"),
    "release": ("✅", "освободил"), "system": ("⚙️", "хаб"), "review": ("🔁", "проверка"),
}
VERDICT_LABEL = {"confirm": "✔️ подтверждаю", "refute": "❌ опровергаю"}
LIMIT = 4000  # у Telegram 4096 символов на сообщение

GROUP_HELP = """Команды хаба: /status — кто чем занят, что ждёт проверки; /ideas — свободные идеи; /digest — сводка за сутки;
/menu — кнопки. Обычные сообщения в этой группе получают все агенты; ответ реплаем на сообщение бота привязывается к нему."""


def render(msg: dict, reply_tg: int | None = None) -> str:
    icon, label = KIND_LABEL.get(msg["kind"], ("•", msg["kind"]))
    if msg["kind"] == "review":
        label += " · " + VERDICT_LABEL.get(msg.get("status"), "")
    lines = [f"{icon} {msg['author']} · {label} · #{msg['id']}"]
    if msg.get("reply_to") and not reply_tg:
        lines[0] += f" (к #{msg['reply_to']})"
    lines += ["", msg["text"]]
    if msg.get("refs"):
        lines += [""] + [f"🔗 {r}" for r in msg["refs"]]
    return "\n".join(lines)


def split(text: str, n: int = LIMIT) -> list[str]:
    parts = []
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts + [text]


class Telegram:
    def __init__(self, cfg: dict, hub: Hub, transport: httpx.BaseTransport | None = None):
        self.cfg, self.hub, self.store = cfg, hub, hub.store
        self.chat_id = cfg.get("chat_id")
        self.http = httpx.Client(timeout=httpx.Timeout(40.0, connect=15.0), proxy=cfg.get("tg_proxy") or None,
                                 transport=transport)
        self.base = (cfg.get("tg_api") or "https://api.telegram.org").rstrip("/")
        self.stop = threading.Event()
        self.admin = AdminUI(self)

    # ---------- боты ----------
    def listener_token(self) -> str | None:
        name = self.cfg.get("listener")
        a = self.store.agent(name) if name else None
        if a and a["bot_token"]:
            return a["bot_token"]
        return next((a["bot_token"] for a in self.store.agents(with_disabled=False) if a["bot_token"]), None)

    def token_for(self, msg: dict) -> str | None:
        a = self.store.agent(msg["agent"])
        return (a or {}).get("bot_token") or self.listener_token()  # хаб и агенты без бота — через слушателя

    @property
    def enabled(self) -> bool:
        return bool(self.chat_id) and self.listener_token() is not None

    def call(self, token: str, method: str, **params) -> dict:
        r = self.http.post(f"{self.base}/bot{token}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise TgError(data.get("description", r.text[:200]), r.status_code,
                          (data.get("parameters") or {}).get("retry_after"))
        return data["result"]

    def reply(self, chat_id: int, text: str):
        token = self.listener_token()
        for part in split(text):
            self.call(token, "sendMessage", chat_id=chat_id, text=part, disable_web_page_preview=True)

    # ---------- отправка ленты ----------
    def send(self, msg: dict) -> int | None:
        token = self.token_for(msg)
        if not token or not self.chat_id:
            return None
        parent = self.store.get(msg["reply_to"]) if msg.get("reply_to") else None
        reply_tg = parent.get("tg_msg") if parent else None
        first = None
        for i, part in enumerate(split(render(msg, reply_tg))):
            params = {"chat_id": self.chat_id, "text": part, "disable_web_page_preview": True}
            if i == 0 and reply_tg:
                params["reply_parameters"] = {"message_id": reply_tg, "allow_sending_without_reply": True}
            res = self.call(token, "sendMessage", **params)
            first = first or res["message_id"]
        return first

    def sender_loop(self):
        backoff = 1.0
        while not self.stop.is_set():
            batch = self.store.pending_tg()
            if not batch:
                self.stop.wait(1.0)
                continue
            for msg in batch:
                if msg["agent"] == "human" or not self.enabled:
                    self.store.set_tg(msg["id"], "skip")
                    continue
                try:
                    tg_msg = self.send(msg)
                    self.store.set_tg(msg["id"], "sent" if tg_msg else "skip", tg_msg)
                    backoff = 1.0
                    self.stop.wait(1.1)  # в группу — не чаще ~20 сообщений в минуту
                except TgError as e:
                    if e.retry_after:
                        self.stop.wait(e.retry_after + 1)
                    elif 400 <= e.status < 500:
                        log.error("сообщение #%s не принято Telegram: %s", msg["id"], e)
                        self.store.set_tg(msg["id"], "failed")
                        continue
                    else:
                        self.stop.wait(backoff)
                        backoff = min(backoff * 2, 60)
                    break
                except (httpx.HTTPError, ValueError) as e:  # сеть: сообщение остаётся в очереди
                    log.warning("Telegram недоступен (%r), повтор через %.0f с", e, backoff)
                    self.stop.wait(backoff)
                    backoff = min(backoff * 2, 60)
                    break

    # ---------- приём ----------
    def poll_once(self, token: str, timeout: int = 25) -> int:
        offset = int(self.store.kv_get("tg_offset", 0))
        ups = self.call(token, "getUpdates", offset=offset, timeout=timeout,
                        allowed_updates=["message", "callback_query"])
        for up in ups:
            self.store.kv_set("tg_offset", up["update_id"] + 1)
            try:
                if up.get("callback_query"):
                    self.admin.handle_callback(up["callback_query"])
                    continue
                m = up.get("message") or {}
                u = m.get("from") or {}
                if not u.get("is_bot"):
                    self.handle(m, u)
            except (TgError, httpx.HTTPError) as e:
                log.warning("ответ в Telegram не ушёл: %s", e)
        return len(ups)

    def handle(self, m: dict, u: dict):
        chat = m.get("chat") or {}
        text = (m.get("text") or m.get("caption") or "").strip()
        if not text:
            return
        if chat.get("type") == "private":
            return self.admin.handle_message(m, u, text)
        if chat.get("id") != self.chat_id:
            return
        if text.startswith("/"):
            cmd = text.split()[0].split("@")[0].lower()
            out = {"/status": self.hub.status_text, "/ideas": self.ideas_text, "/help": lambda: GROUP_HELP,
                   "/digest": lambda: self.hub.digest_text(time.time() - 86400) or "За сутки сообщений не было.",
                   "/start": lambda: GROUP_HELP, "/menu": lambda: "Хаб агентов:"}.get(cmd)
            if out:
                return self.admin.send(chat["id"], out(), GROUP_KB)
            if cmd in ADMIN_ONLY:
                return self.reply(chat["id"], "Управление — только в личке с ботом.")
        name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or u.get("username") or "человек"
        reply = m.get("reply_to_message") or {}
        parent = self.store.by_tg_msg(reply["message_id"]) if reply.get("message_id") else None
        self.store.add_message("human", name, "human", text, reply_to=parent["id"] if parent else None,
                               tg_state="skip", tg_msg=m.get("message_id"))

    def ideas_text(self) -> str:
        ideas = self.hub.ideas("open")
        if not ideas:
            return "Свободных идей нет."
        return "Свободные идеи (агент берёт: hub take <id>):\n" + "\n".join(
            f"• #{i['id']} {i['author']}: {i['text'].strip().splitlines()[0][:120]}" for i in ideas[-20:])

    def poller_loop(self):
        backoff = 1.0
        while not self.stop.is_set():
            token = self.listener_token()
            if not token:
                self.stop.wait(5)
                continue
            try:
                self.poll_once(token)
                backoff = 1.0
            except TgError as e:
                log.warning("getUpdates: %s", e)
                self.stop.wait(e.retry_after or backoff)
                backoff = min(backoff * 2, 60)
            except (httpx.HTTPError, ValueError) as e:
                log.warning("getUpdates: Telegram недоступен (%r)", e)
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60)

    def start(self):
        if not self.chat_id:
            log.warning("Telegram-группа не задана (chat_id): лента работает, чат и команды — нет")
        for target in (self.sender_loop, self.poller_loop):
            threading.Thread(target=target, daemon=True, name=target.__name__).start()


ADMIN_ONLY = {"/add", "/bot", "/role", "/title", "/disable", "/enable", "/token", "/counter", "/release", "/agents",
              "/admin"}


class TgError(Exception):
    def __init__(self, desc: str, status: int, retry_after: int | None = None):
        super().__init__(f"{status}: {desc}")
        self.status, self.retry_after = status, retry_after


def wait_until(cond, timeout=5.0):
    """Для тестов: дождаться условия."""
    t = time.time()
    while time.time() - t < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False
