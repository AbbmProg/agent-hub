"""HTTP API хаба. Запуск: AGENT_HUB_CONFIG=/etc/agent-hub/config.json uvicorn hub.server:app

Авторизация: `Authorization: Bearer <api_token агента>`; админ-токен — для служебных операций.
"""
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .store import Store
from .telegram import Telegram

AGENT_KINDS = {"report", "finding", "idea", "question", "answer", "note"}
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def load_config(path: str | None = None) -> dict:
    path = path or os.environ.get("AGENT_HUB_CONFIG", "/etc/agent-hub/config.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("agents", {})
    return cfg


class PostIn(BaseModel):
    kind: str
    text: str = Field(min_length=1, max_length=16000)
    refs: list[str] = Field(default_factory=list, max_length=20)
    reply_to: int | None = None


class ClaimIn(BaseModel):
    topic: str = Field(min_length=2, max_length=300)
    note: str = Field(default="", max_length=2000)
    ttl_hours: float = Field(default=12, gt=0, le=24 * 14)


class CounterIn(BaseModel):
    value: int = Field(ge=0)


def create_app(cfg: dict | None = None, transport=None) -> FastAPI:
    holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        c = cfg or load_config()
        store = Store(c.get("db", "/var/lib/agent-hub/hub.db"))
        tg = Telegram(c, store, transport=transport)
        holder.update(cfg=c, store=store, tg=tg)
        tg.start()
        yield
        tg.stop.set()

    app = FastAPI(title="Agent Hub", lifespan=lifespan)

    def who(authorization: str = Header(default="")) -> str:
        token = authorization.removeprefix("Bearer ").strip()
        c = holder["cfg"]
        if token and c.get("admin_token") and secrets.compare_digest(token, c["admin_token"]):
            return "admin"
        for name, a in c["agents"].items():
            if token and a.get("api_token") and secrets.compare_digest(token, a["api_token"]):
                return name
        raise HTTPException(401, "неверный токен")

    def admin(agent: str = Depends(who)) -> str:
        if agent != "admin":
            raise HTTPException(403, "нужен админ-токен")
        return agent

    def title(agent: str) -> str:
        return (holder["cfg"]["agents"].get(agent) or {}).get("title") or agent

    def post_system(agent: str, kind: str, text: str, refs=()):
        return holder["store"].add_message(agent, title(agent), kind, text, refs)

    @app.get("/health")
    def health():
        tg = holder["tg"]
        return {"ok": True, "telegram": tg.enabled, "agents": sorted(holder["cfg"]["agents"])}

    @app.get("/me")
    def me(agent: str = Depends(who)):
        return {"agent": agent, "title": title(agent)}

    @app.get("/agents")
    def agents(_: str = Depends(who)):
        return [{"agent": n, "title": a.get("title") or n, "bot": bool(a.get("bot_token"))}
                for n, a in holder["cfg"]["agents"].items()]

    @app.post("/messages", status_code=201)
    def post(m: PostIn, agent: str = Depends(who)):
        kinds = AGENT_KINDS | ({"system"} if agent == "admin" else set())
        if m.kind not in kinds:
            raise HTTPException(422, f"kind должен быть одним из: {', '.join(sorted(kinds))}")
        if m.reply_to is not None and holder["store"].get(m.reply_to) is None:
            raise HTTPException(404, f"нет сообщения #{m.reply_to}")
        mid = holder["store"].add_message(agent, title(agent) if agent != "admin" else "Хаб", m.kind, m.text,
                                          m.refs, m.reply_to)
        return {"id": mid}

    @app.get("/messages")
    def messages(after: int = 0, limit: int = Query(100, le=1000), kind: str | None = None,
                 agent_name: str | None = Query(None, alias="agent"), last: int | None = Query(None, le=1000),
                 _: str = Depends(who)):
        return holder["store"].messages(after, limit, kind, agent_name, last)

    @app.get("/messages/{mid}")
    def message(mid: int, _: str = Depends(who)):
        m = holder["store"].get(mid)
        if m is None:
            raise HTTPException(404, "нет такого сообщения")
        return m

    @app.get("/inbox")
    def inbox(limit: int = Query(100, le=1000), peek: bool = False, agent: str = Depends(who)):
        items, more = holder["store"].inbox(agent, limit, peek)
        return {"messages": items, "more": more}

    @app.get("/claims")
    def claims(_: str = Depends(who)):
        return holder["store"].active_claims()

    @app.post("/claims", status_code=201)
    def claim(c: ClaimIn, agent: str = Depends(who)):
        cl, other = holder["store"].claim(agent, c.topic, c.note, c.ttl_hours)
        if other:
            raise HTTPException(409, {"message": f"тема уже занята: {title(other['agent'])}", "claim": other})
        if not cl["renewed"]:  # продление своего захвата в чат не пишем
            post_system(agent, "claim", cl["topic"] + (f"\n{c.note}" if c.note else ""))
        return cl

    @app.post("/claims/release")
    def release(key: str, agent: str = Depends(who)):
        cl = holder["store"].release(agent, key, force=agent == "admin")
        if cl is None:
            raise HTTPException(404, "активного захвата с таким id/темой у вас нет")
        post_system(cl["agent"], "release", cl["topic"])
        return cl

    @app.get("/counters")
    def counters(_: str = Depends(who)):
        return holder["store"].counters()

    @app.post("/counters/{name}/next")
    def next_value(name: str, agent: str = Depends(who)):
        v = holder["store"].next_value(name)
        return {"name": name, "value": v, "agent": agent}

    @app.put("/counters/{name}")
    def set_counter(name: str, c: CounterIn, _: str = Depends(admin)):
        holder["store"].set_counter(name, c.value)
        return {"name": name, "value": c.value}

    return app


app = create_app() if os.environ.get("AGENT_HUB_CONFIG") else None
