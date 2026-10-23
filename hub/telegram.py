"""Telegram: зеркало ленты (каждый агент пишет от своего бота), сообщения людей из группы — в ленту, команды.

Бот-слушатель (по умолчанию первый агент с ботом) читает группу и личку:
- в группе любой может спросить /status, /ideas, /digest, /help;
- в личке админы управляют агентами (/add, /bot, /role, ...). Токены выдаются только в личке; сообщение с токеном
  бота удаляется сразу после обработки.

Боты не получают историю чата — только новые сообщения (недоставленные Telegram хранит ~24 ч). Поэтому память —
лента хаба, а Telegram — витрина и канал для людей.
"""
import json
import logging
import re
import threading
import time

import httpx

from .core import AGENT_KINDS, Hub
from .tgadmin import GROUP_KB, AdminUI

log = logging.getLogger("agent-hub.telegram")
TOKEN_RE = re.compile(r"\d{6,}:[\w-]{30,}")


def redact(e) -> str:
    """Текст ошибки без токенов ботов (они бывают в адресе запроса)."""
    return TOKEN_RE.sub("<token>", repr(e))

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
        self.cfg_chat_id = cfg.get("chat_id")
        self.http = httpx.Client(timeout=httpx.Timeout(40.0, connect=15.0), proxy=cfg.get("tg_proxy") or None,
                                 transport=transport)
        self.base = (cfg.get("tg_api") or "https://api.telegram.org").rstrip("/")
        self.stop = threading.Event()
        self.admin = AdminUI(self)
        # «агент работает»: сообщение-статус от бота агента + «печатает…»; исчезает с его следующим сообщением
        self.busy: dict[str, dict] = {}
        self.busy_lock = threading.Lock()
        self.busy_interval = float(cfg.get("busy_interval", 5.0))
        self.busy_wake = threading.Event()  # новый статус — показать сразу, не ждать такта
        self.acked: dict[str, set] = {}     # агент -> сообщения людей (tg id), которые он взял (👀) и ещё не ответил

    # ---------- статус «думает…» ----------
    def set_busy(self, agent: str, text: str, ttl: float):
        with self.busy_lock:
            b = self.busy.setdefault(agent, {"tg_msg": None, "shown": None})
            b.update(text=text.strip() or "думает…", until=time.time() + ttl)
        self.busy_wake.set()

    # ---------- «услышал»: реакции на сообщения людей ----------
    def react(self, agent: str, tg_msg: int, emoji: str):
        if not self.enabled:
            return
        try:
            self.call(self.token_for({"agent": agent}), "setMessageReaction", chat_id=self.chat_id,
                      message_id=tg_msg, reaction=[{"type": "emoji", "emoji": emoji}])
        except (TgError, httpx.HTTPError) as e:
            log.warning("реакция не поставлена: %s", redact(e))

    def picked_up(self, agent: str, msgs: list):
        """Агент взял сообщения людей: 👀 на каждое и статус «думает…» — люди сразу видят, что их услышали."""
        new = [m for m in msgs if m["agent"] == "human" and m.get("tg_msg")
               and m["tg_msg"] not in self.acked.get(agent, set())]
        if not new:
            return
        for m in new:
            self.acked.setdefault(agent, set()).add(m["tg_msg"])
            self.react(agent, m["tg_msg"], "👀")
        if agent not in self.busy:
            self.set_busy(agent, "думает", 15 * 60)

    def answered(self, agent: str):
        """Агент написал в ленту — его 👀 меняются на 👍."""
        for tg_msg in self.acked.pop(agent, set()):
            self.react(agent, tg_msg, "👍")

    def clear_busy(self, agent: str):
        with self.busy_lock:
            b = self.busy.pop(agent, None)
        if b and b.get("tg_msg") and self.chat_id:
            self._delete(agent, b["tg_msg"])

    def _delete(self, agent: str, tg_msg: int):
        try:
            self.call(self.token_for({"agent": agent}), "deleteMessage", chat_id=self.chat_id, message_id=tg_msg)
        except (TgError, httpx.HTTPError):
            pass

    def busy_loop(self):
        while not self.stop.is_set():
            self.busy_wake.wait(self.busy_interval)
            self.busy_wake.clear()
            for agent, b in list(self.busy.items()):
                if time.time() > b["until"]:
                    self.clear_busy(agent)
                    continue
                if not self.enabled:
                    continue
                # бегущие точки: «думает.» → «думает..» → «думает...»; через минуту — сколько уже работает
                b["tick"] = b.get("tick", -1) + 1
                core = b["text"].rstrip(".… ")
                mins = int((time.time() - b.setdefault("since", time.time())) // 60)
                text = f"⏳ {self.hub.title(agent)} · {core}{'.' * (b['tick'] % 3 + 1)}" + (f" ({mins} мин)" if mins else "")
                token = self.token_for({"agent": agent})
                try:
                    if b["tg_msg"] is None:
                        b["tg_msg"] = self.call(token, "sendMessage", chat_id=self.chat_id, text=text,
                                                disable_notification=True)["message_id"]
                        b["shown"] = text
                        with self.busy_lock:
                            gone = self.busy.get(agent) is not b
                        if gone:  # ответ агента пришёл, пока статус отправлялся
                            self._delete(agent, b["tg_msg"])
                            continue
                    elif b["shown"] != text:
                        self.call(token, "editMessageText", chat_id=self.chat_id, message_id=b["tg_msg"], text=text)
                        b["shown"] = text
                    self.call(token, "sendChatAction", chat_id=self.chat_id, action="typing")
                except (TgError, httpx.HTTPError) as e:
                    log.warning("статус агента %s не показан: %s", agent, redact(e))

    def backfill_bots(self):
        """У ботов, привязанных до этой версии или из конфига установки, узнать @username (для обращений в группе)."""
        for a in self.store.agents():
            if a["bot_token"] and not a.get("bot_id"):
                try:
                    me = self.call(a["bot_token"], "getMe")
                    self.store.update_agent(a["name"], bot_username=me.get("username"), bot_id=me["id"])
                except (TgError, httpx.HTTPError, KeyError):
                    pass

    # ---------- группа ----------
    @property
    def chat_id(self) -> int | None:
        """Группа хаба: выбранная админом кнопкой (хранится в базе) или из конфига установки."""
        v = self.store.kv_get("chat_id")
        return int(v) if v else self.cfg_chat_id

    def set_chat(self, chat_id: int):
        """Выбрать группу; накопленное до выбора остаётся только в ленте (в чат не вываливаем старое)."""
        if not self.chat_id:
            self.store.skip_pending_tg()
        self.store.kv_set("chat_id", chat_id)

    def seen_groups(self) -> dict:
        try:
            return {int(k): v for k, v in json.loads(self.store.kv_get("seen_groups", "{}")).items()}
        except ValueError:
            return {}

    def remember_group(self, chat: dict):
        """Группы, куда добавили бота или где он видел сообщения, — кандидаты для выбора в меню «Группа»."""
        if chat.get("type") not in ("group", "supergroup") or chat.get("id") == self.chat_id:
            return
        seen = self.seen_groups()
        if seen.get(chat["id"]) != chat.get("title", ""):
            seen[chat["id"]] = chat.get("title", "")
            self.store.kv_set("seen_groups", json.dumps(seen, ensure_ascii=False))

    # ---------- боты ----------
    def listener_name(self) -> str | None:
        """Бот хаба: читает группу и личку, пишет за агентов без своего бота. Выключение его агента (отзыв токена
        API) Telegram не останавливает — иначе админ потерял бы и меню, чтобы включить его обратно."""
        name = self.cfg.get("listener")
        a = self.store.agent(name) if name else None
        if a and a["bot_token"]:
            return name
        return next((a["name"] for a in self.store.agents() if a["bot_token"]), None)

    def listener_token(self) -> str | None:
        name = self.listener_name()
        return self.store.agent(name)["bot_token"] if name else None

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
                    if msg["kind"] in AGENT_KINDS and msg["agent"] in self.busy:
                        self.clear_busy(msg["agent"])  # агент ответил — «думает…» убираем
                    tg_msg = self.send(msg)
                    if msg["kind"] in AGENT_KINDS:
                        self.answered(msg["agent"])
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
                    log.warning("Telegram недоступен (%s), повтор через %.0f с", redact(e), backoff)
                    self.stop.wait(backoff)
                    backoff = min(backoff * 2, 60)
                    break

    # ---------- приём ----------
    def poll_once(self, token: str, timeout: int = 25) -> int:
        offset = int(self.store.kv_get("tg_offset", 0))
        ups = self.call(token, "getUpdates", offset=offset, timeout=timeout,
                        allowed_updates=["message", "callback_query", "my_chat_member"])
        for up in ups:
            self.store.kv_set("tg_offset", up["update_id"] + 1)
            try:
                if up.get("callback_query"):
                    self.admin.handle_callback(up["callback_query"])
                    continue
                if up.get("my_chat_member"):  # бота добавили в группу / изменили права
                    self.remember_group(up["my_chat_member"].get("chat") or {})
                    continue
                m = up.get("message") or {}
                u = m.get("from") or {}
                if not u.get("is_bot"):
                    self.handle(m, u)
            except (TgError, httpx.HTTPError) as e:
                log.warning("ответ в Telegram не ушёл: %s", redact(e))
            except Exception:  # noqa: BLE001  (ошибка в обработке одного сообщения не должна останавливать приём)
                log.exception("сбой при обработке обновления Telegram %s", up.get("update_id"))
        return len(ups)

    def handle(self, m: dict, u: dict):
        chat = m.get("chat") or {}
        text = (m.get("text") or m.get("caption") or "").strip()
        if not text:
            return
        if chat.get("type") == "private":
            return self.admin.handle_message(m, u, text)
        if chat.get("id") != self.chat_id:
            self.remember_group(chat)
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
        uid = u.get("id")
        if uid is not None:
            self.store.see_person(uid, name)
        trusted = self.store.is_trusted(uid)
        # status сообщения человека: trusted / untrusted — агенты выполняют указания только доверенных
        self.store.add_message("human", name, "human", text, reply_to=parent["id"] if parent else None,
                               tg_state="skip", tg_msg=m.get("message_id"), tg_user=uid,
                               status="trusted" if trusted else "untrusted")

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
                log.warning("getUpdates: Telegram недоступен (%s)", redact(e))
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60)

    def start(self):
        if not self.chat_id:
            log.warning("Telegram-группа не выбрана: добавьте бота в группу и выберите её в меню «Группа»")
        threading.Thread(target=self.backfill_bots, daemon=True, name="backfill_bots").start()
        for target in (self.sender_loop, self.poller_loop, self.busy_loop):
            threading.Thread(target=target, daemon=True, name=target.__name__).start()


ADMIN_ONLY = {"/add", "/bot", "/role", "/title", "/project", "/disable", "/enable", "/token", "/counter", "/release",
              "/agents", "/admin"}


class TgError(Exception):
    def __init__(self, desc: str, status: int, retry_after: int | None = None):
        super().__init__(f"{status}: {desc}")
        self.status, self.retry_after = status, retry_after


def wait_until(cond, timeout=15.0):
    """Для тестов: дождаться условия."""
    t = time.time()
    while time.time() - t < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False
