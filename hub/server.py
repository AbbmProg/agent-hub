"""HTTP API хаба. Запуск: AGENT_HUB_CONFIG=/etc/agent-hub/config.json uvicorn hub.server:app

Авторизация: `Authorization: Bearer <токен агента>`; админ-токен из конфига — для служебных операций.
"""
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .core import Hub, HubError
from .store import Store
from .telegram import Telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "httpcore"):  # httpx пишет адрес запроса, а в нём токен бота — в журнал не пускаем
    logging.getLogger(noisy).setLevel(logging.WARNING)


def load_config(path: str | None = None) -> dict:
    path = path or os.environ.get("AGENT_HUB_CONFIG", "/etc/agent-hub/config.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class PostIn(BaseModel):
    kind: str
    text: str = Field(min_length=1, max_length=16000)
    refs: list[str] = Field(default_factory=list, max_length=20)
    reply_to: int | None = None


class ClaimIn(BaseModel):
    topic: str = Field(min_length=2, max_length=300)
    note: str = Field(default="", max_length=2000)
    ttl_hours: float = Field(default=12, gt=0, le=24 * 14)


class TakeIn(BaseModel):
    note: str = Field(default="", max_length=2000)
    ttl_hours: float = Field(default=12, gt=0, le=24 * 14)


class CloseIn(BaseModel):
    outcome: str
    text: str = Field(default="", max_length=8000)


class ReviewIn(BaseModel):
    verdict: str
    text: str = Field(min_length=1, max_length=16000)
    refs: list[str] = Field(default_factory=list, max_length=20)


class BusyIn(BaseModel):
    text: str = Field(default="думает…", max_length=200)
    ttl_minutes: float = Field(default=15, gt=0, le=240)


class CounterIn(BaseModel):
    value: int = Field(ge=0)


class AgentIn(BaseModel):
    name: str
    title: str = ""
    role: str = ""


def create_app(cfg: dict | None = None, transport=None) -> FastAPI:
    st: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        c = cfg if cfg is not None else load_config()
        store = Store(c.get("db", "/var/lib/agent-hub/hub.db"))
        hub = Hub(c, store)
        tg = Telegram(c, hub, transport=transport)
        st.update(cfg=c, store=store, hub=hub, tg=tg)
        hub.start()
        tg.start()
        yield
        hub.stop.set()
        tg.stop.set()

    app = FastAPI(title="Agent Hub", lifespan=lifespan)

    @app.exception_handler(HubError)
    async def hub_error(_: Request, e: HubError):
        return JSONResponse({"detail": {"message": e.message, **e.extra}}, status_code=e.status)

    def who(authorization: str = Header(default="")) -> str:
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            raise HTTPException(401, "нет токена")
        adm = st["cfg"].get("admin_token")
        if adm and secrets.compare_digest(token, adm):
            return "admin"
        a = st["store"].agent_by_token(token)
        if a is None:
            raise HTTPException(401, "неверный токен или агент выключен")
        return a["name"]

    def admin(agent: str = Depends(who)) -> str:
        if agent != "admin":
            raise HTTPException(403, "нужен админ-токен")
        return agent

    def agent_only(agent: str = Depends(who)) -> str:
        if agent == "admin":
            raise HTTPException(403, "это действие агента, а не админа")
        return agent

    @app.get("/health")
    def health():
        return {"ok": True, "telegram": st["tg"].enabled,
                "agents": [a["name"] for a in st["store"].agents(with_disabled=False)]}

    @app.get("/me")
    def me(agent: str = Depends(who)):
        a = st["store"].agent(agent) or {}
        return {"agent": agent, "title": a.get("title", agent), "role": a.get("role", "")}

    @app.get("/agents")
    def agents(_: str = Depends(who)):
        return [{"agent": a["name"], "title": a["title"], "role": a["role"], "bot": bool(a["bot_token"]),
                 "disabled": bool(a["disabled"])} for a in st["store"].agents()]

    # ---------- лента ----------
    @app.post("/messages", status_code=201)
    def post(m: PostIn, agent: str = Depends(who)):
        return {"id": st["hub"].post("hub" if agent == "admin" else agent, m.kind, m.text, m.refs, m.reply_to)}

    @app.get("/messages")
    def messages(after: int = 0, limit: int = Query(100, le=1000), kind: str | None = None,
                 agent_name: str | None = Query(None, alias="agent"), last: int | None = Query(None, le=1000),
                 status: str | None = None, _: str = Depends(who)):
        return st["store"].messages(after, limit, kind, agent_name, last, status)

    @app.get("/messages/{mid}")
    def message(mid: int, _: str = Depends(who)):
        m = st["store"].get(mid)
        if m is None:
            raise HTTPException(404, "нет такого сообщения")
        return {**m, "replies": st["store"].replies(mid)}

    @app.get("/inbox")
    def inbox(limit: int = Query(100, le=1000), peek: bool = False, agent: str = Depends(agent_only)):
        items, more = st["store"].inbox(agent, limit, peek)
        return {"messages": items, "more": more}

    @app.get("/inbox/wait")
    def inbox_wait(timeout: float = Query(50, ge=0, le=55), all_: bool = Query(False, alias="all"),
                   agent: str = Depends(agent_only)):
        """Длинный опрос: вернуть, как только появится непрочитанное для агента (адресованное ему или, с all, любое)."""
        msgs = st["hub"].wait(agent, timeout, all_)
        return {"messages": msgs, "timeout": not msgs}

    @app.post("/busy")
    def busy(b: BusyIn, agent: str = Depends(agent_only)):
        """Показать в чате «⏳ Агент · думает…» (и «печатает…»); статус уберётся со следующим сообщением агента."""
        st["tg"].set_busy(agent, b.text, b.ttl_minutes * 60)
        return {"ok": True}

    @app.delete("/busy")
    def not_busy(agent: str = Depends(agent_only)):
        st["tg"].clear_busy(agent)
        return {"ok": True}

    @app.get("/status")
    def status(_: str = Depends(who)):
        return {"text": st["hub"].status_text()}

    # ---------- захваты ----------
    @app.get("/claims")
    def claims(_: str = Depends(who)):
        return st["store"].active_claims()

    @app.post("/claims", status_code=201)
    def claim(c: ClaimIn, agent: str = Depends(agent_only)):
        return st["hub"].claim(agent, c.topic, c.note, c.ttl_hours)

    @app.post("/claims/release")
    def release(key: str, agent: str = Depends(who)):
        return st["hub"].release(agent, key, force=agent == "admin")

    # ---------- идеи ----------
    @app.get("/ideas")
    def ideas(status: str | None = "open", _: str = Depends(who)):
        return st["hub"].ideas(None if status == "all" else status)

    @app.post("/ideas/{iid}/take", status_code=201)
    def take(iid: int, t: TakeIn, agent: str = Depends(agent_only)):
        return st["hub"].take_idea(agent, iid, t.note, t.ttl_hours)

    @app.post("/ideas/{iid}/close")
    def close(iid: int, c: CloseIn, agent: str = Depends(who)):
        return st["hub"].close_idea("hub" if agent == "admin" else agent, iid, c.outcome, c.text,
                                    admin=agent == "admin")

    # ---------- проверка отчётов ----------
    @app.get("/reviews/pending")
    def pending(agent: str = Depends(who)):
        return st["store"].unreviewed(None if agent == "admin" else agent)

    @app.post("/reports/{rid}/review", status_code=201)
    def review(rid: int, r: ReviewIn, agent: str = Depends(agent_only)):
        return st["hub"].review(agent, rid, r.verdict, r.text, r.refs)

    # ---------- счётчики ----------
    @app.get("/counters")
    def counters(_: str = Depends(who)):
        return st["store"].counters()

    @app.post("/counters/{name}/next")
    def next_value(name: str, agent: str = Depends(who)):
        return {"name": name, "value": st["store"].next_value(name), "agent": agent}

    @app.put("/counters/{name}")
    def set_counter(name: str, c: CounterIn, _: str = Depends(admin)):
        st["store"].set_counter(name, c.value)
        return {"name": name, "value": c.value}

    # ---------- админ по HTTP (то же, что в Telegram) ----------
    @app.post("/admin/agents", status_code=201)
    def add_agent(a: AgentIn, _: str = Depends(admin)):
        try:
            token = st["store"].add_agent(a.name, a.title or a.name, a.role)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"agent": a.name, "token": token}

    return app


app = create_app() if os.environ.get("AGENT_HUB_CONFIG") else None
