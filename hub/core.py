"""Логика хаба поверх хранилища: публикация, захваты, очередь идей, перекрёстная проверка отчётов, статус и сводка.

Общая для HTTP API (агенты) и Telegram (люди, админ).
"""
import threading
import time

from .store import Store

AGENT_KINDS = {"report", "finding", "idea", "question", "answer", "note"}
VERDICTS = {"confirm": "подтверждаю", "refute": "опровергаю"}
STALE_HOURS = 3  # агент молчит дольше — в статусе помечается


class HubError(Exception):
    def __init__(self, status: int, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra or {}


class Hub:
    def __init__(self, cfg: dict, store: Store):
        self.cfg, self.store = cfg, store
        store.seed_agents(cfg.get("agents") or {})
        for uid in cfg.get("admins") or []:
            if not store.is_admin(int(uid)):
                store.add_admin(int(uid), "из конфига")
        self.stop = threading.Event()

    def title(self, agent: str) -> str:
        if agent == "hub":
            return "Хаб"
        a = self.store.agent(agent)
        return a["title"] if a else agent

    def _system(self, agent: str, kind: str, text: str, reply_to=None, status=None) -> int:
        return self.store.add_message(agent, self.title(agent), kind, text, reply_to=reply_to, status=status)

    # ---------- лента ----------
    def post(self, agent: str, kind: str, text: str, refs=(), reply_to=None) -> int:
        kinds = AGENT_KINDS | ({"system"} if agent == "hub" else set())
        if kind not in kinds:
            raise HubError(422, f"вид сообщения — один из: {', '.join(sorted(kinds))}")
        if reply_to is not None and self.store.get(reply_to) is None:
            raise HubError(404, f"нет сообщения #{reply_to}")
        if kind == "report" and not refs:
            raise HubError(422, "отчёт без --ref (коммит, метка прогона) проверить нельзя — укажите ссылку "
                                "или опубликуйте как note")
        return self.store.add_message(agent, self.title(agent), kind, text, refs, reply_to)

    # ---------- адресация и ожидание ----------
    def mentioned(self, text: str) -> set:
        """Агенты, упомянутые в тексте как @username их бота, @имя или @подпись."""
        low = text.lower()
        return {a["name"] for a in self.store.agents(with_disabled=False)
                if f"@{a['name'].lower()}" in low or f"@{a['title'].lower()}" in low
                or (a.get("bot_username") and f"@{a['bot_username'].lower()}" in low)}

    def addressed(self, agent: str, m: dict) -> bool:
        """Сообщение требует реакции агента: ответ на его сообщение, упоминание или общее сообщение человека."""
        if m["agent"] == agent:
            return False
        if m["agent"] == "human" and m.get("status") != "trusted":
            return False  # посторонний в группе агентов не будит и не командует ими
        parent = self.store.get(m["reply_to"]) if m.get("reply_to") else None
        if parent and parent["agent"] == agent:
            return True
        names = self.mentioned(m["text"])
        if agent in names:
            return True
        # человек пишет всем: не реплай другому агенту и без упоминаний
        return m["agent"] == "human" and not names and not (parent and parent["agent"] not in ("human", "hub"))

    def wait(self, agent: str, timeout: float = 50, everything: bool = False) -> list[dict]:
        """Ждать до timeout секунд непрочитанного, адресованного агенту (или любого, если everything). Не отмечает
        прочитанным — агент потом делает hub read."""
        end = time.time() + timeout
        while True:
            items, _ = self.store.inbox(agent, limit=200, peek=True)
            hit = [m for m in items if everything or self.addressed(agent, m)]
            if hit or time.time() >= end or self.stop.is_set():
                return hit
            self.stop.wait(1.0)

    # ---------- захваты ----------
    def claim(self, agent: str, topic: str, note: str = "", ttl_hours: float = 12, idea: int | None = None) -> dict:
        cl, other = self.store.claim(agent, topic, note, ttl_hours, idea)
        if other:
            raise HubError(409, f"тема уже занята: {self.title(other['agent'])} (захват #{other['id']})",
                           {"claim": other})
        if not cl["renewed"]:  # продление своего захвата в чат не пишем
            self._system(agent, "claim", cl["topic"] + (f"\n{note}" if note else ""), reply_to=idea)
        return cl

    def release(self, agent: str, key: str, force=False) -> dict:
        cl = self.store.release(agent, key, force)
        if cl is None:
            raise HubError(404, "активного захвата с таким id/темой у вас нет")
        self._system(cl["agent"], "release", cl["topic"], reply_to=cl.get("idea"))
        return cl

    # ---------- очередь идей ----------
    def ideas(self, status: str | None = "open") -> list[dict]:
        return self.store.messages(kind="idea", status=status, limit=500)

    def take_idea(self, agent: str, idea_id: int, note: str = "", ttl_hours: float = 12) -> dict:
        idea = self.store.get(idea_id)
        if not idea or idea["kind"] != "idea":
            raise HubError(404, f"#{idea_id} — не идея")
        if idea["status"] == "taken" and idea["owner"] != agent:
            raise HubError(409, f"идею #{idea_id} уже взял {self.title(idea['owner'])}")
        if idea["status"] in ("done", "dropped"):
            raise HubError(409, f"идея #{idea_id} уже закрыта ({idea['status']})")
        first = idea["text"].strip().splitlines()[0][:90]
        cl = self.claim(agent, f"идея #{idea_id}: {first}", note, ttl_hours, idea=idea_id)
        self.store.set_status(idea_id, "taken", agent)
        return cl

    def close_idea(self, agent: str, idea_id: int, outcome: str, text: str = "", admin=False) -> dict:
        idea = self.store.get(idea_id)
        if not idea or idea["kind"] != "idea":
            raise HubError(404, f"#{idea_id} — не идея")
        if outcome not in ("done", "dropped"):
            raise HubError(422, "исход: done или dropped")
        if not admin and agent not in (idea["owner"], idea["agent"]):
            raise HubError(403, "закрыть идею может её автор, тот, кто её взял, или админ")
        self.store.set_status(idea_id, outcome)
        for cl in self.store.active_claims():
            if cl["idea"] == idea_id:
                self.store.release(cl["agent"], str(cl["id"]), force=True)
        label = "сделано" if outcome == "done" else "отказ"
        self._system(agent, "note", f"Идея #{idea_id} закрыта: {label}." + (f"\n{text}" if text else ""),
                     reply_to=idea_id)
        return self.store.get(idea_id)

    # ---------- перекрёстная проверка ----------
    def review(self, agent: str, report_id: int, verdict: str, text: str, refs=()) -> dict:
        rep = self.store.get(report_id)
        if not rep or rep["kind"] != "report":
            raise HubError(404, f"#{report_id} — не отчёт")
        if rep["agent"] == agent:
            raise HubError(403, "свой отчёт проверяет другой агент")
        if verdict not in VERDICTS:
            raise HubError(422, "вердикт: confirm или refute")
        self.store.add_message(agent, self.title(agent), "review", text, refs, reply_to=report_id, status=verdict)
        self.store.set_status(report_id, self.store.review_status(report_id))
        return self.store.get(report_id)

    # ---------- статус и сводка ----------
    def status_text(self) -> str:
        now, act = time.time(), self.store.last_activity()
        L = ["Агенты:"]
        for a in self.store.agents(with_disabled=False):
            t = act.get(a["name"])
            ago = "ещё не писал" if t is None else f"{_ago(now - t)} назад"
            stale = " ⚠️ молчит" if t is not None and now - t > STALE_HOURS * 3600 else ""
            L.append(f"• {a['title']} ({a['name']}){': ' + a['role'] if a['role'] else ''} — {ago}{stale}")
        cl = self.store.active_claims()
        L += ["", f"В работе ({len(cl)}):"] + [f"• {self.title(c['agent'])}: {c['topic']}" for c in cl] or []
        ideas = self.ideas("open")
        L += ["", f"Свободные идеи ({len(ideas)}):"] + [f"• #{i['id']} {_first(i['text'])} — {i['author']}"
                                                       for i in ideas[-8:]]
        un = self.store.unreviewed()
        L += ["", f"Ждут проверки ({len(un)}):"] + [f"• #{r['id']} {_first(r['text'])} — {r['author']}"
                                                   + (" ❌ оспорен" if r["status"] == "disputed" else "")
                                                   for r in un[-8:]]
        return "\n".join(L)

    def digest_text(self, since: float) -> str | None:
        msgs = [m for m in self.store.messages(limit=100000) if m["ts"] >= since]
        if not msgs:
            return None
        by = lambda k: [m for m in msgs if m["kind"] == k]  # noqa: E731
        mark = {"confirmed": "✔️", "disputed": "❌", "unreviewed": "…"}
        L = [f"Сводка за {_ago(time.time() - since)}: сообщений {len(msgs)}, отчётов {len(by('report'))}, "
             f"находок {len(by('finding'))}, идей {len(by('idea'))}, проверок {len(by('review'))}."]
        reps = by("report")
        if reps:
            L += ["", "Отчёты (✔️ подтверждён, ❌ оспорен, … ждёт проверки):"]
            L += [f"{mark.get(self.store.get(r['id'])['status'], '…')} #{r['id']} {r['author']}: {_first(r['text'])}"
                  for r in reps]
        if by("finding"):
            L += ["", "Находки:"] + [f"• #{f['id']} {f['author']}: {_first(f['text'])}" for f in by("finding")]
        open_ideas = self.ideas("open")
        if open_ideas:
            L += ["", f"Свободных идей: {len(open_ideas)} — `hub ideas`."]
        return "\n".join(L)

    def digest_loop(self):
        """Ежедневная сводка в чат в digest_hour (UTC); None — выключено."""
        hour = self.cfg.get("digest_hour", 18)
        if hour is None:
            return
        while not self.stop.wait(60):
            now = time.gmtime()
            day = time.strftime("%Y-%m-%d", now)
            if now.tm_hour == int(hour) and self.store.kv_get("digest_day") != day:
                self.store.kv_set("digest_day", day)
                text = self.digest_text(time.time() - 24 * 3600)
                if text:
                    self._system("hub", "system", text)

    def start(self):
        threading.Thread(target=self.digest_loop, daemon=True, name="digest").start()


def _first(text: str, n: int = 90) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= n else line[: n - 1] + "…"


def _ago(sec: float) -> str:
    if sec < 3600:
        return f"{max(1, int(sec // 60))} мин"
    if sec < 2 * 86400:
        return f"{sec / 3600:.0f} ч"
    return f"{sec / 86400:.0f} дн"
