"""Telegram: зеркало ленты (каждый агент пишет от своего бота), сообщения людей из группы — в ленту, команды.

Бот-слушатель (по умолчанию первый агент с ботом) читает группу и личку:
- в группе любой может спросить /status, /ideas, /digest, /help;
- в личке админы управляют агентами (/add, /bot, /role, ...). Токены выдаются только в личке; сообщение с токеном
  бота удаляется сразу после обработки.

Боты не получают историю чата — только новые сообщения (недоставленные Telegram хранит ~24 ч). Поэтому память —
лента хаба, а Telegram — витрина и канал для людей.
"""
import hmac
import logging
import threading
import time

import httpx

from .core import Hub, HubError

log = logging.getLogger("agent-hub.telegram")

KIND_LABEL = {
    "report": ("📊", "отчёт"), "finding": ("🔎", "находка"), "idea": ("💡", "идея"), "question": ("❓", "вопрос"),
    "answer": ("💬", "ответ"), "note": ("📝", "заметка"), "claim": ("📌", "беру в работу"),
    "release": ("✅", "освободил"), "system": ("⚙️", "хаб"), "review": ("🔁", "проверка"),
}
VERDICT_LABEL = {"confirm": "✔️ подтверждаю", "refute": "❌ опровергаю"}
LIMIT = 4000  # у Telegram 4096 символов на сообщение

ADMIN_HELP = """Управление хабом (только в личке, только админам):
/agents — агенты, роли, боты, последняя активность
/add <имя> <Заголовок> — новый агент; пришлю его токен и настройку клиента
/bot <имя> <токен бота> — привязать бота (сообщение с токеном удалю); бота добавьте в группу
/role <имя> <текст> — роль/направление агента (агент видит её в hub whoami)
/title <имя> <текст> — подпись в чате
/disable <имя> · /enable <имя> — выключить/включить агента
/token <имя> — выпустить новый токен (старый перестанет работать)
/counter <имя> <число> — задать общий счётчик (например, номер последнего эксперимента)
/release <id захвата> — снять чужой захват
/status · /digest · /ideas — то же, что в группе"""
GROUP_HELP = """Команды хаба: /status — кто чем занят, что ждёт проверки; /ideas — свободные идеи; /digest — сводка за сутки.
Обычные сообщения в этой группе получают все агенты; ответ реплаем на сообщение бота привязывается к нему."""


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
        ups = self.call(token, "getUpdates", offset=offset, timeout=timeout, allowed_updates=["message"])
        for up in ups:
            self.store.kv_set("tg_offset", up["update_id"] + 1)
            m = up.get("message") or {}
            u = m.get("from") or {}
            if u.get("is_bot"):
                continue
            try:
                self.handle(m, u)
            except TgError as e:
                log.warning("ответ в Telegram не ушёл: %s", e)
        return len(ups)

    def handle(self, m: dict, u: dict):
        chat = m.get("chat") or {}
        text = (m.get("text") or m.get("caption") or "").strip()
        if not text:
            return
        if chat.get("type") == "private":
            return self.handle_private(m, u, text)
        if chat.get("id") != self.chat_id:
            return
        if text.startswith("/"):
            cmd = text.split()[0].split("@")[0].lower()
            out = {"/status": self.hub.status_text, "/ideas": self.ideas_text, "/help": lambda: GROUP_HELP,
                   "/digest": lambda: self.hub.digest_text(time.time() - 86400) or "За сутки сообщений не было.",
                   "/start": lambda: GROUP_HELP}.get(cmd)
            if out:
                return self.reply(chat["id"], out())
            if cmd in ADMIN_COMMANDS:
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

    # ---------- админ в личке ----------
    def handle_private(self, m: dict, u: dict, text: str):
        uid, chat_id = u.get("id"), m["chat"]["id"]
        parts = text.split(maxsplit=2)
        cmd = parts[0].split("@")[0].lower()
        if cmd == "/admin":
            code = self.cfg.get("admin_code")
            ok = bool(code) and len(parts) > 1 and hmac.compare_digest(parts[1], code)
            if ok:
                self.store.add_admin(uid, u.get("username") or u.get("first_name") or str(uid))
                return self.reply(chat_id, "Вы админ хаба.\n\n" + ADMIN_HELP)
            return self.reply(chat_id, "Неверный код.")
        if not self.store.is_admin(uid):
            return self.reply(chat_id, "Я бот хаба агентов. Управление — для админов: /admin <код из установки>.")
        if cmd not in ADMIN_COMMANDS:
            return self.reply(chat_id, ADMIN_HELP)
        try:
            out = ADMIN_COMMANDS[cmd](self, parts[1:], m)
        except HubError as e:
            out = f"Ошибка: {e.message}"
        except ValueError as e:
            out = f"Ошибка: {e}"
        except TgError as e:
            out = f"Telegram ответил: {e}"
        if out:
            self.reply(chat_id, out)

    def admin_agents(self, args, m):
        act, now = self.store.last_activity(), time.time()
        rows = []
        for a in self.store.agents():
            t = act.get(a["name"])
            rows.append(f"{'🚫' if a['disabled'] else '•'} {a['name']} — «{a['title']}»"
                        f"{', бот ✓' if a['bot_token'] else ', без бота'}"
                        f"{', ' + a['role'] if a['role'] else ''}"
                        f"{'' if t is None else f', {int((now - t) // 60)} мин назад'}")
        return "Агенты:\n" + "\n".join(rows) if rows else "Агентов нет: /add <имя> <Заголовок>"

    def admin_add(self, args, m):
        if not args:
            raise ValueError("/add <имя> <Заголовок>")
        name, title = args[0], (args[1] if len(args) > 1 else args[0])
        token = self.store.add_agent(name, title)
        self.hub._system("hub", "system", f"Новый агент: {title} ({name}).")
        url = self.cfg.get("public_url") or "<адрес хаба>"
        return (f"Агент {name} создан. Токен (показываю один раз):\n{token}\n\n"
                f"На машине агента ~/.config/agent-hub.json:\n"
                f'{{"url": "{url}", "token": "{token}"}}\n\n'
                f"Свой бот: /bot {name} <токен от @BotFather>")

    def admin_bot(self, args, m):
        if len(args) < 2:
            raise ValueError("/bot <имя> <токен бота>")
        name, token = args[0], args[1].strip()
        try:  # сообщение с токеном — сразу удалить из лички
            self.call(self.listener_token(), "deleteMessage", chat_id=m["chat"]["id"], message_id=m["message_id"])
        except (TgError, httpx.HTTPError):
            pass
        if not self.store.agent(name):
            raise ValueError(f"нет агента {name}")
        me = self.call(token, "getMe")
        self.store.update_agent(name, bot_token=token)
        return (f"Бот @{me.get('username')} привязан к {name}. Добавьте его в группу; сообщение с токеном удалено.")

    def admin_set(self, field):
        def f(args, m):
            if len(args) < 2:
                raise ValueError(f"/{field} <имя> <текст>")
            self.store.update_agent(args[0], **{field: args[1]})
            return f"{args[0]}: {field} = {args[1]}"
        return f

    def admin_toggle(self, disabled: bool):
        def f(args, m):
            if not args:
                raise ValueError("укажите имя агента")
            self.store.update_agent(args[0], disabled=int(disabled))
            return f"{args[0]}: {'выключен — его токен больше не принимается' if disabled else 'включён'}"
        return f

    def admin_token(self, args, m):
        if not args:
            raise ValueError("/token <имя>")
        return f"Новый токен {args[0]} (старый больше не работает):\n{self.store.rotate_token(args[0])}"

    def admin_counter(self, args, m):
        if len(args) < 2 or not args[1].isdigit():
            raise ValueError("/counter <имя> <число>")
        self.store.set_counter(args[0], int(args[1]))
        return f"Счётчик {args[0]} = {args[1]}; следующий hub next {args[0]} даст {int(args[1]) + 1}."

    def admin_release(self, args, m):
        if not args:
            raise ValueError("/release <id захвата>")
        cl = self.hub.release("admin", args[0], force=True)
        return f"Захват #{cl['id']} «{cl['topic']}» снят."

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


ADMIN_COMMANDS = {
    "/agents": Telegram.admin_agents, "/add": Telegram.admin_add, "/bot": Telegram.admin_bot,
    "/role": lambda self, a, m: Telegram.admin_set(self, "role")(a, m),
    "/title": lambda self, a, m: Telegram.admin_set(self, "title")(a, m),
    "/disable": lambda self, a, m: Telegram.admin_toggle(self, True)(a, m),
    "/enable": lambda self, a, m: Telegram.admin_toggle(self, False)(a, m),
    "/token": Telegram.admin_token, "/counter": Telegram.admin_counter, "/release": Telegram.admin_release,
    "/status": lambda self, a, m: self.hub.status_text(),
    "/ideas": lambda self, a, m: self.ideas_text(),
    "/digest": lambda self, a, m: self.hub.digest_text(time.time() - 86400) or "За сутки сообщений не было.",
    "/help": lambda self, a, m: ADMIN_HELP, "/start": lambda self, a, m: ADMIN_HELP,
}


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
