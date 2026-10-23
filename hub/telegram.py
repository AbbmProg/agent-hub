"""Зеркало ленты в Telegram: сообщения агентов публикуются от имени их ботов, сообщения людей из группы — в ленту.

Боты не получают историю чата, только новые сообщения (Telegram хранит недоставленные ~24 ч), поэтому память — лента
хаба, а Telegram — витрина и канал для людей.
"""
import logging
import threading
import time

import httpx

log = logging.getLogger("agent-hub.telegram")

KIND_LABEL = {
    "report": ("📊", "отчёт"), "finding": ("🔎", "находка"), "idea": ("💡", "идея"), "question": ("❓", "вопрос"),
    "answer": ("💬", "ответ"), "note": ("📝", "заметка"), "claim": ("📌", "беру в работу"),
    "release": ("✅", "освободил"), "system": ("⚙️", "хаб"),
}
LIMIT = 4000  # у Telegram 4096 символов на сообщение


def render(msg: dict, reply_tg: int | None = None) -> str:
    icon, label = KIND_LABEL.get(msg["kind"], ("•", msg["kind"]))
    lines = [f"{icon} {msg['author']} · {label} · #{msg['id']}"]
    if msg.get("reply_to") and not reply_tg:
        lines[0] += f" (ответ на #{msg['reply_to']})"
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
    def __init__(self, cfg: dict, store, transport: httpx.BaseTransport | None = None):
        self.cfg, self.store = cfg, store
        self.chat_id = cfg.get("chat_id")
        proxy = cfg.get("tg_proxy") or None
        self.http = httpx.Client(timeout=httpx.Timeout(40.0, connect=15.0), proxy=proxy, transport=transport)
        self.base = (cfg.get("tg_api") or "https://api.telegram.org").rstrip("/")
        self.stop = threading.Event()
        self.listener = cfg.get("listener") or next((n for n, a in cfg["agents"].items() if a.get("bot_token")), None)

    @property
    def enabled(self) -> bool:
        return bool(self.chat_id) and any(a.get("bot_token") for a in self.cfg["agents"].values())

    def call(self, token: str, method: str, **params) -> dict:
        r = self.http.post(f"{self.base}/bot{token}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            retry = (data.get("parameters") or {}).get("retry_after")
            raise TgError(data.get("description", r.text[:200]), r.status_code, retry)
        return data["result"]

    def token_for(self, msg: dict) -> str | None:
        a = self.cfg["agents"].get(msg["agent"]) or {}
        if a.get("bot_token"):
            return a["bot_token"]
        lst = self.cfg["agents"].get(self.listener) or {}  # системные и агенты без бота — через бота-слушателя
        return lst.get("bot_token")

    # ---------- отправка ----------
    def send(self, msg: dict) -> int | None:
        token = self.token_for(msg)
        if not token:
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
                if msg["agent"] == "human":
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
                    elif 400 <= e.status < 500 and e.status != 429:
                        log.error("сообщение #%s не принято Telegram: %s", msg["id"], e)
                        self.store.set_tg(msg["id"], "failed")
                    else:
                        self.stop.wait(backoff)
                        backoff = min(backoff * 2, 60)
                    break
                except (httpx.HTTPError, ValueError) as e:  # сеть: сообщение остаётся в очереди
                    log.warning("Telegram недоступен (%r), повтор через %.0f с", e, backoff)
                    self.stop.wait(backoff)
                    backoff = min(backoff * 2, 60)
                    break

    # ---------- приём сообщений людей ----------
    def poll_once(self, token: str, timeout: int = 25) -> int:
        offset = int(self.store.kv_get("tg_offset", 0))
        ups = self.call(token, "getUpdates", offset=offset, timeout=timeout, allowed_updates=["message"])
        n = 0
        for up in ups:
            self.store.kv_set("tg_offset", up["update_id"] + 1)
            m = up.get("message") or {}
            if m.get("chat", {}).get("id") != self.chat_id or (m.get("from") or {}).get("is_bot"):
                continue
            text = m.get("text") or m.get("caption")
            if not text:
                continue
            u = m.get("from") or {}
            name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or u.get("username") or "человек"
            reply = m.get("reply_to_message") or {}
            parent = self.store.by_tg_msg(reply["message_id"]) if reply.get("message_id") else None
            self.store.add_message("human", name, "human", text, reply_to=parent["id"] if parent else None,
                                   tg_state="skip", tg_msg=m.get("message_id"))
            n += 1
        return n

    def poller_loop(self):
        token = (self.cfg["agents"].get(self.listener) or {}).get("bot_token")
        if not token:
            return
        backoff = 1.0
        while not self.stop.is_set():
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
        if not self.enabled:
            log.warning("Telegram выключен: нет chat_id или ни у одного агента нет bot_token")
            return
        for target in (self.sender_loop, self.poller_loop):
            threading.Thread(target=target, daemon=True, name=target.__name__).start()


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
