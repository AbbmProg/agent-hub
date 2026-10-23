#!/usr/bin/env python3
"""Конфиг хаба (/etc/agent-hub/config.json): создать или дополнить, не теряя выданных токенов. Только stdlib.

    configure.py init   --config PATH --db PATH [--agent NAME[:Заголовок]] [--bot NAME=TOKEN] [--chat-id ID]
                        [--listener NAME] [--tg-proxy URL]
    configure.py find-chat --token BOT_TOKEN [--tg-proxy URL]     # id групп, где бот видел сообщения
    configure.py tokens --config PATH                             # токены агентов и админа
"""
import argparse
import json
import os
import secrets
import sys
import urllib.request


def load(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save(path: str, cfg: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def tg_get(token: str, method: str, proxy: str | None = None, **params) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    if proxy and proxy.startswith("socks"):  # urllib не умеет SOCKS — httpx из venv хаба
        import httpx
        return httpx.get(url, proxy=proxy, timeout=20).json()
    handlers = [urllib.request.ProxyHandler({"https": proxy, "http": proxy})] if proxy else []
    with urllib.request.build_opener(*handlers).open(url, timeout=20) as r:
        return json.loads(r.read())


def find_chats(token: str, proxy: str | None = None) -> dict:
    data = tg_get(token, "getUpdates", proxy, timeout=0)
    chats = {}
    for up in data.get("result", []):
        for key in ("message", "my_chat_member", "channel_post"):
            c = (up.get(key) or {}).get("chat")
            if c and c.get("type") in ("group", "supergroup"):
                chats[c["id"]] = c.get("title", "")
    return chats


def cmd_init(a):
    cfg = load(a.config)
    cfg.setdefault("agents", {})
    cfg["db"] = a.db or cfg.get("db", "/var/lib/agent-hub/hub.db")
    cfg.setdefault("admin_token", secrets.token_urlsafe(32))
    for spec in a.agent:
        name, _, title = spec.partition(":")
        ag = cfg["agents"].setdefault(name.strip(), {})
        ag.setdefault("api_token", secrets.token_urlsafe(32))
        if title:
            ag["title"] = title.strip()
    for spec in a.bot:
        name, _, token = spec.partition("=")
        if not token:
            sys.exit(f"--bot ожидает NAME=TOKEN, получено {spec!r}")
        ag = cfg["agents"].setdefault(name.strip(), {})
        ag.setdefault("api_token", secrets.token_urlsafe(32))
        ag["bot_token"] = token.strip()
    if a.chat_id:
        cfg["chat_id"] = int(a.chat_id)
    if a.listener:
        cfg["listener"] = a.listener
    if a.tg_proxy is not None:
        cfg["tg_proxy"] = a.tg_proxy or None
    if not cfg.get("chat_id"):  # попробовать найти группу по сообщениям, которые видел бот
        for name, ag in cfg["agents"].items():
            if not ag.get("bot_token"):
                continue
            try:
                chats = find_chats(ag["bot_token"], cfg.get("tg_proxy"))
            except Exception as e:  # noqa: BLE001
                print(f"  не удалось спросить Telegram ботом {name}: {e}", file=sys.stderr)
                break
            if len(chats) == 1:
                cfg["chat_id"] = next(iter(chats))
                print(f"  группа найдена: {cfg['chat_id']} «{chats[cfg['chat_id']]}»")
            elif chats:
                print("  бот видел несколько групп — укажите --chat-id: "
                      + ", ".join(f"{k} «{v}»" for k, v in chats.items()))
            break
    save(a.config, cfg)
    print(f"конфиг: {a.config}; агентов {len(cfg['agents'])}; Telegram-группа: {cfg.get('chat_id') or 'не задана'}")


def cmd_find_chat(a):
    chats = find_chats(a.token, a.tg_proxy)
    if not chats:
        print("групп не найдено: добавьте бота в группу и напишите там любое сообщение, затем повторите")
    for k, v in chats.items():
        print(f"{k}\t{v}")


def cmd_tokens(a):
    cfg = load(a.config)
    print(f"admin\t{cfg.get('admin_token')}")
    for name, ag in cfg.get("agents", {}).items():
        print(f"{name}\t{ag.get('api_token')}\t{ag.get('title') or name}\tбот: {'да' if ag.get('bot_token') else 'нет'}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--config", required=True)
    p.add_argument("--db")
    p.add_argument("--agent", action="append", default=[], help="NAME[:Заголовок в чате]")
    p.add_argument("--bot", action="append", default=[], help="NAME=BOT_TOKEN")
    p.add_argument("--chat-id")
    p.add_argument("--listener", help="чей бот читает сообщения людей (по умолчанию — первый с токеном)")
    p.add_argument("--tg-proxy", help="прокси к api.telegram.org: socks5://host:port или http://...; '' — убрать")
    p = sub.add_parser("find-chat")
    p.add_argument("--token", required=True)
    p.add_argument("--tg-proxy")
    p = sub.add_parser("tokens")
    p.add_argument("--config", required=True)
    a = ap.parse_args()
    {"init": cmd_init, "find-chat": cmd_find_chat, "tokens": cmd_tokens}[a.cmd](a)


if __name__ == "__main__":
    main()
