"""Управление хабом из Telegram: меню на кнопках в личке бота-слушателя (только админам) и кнопки в группе.

Админ входит один раз: /admin <код из установки>. Дальше /start или /menu — меню; значения (имя агента, роль, токен
бота) бот спрашивает сообщением и ждёт ответ. Текстовые команды (/add, /role...) тоже работают.
"""
import hmac
import time

from .core import HubError

HELP = """Меню — /menu. Те же действия текстом:
/add <имя> <Заголовок> · /bot <имя> <токен> · /role <имя> <текст> · /title <имя> <текст>
/disable <имя> · /enable <имя> · /token <имя> · /counter <имя> <число> · /release <id захвата>
/status · /ideas · /digest · /agents · /cancel — отменить ввод"""
ASK = {
    "add": "Пришлите имя агента латиницей и подпись в чате через пробел.\nНапример: gpt GPT · модели",
    "role": "Пришлите роль (направление) агента {name}.\nНапример: валидация и утечки",
    "title": "Пришлите новую подпись агента {name} в чате.",
    "bot": "Пришлите токен бота для {name} (выдаёт @BotFather). Сообщение с токеном я сразу удалю.",
    "counter": "Пришлите имя счётчика и число через пробел.\nНапример: exp 29 — следующий номер будет 30",
}


def kb(*rows) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows if row]}


MAIN_KB = kb([("👥 Агенты", "m:agents"), ("➕ Добавить агента", "m:add")],
             [("📊 Статус", "m:status"), ("💡 Идеи", "m:ideas")],
             [("📰 Сводка за сутки", "m:digest"), ("🔢 Счётчики", "m:counters")],
             [("💬 Группа", "m:group"), ("🛡 Доверенные", "m:trust")])
GROUP_KB = kb([("📊 Статус", "g:status"), ("💡 Идеи", "g:ideas"), ("📰 Сводка", "g:digest")])
BACK = [("« Меню", "m:main")]


class AdminUI:
    def __init__(self, tg):
        self.tg, self.store, self.hub = tg, tg.store, tg.hub
        self.pending: dict[int, tuple] = {}  # ожидание ввода: tg user -> (действие, агент)

    # ---------- отправка ----------
    def send(self, chat: int, text: str, markup: dict | None = None):
        params = {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}
        if markup:
            params["reply_markup"] = markup
        return self.tg.call(self.tg.listener_token(), "sendMessage", **params)

    def edit(self, chat: int, mid: int, text: str, markup: dict | None = None):
        params = {"chat_id": chat, "message_id": mid, "text": text[:4000], "disable_web_page_preview": True,
                  "reply_markup": markup or {"inline_keyboard": []}}
        try:
            self.tg.call(self.tg.listener_token(), "editMessageText", **params)
        except Exception as e:  # noqa: BLE001  (старое сообщение не редактируется — пришлём новое)
            if "not modified" not in str(e):
                self.send(chat, text, markup)

    # ---------- экраны ----------
    def screen_main(self):
        n = len(self.store.agents(with_disabled=False))
        warn = "" if self.tg.chat_id else "\n\n⚠️ Группа не выбрана: добавьте бота в группу и нажмите «💬 Группа»."
        return f"Хаб агентов: активных агентов {n}. Что делаем?{warn}", MAIN_KB

    def screen_group(self):
        cur, seen = self.tg.chat_id, self.tg.seen_groups()
        text = (f"Группа хаба: {cur}" if cur else "Группа хаба не выбрана.") + "\n\n"
        if seen:
            text += "Бот видел эти группы — выберите, где будет работать хаб:"
        else:
            text += ("Добавьте бота в группу (лучше администратором) и напишите там любое сообщение, "
                     "затем нажмите «Обновить».")
        rows = [[(f"💬 {title or gid}", f"grp:{gid}")] for gid, title in seen.items()]
        return text, kb(*rows, [("🔄 Обновить", "m:group")], BACK)

    def screen_agents(self):
        act, now = self.store.last_activity(), time.time()
        rows, lines = [], []
        for a in self.store.agents():
            t = act.get(a["name"])
            seen = "ещё не писал" if t is None else f"{_ago(now - t)} назад"
            mark = "🚫" if a["disabled"] else ("🤖" if a["bot_token"] else "•")
            lines.append(f"{mark} {a['title']} ({a['name']}) — {seen}" + (f"\n    роль: {a['role']}" if a["role"] else ""))
            rows.append([(f"{mark} {a['title']}", f"a:{a['name']}")])
        text = "Агенты (🤖 — свой бот, 🚫 — выключен):\n\n" + ("\n".join(lines) or "пока нет")
        return text, kb(*rows, [("➕ Добавить агента", "m:add")], BACK)

    def screen_agent(self, name: str):
        a = self.store.agent(name)
        if not a:
            return "Агент не найден.", kb(BACK)
        t = self.store.last_activity().get(name)
        text = (f"{a['title']} ({name})\n"
                f"Роль: {a['role'] or '—'}\n"
                f"Бот: {'привязан' if a['bot_token'] else 'нет (пишет через бота хаба)'}\n"
                f"Состояние: {'выключен' if a['disabled'] else 'активен'}\n"
                f"Последнее сообщение: {'—' if t is None else _ago(time.time() - t) + ' назад'}")
        if name == self.tg.listener_name():  # бот хаба не выключается: через него идёт весь Telegram
            text += "\n\n🔒 Это бот хаба — через него работают меню и группа, поэтому выключить его нельзя."
            toggle = None
        else:
            toggle = [("✅ Включить", f"a:{name}:on") if a["disabled"] else ("⛔ Выключить", f"a:{name}:off")]
        return text, kb([("🎭 Роль", f"a:{name}:role"), ("🏷 Подпись", f"a:{name}:title")],
                        [("🤖 Бот", f"a:{name}:bot"), ("🔑 Новый токен", f"a:{name}:tok")],
                        toggle, [("« Агенты", "m:agents")])

    def screen_trust(self):
        """Кто может давать агентам указания. Остальные люди в группе видят ленту, но агенты им не подчиняются."""
        admins = {a["tg_user"] for a in self.store.admins()}
        trusted = {t["tg_user"]: t["name"] for t in self.store.trusted()}
        lines = ["🛡 Указания агентам дают только доверенные. Сообщения остальных агенты видят с пометкой ⚠️, "
                 "не выполняют и на них не просыпаются.", "", "Админы (доверенные всегда):"]
        lines += [f"• {a['name']}" for a in self.store.admins()] or ["—"]
        rows = []
        lines += ["", "Доверенные:"] + ([f"• {n}" for n in trusted.values()] or ["—"])
        rows += [[(f"❌ {n}", f"tr:-{uid}")] for uid, n in trusted.items()]
        others = [p for p in self.store.people() if p["tg_user"] not in admins and p["tg_user"] not in trusted]
        if others:
            lines += ["", "Писали в группе, не доверенные — нажмите, чтобы доверять:"]
            rows += [[(f"✅ {p['name']}", f"tr:+{p['tg_user']}")] for p in others[:10]]
        return "\n".join(lines), kb(*rows, BACK)

    def screen_counters(self):
        c = self.store.counters()
        text = "Общие счётчики (агенты берут следующий номер: hub next <имя>):\n\n" + (
            "\n".join(f"• {k} = {v}" for k, v in c.items()) or "пока нет")
        return text, kb([("✏️ Задать счётчик", "m:counter")], BACK)

    def info_screen(self, what: str):
        if what == "status":
            return self.hub.status_text()
        if what == "ideas":
            return self.tg.ideas_text()
        return self.hub.digest_text(time.time() - 86400) or "За сутки сообщений не было."

    # ---------- входящие ----------
    def handle_message(self, m: dict, u: dict, text: str):
        uid, chat = u.get("id"), m["chat"]["id"]
        parts = text.split(maxsplit=2)
        cmd = parts[0].split("@")[0].lower() if text.startswith("/") else ""
        if cmd == "/admin":
            code = self.tg.cfg.get("admin_code")
            if code and len(parts) > 1 and hmac.compare_digest(parts[1], code):
                self.store.add_admin(uid, u.get("username") or u.get("first_name") or str(uid))
                return self.send(chat, "Готово, вы админ хаба.\n\n" + self.screen_main()[0], MAIN_KB)
            return self.send(chat, "Неверный код. Код печатает установка хаба (setup.sh) в конце.")
        if not self.store.is_admin(uid):
            return self.send(chat, "Это бот хаба агентов. Чтобы управлять им, пришлите /admin <код из установки>.")
        if cmd == "/cancel":
            self.pending.pop(uid, None)
            return self.send(chat, "Отменено.", MAIN_KB)
        if cmd in ("/start", "/menu", "/help"):
            self.pending.pop(uid, None)
            text_, markup = self.screen_main()
            return self.send(chat, (HELP + "\n\n" if cmd == "/help" else "") + text_, markup)
        if cmd in TEXT_COMMANDS:
            self.pending.pop(uid, None)
            return self.run(chat, lambda: TEXT_COMMANDS[cmd](self, parts[1:], m))
        if uid in self.pending and not cmd:
            action, name = self.pending.pop(uid)
            return self.run(chat, lambda: self.apply_input(action, name, text, m))
        self.send(chat, *self.screen_main())

    def handle_callback(self, q: dict):
        uid, data = q["from"]["id"], q.get("data", "")
        msg = q.get("message") or {}
        chat, mid = msg.get("chat", {}).get("id"), msg.get("message_id")
        try:
            self.tg.call(self.tg.listener_token(), "answerCallbackQuery", callback_query_id=q["id"])
        except Exception:  # noqa: BLE001
            pass
        if data.startswith("g:"):  # кнопки в группе — для всех, без секретов
            if chat == self.tg.chat_id:
                self.send(chat, self.info_screen(data[2:]), GROUP_KB)
            return
        if msg.get("chat", {}).get("type") != "private" or not self.store.is_admin(uid):
            return
        self.pending.pop(uid, None)
        kind, _, rest = data.partition(":")
        if kind == "m":
            if rest in ("main", "agents", "counters", "group", "trust"):
                return self.edit(chat, mid, *getattr(self, f"screen_{rest}")())
            if rest in ("status", "ideas", "digest"):
                return self.edit(chat, mid, self.info_screen(rest), kb(BACK))
            if rest in ("add", "counter"):
                self.pending[uid] = (rest, None)
                return self.send(chat, ASK[rest] + "\n\n/cancel — отмена")
        if kind == "tr":
            uid, on = int(rest[1:]), rest[0] == "+"
            person = next((p for p in self.store.people(1000) if p["tg_user"] == uid), None)
            name = person["name"] if person else str(uid)
            self.store.set_trusted(uid, name, on)
            return self.edit(chat, mid, *self.screen_trust())
        if kind == "grp":
            gid = int(rest)
            title = self.tg.seen_groups().get(gid, "")
            self.tg.set_chat(gid)
            self.send(gid, "Хаб агентов подключён к этой группе. /menu — кнопки, /status — кто чем занят.", GROUP_KB)
            return self.edit(chat, mid, f"Готово: хаб работает в группе «{title or gid}».", kb(BACK))
        if kind == "a":
            name, _, act = rest.partition(":")
            if not act:
                return self.edit(chat, mid, *self.screen_agent(name))
            if act in ("role", "title", "bot"):
                self.pending[uid] = (act, name)
                return self.send(chat, ASK[act].format(name=name) + "\n\n/cancel — отмена")
            if act == "tok":
                return self.edit(chat, mid, f"Выпустить новый токен для {name}? Старый сразу перестанет работать — "
                                            f"агенту нужно будет обновить ~/.config/agent-hub.json.",
                                 kb([("🔑 Да, выпустить", f"a:{name}:tok!"), ("Отмена", f"a:{name}")]))
            if act == "tok!":
                token = self.store.rotate_token(name)
                self.send(chat, f"Новый токен {name} (показываю один раз):\n{token}\n\n" + self.client_config(token))
                return self.edit(chat, mid, *self.screen_agent(name))
            if act in ("on", "off"):
                if act == "off" and name == self.tg.listener_name():
                    return self.edit(chat, mid, *self.screen_agent(name))
                self.store.update_agent(name, disabled=int(act == "off"))
                return self.edit(chat, mid, *self.screen_agent(name))

    def run(self, chat: int, fn):
        try:
            out = fn()
        except HubError as e:
            out = f"Ошибка: {e.message}"
        except ValueError as e:
            out = f"Ошибка: {e}"
        except Exception as e:  # noqa: BLE001  (ошибка Telegram и т.п. — сообщить админу, а не уронить приём)
            out = f"Не получилось: {e}"
        if isinstance(out, tuple):
            self.send(chat, *out)
        elif out:
            self.send(chat, out, kb(BACK))

    def client_config(self, token: str) -> str:
        url = self.tg.cfg.get("public_url") or "<адрес хаба>"
        return f'Файл ~/.config/agent-hub.json на машине агента:\n{{"url": "{url}", "token": "{token}"}}'

    # ---------- действия ----------
    def apply_input(self, action: str, name: str | None, text: str, m: dict):
        if action == "add":
            parts = text.split(maxsplit=1)
            return self.add_agent(parts[0].lower(), parts[1] if len(parts) > 1 else parts[0])
        if action == "counter":
            parts = text.split()
            return self.set_counter(parts)
        if action == "bot":
            return self.bind_bot(name, text.strip(), m)
        self.store.update_agent(name, **{action: text.strip()})
        return self.screen_agent(name)

    def add_agent(self, name: str, title: str):
        token = self.store.add_agent(name, title)
        self.hub._system("hub", "system", f"Новый агент: {title} ({name}).")
        return (f"Агент {name} создан. Токен (показываю один раз):\n{token}\n\n{self.client_config(token)}",
                kb([("🤖 Привязать бота", f"a:{name}:bot"), ("🎭 Задать роль", f"a:{name}:role")],
                   [("« Агенты", "m:agents")]))

    def bind_bot(self, name: str, token: str, m: dict):
        try:  # сообщение с токеном — сразу удалить из лички
            self.tg.call(self.tg.listener_token(), "deleteMessage", chat_id=m["chat"]["id"], message_id=m["message_id"])
        except Exception:  # noqa: BLE001
            pass
        if not self.store.agent(name):
            raise ValueError(f"нет агента {name}")
        try:
            me = self.tg.call(token, "getMe")
        except Exception as e:  # noqa: BLE001  (TgError, сеть)
            raise ValueError(f"Telegram не принял токен ({e})") from e
        self.store.update_agent(name, bot_token=token)
        text, markup = self.screen_agent(name)
        return f"Бот @{me.get('username')} привязан, сообщение с токеном удалено. Добавьте бота в группу.\n\n" + text, markup

    def set_counter(self, parts: list):
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError("нужно имя и число, например: exp 29")
        self.store.set_counter(parts[0], int(parts[1]))
        return self.screen_counters()

    # ---------- текстовые команды ----------
    def cmd_set(field):
        def f(self, args, m):
            if len(args) < 2:
                raise ValueError(f"/{field} <имя> <текст>")
            self.store.update_agent(args[0], **{field: args[1]})
            return self.screen_agent(args[0])
        return f

    def cmd_toggle(disabled):
        def f(self, args, m):
            if not args:
                raise ValueError("укажите имя агента")
            if disabled and args[0] == self.tg.listener_name():
                raise ValueError("это бот хаба — через него работают меню и группа, выключить его нельзя")
            self.store.update_agent(args[0], disabled=int(disabled))
            return self.screen_agent(args[0])
        return f

    def cmd_token(self, args, m):
        if not args:
            raise ValueError("/token <имя>")
        token = self.store.rotate_token(args[0])
        return f"Новый токен {args[0]} (старый больше не работает):\n{token}\n\n" + self.client_config(token)

    def cmd_add(self, args, m):
        if not args:
            raise ValueError("/add <имя> <Заголовок>")
        return self.add_agent(args[0].lower(), args[1] if len(args) > 1 else args[0])

    def cmd_bot(self, args, m):
        if len(args) < 2:
            raise ValueError("/bot <имя> <токен бота>")
        return self.bind_bot(args[0], args[1], m)

    def cmd_release(self, args, m):
        if not args:
            raise ValueError("/release <id захвата>")
        cl = self.hub.release("admin", args[0], force=True)
        return f"Захват #{cl['id']} «{cl['topic']}» снят."


TEXT_COMMANDS = {
    "/agents": lambda self, a, m: self.screen_agents(),
    "/add": AdminUI.cmd_add, "/bot": AdminUI.cmd_bot,
    "/role": AdminUI.cmd_set("role"), "/title": AdminUI.cmd_set("title"),
    "/disable": AdminUI.cmd_toggle(True), "/enable": AdminUI.cmd_toggle(False),
    "/token": AdminUI.cmd_token, "/release": AdminUI.cmd_release,
    "/counter": lambda self, a, m: self.set_counter(" ".join(a).split()),
    "/status": lambda self, a, m: self.info_screen("status"),
    "/ideas": lambda self, a, m: self.info_screen("ideas"),
    "/digest": lambda self, a, m: self.info_screen("digest"),
}


def _ago(sec: float) -> str:
    if sec < 3600:
        return f"{max(1, int(sec // 60))} мин"
    if sec < 2 * 86400:
        return f"{sec / 3600:.0f} ч"
    return f"{sec / 86400:.0f} дн"
