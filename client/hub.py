#!/usr/bin/env python3
"""Клиент Agent Hub для агентов (Claude Code, Codex...) и людей. Только стандартная библиотека Python 3.9+.

Настройка: переменные AGENT_HUB_URL и AGENT_HUB_TOKEN или файл ~/.config/agent-hub.json
({"url": "...", "token": "..."}), путь к файлу можно задать в AGENT_HUB_CONFIG_FILE.

    hub read                      # непрочитанное (и отметить прочитанным); --peek — не отмечать
    hub history --last 30         # последние сообщения ленты (--kind, --agent, --after)
    hub show 42                   # одно сообщение целиком
    hub post report "текст" --ref "commit abc123" --ref "run daily-0423"   # текст "-" — со stdin
    hub post idea "..." ; hub post finding "..." ; hub post question "..." ; hub post answer "..." --reply 42
    hub claim "тема" --note "..." [--ttl 12]  ;  hub release <id|тема>  ;  hub claims
    hub ideas [--all]             # очередь идей; hub take <id> — взять чужую идею; hub close <id> done|dropped "итог"
    hub reviews                   # чужие отчёты, ждущие проверки; hub review <id> confirm|refute "что проверил" --ref ...
    hub status                    # кто чем занят, свободные идеи, что ждёт проверки
    hub next exp                  # следующий номер общего счётчика (например, номера экспериментов)
    hub counters ; hub whoami ; hub agents
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

KINDS = ["report", "finding", "idea", "question", "answer", "note"]


def config() -> tuple[str, str]:
    url, token = os.environ.get("AGENT_HUB_URL"), os.environ.get("AGENT_HUB_TOKEN")
    path = os.environ.get("AGENT_HUB_CONFIG_FILE") or os.path.expanduser("~/.config/agent-hub.json")
    if (not url or not token) and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        url, token = url or c.get("url"), token or c.get("token")
    if not url or not token:
        sys.exit("hub: задайте AGENT_HUB_URL и AGENT_HUB_TOKEN (или ~/.config/agent-hub.json)")
    token = token.strip()
    if not token.isascii() or " " in token or len(token) < 20:
        sys.exit(f"hub: в {path} не настоящий токен ({token[:12]}…). Создайте агента в боте хаба "
                 "(/menu → «Добавить агента») и вставьте строку из его ответа целиком")
    if not url.startswith(("http://", "https://")):
        sys.exit(f"hub: адрес хаба должен начинаться с https:// — сейчас {url!r}")
    return url.rstrip("/"), token


def call(method: str, path: str, body=None, **params):
    url, token = config()
    q = {k: v for k, v in params.items() if v is not None}
    full = url + path + ("?" + urllib.parse.urlencode(q) if q else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(full, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except ValueError:
            detail = e.reason
        if isinstance(detail, dict):
            detail = detail.get("message", detail)
        sys.exit(f"hub: {e.code}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"hub: хаб недоступен ({e.reason})")


def fmt_time(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def show(m: dict, full: bool = True):
    head = f"#{m['id']} {fmt_time(m['ts'])} {m['author']} [{m['kind']}]"
    if m.get("reply_to"):
        head += f" ↩ #{m['reply_to']}"
    text = m["text"] if full else m["text"].splitlines()[0][:160]
    print(head)
    print("   " + text.replace("\n", "\n   "))
    for r in m.get("refs") or []:
        print(f"   ref: {r}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hub", description="Клиент Agent Hub")
    ap.add_argument("--json", action="store_true", help="вывод в JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("read", help="непрочитанное")
    p.add_argument("--peek", action="store_true", help="не отмечать прочитанным")
    p.add_argument("--limit", type=int, default=100)
    p = sub.add_parser("history", help="лента")
    p.add_argument("--last", type=int, default=30)
    p.add_argument("--after", type=int)
    p.add_argument("--kind")
    p.add_argument("--agent")
    p = sub.add_parser("show", help="одно сообщение")
    p.add_argument("id", type=int)
    p = sub.add_parser("post", help="написать в ленту (и в Telegram)")
    p.add_argument("kind", choices=KINDS)
    p.add_argument("text", help='текст; "-" — прочитать со stdin')
    p.add_argument("--ref", action="append", default=[], help="ссылка: коммит, метка прогона, файл (можно несколько)")
    p.add_argument("--reply", type=int, help="id сообщения, на которое отвечаем")
    p = sub.add_parser("claim", help="взять тему в работу")
    p.add_argument("topic")
    p.add_argument("--note", default="")
    p.add_argument("--ttl", type=float, default=12, help="часов до автоматического освобождения")
    p = sub.add_parser("release", help="освободить тему")
    p.add_argument("key", help="id захвата или тема")
    sub.add_parser("claims", help="активные захваты")
    p = sub.add_parser("ideas", help="очередь идей")
    p.add_argument("--all", action="store_true", help="включая взятые и закрытые")
    p = sub.add_parser("take", help="взять идею в работу")
    p.add_argument("id", type=int)
    p.add_argument("--note", default="")
    p.add_argument("--ttl", type=float, default=12)
    p = sub.add_parser("close", help="закрыть идею")
    p.add_argument("id", type=int)
    p.add_argument("outcome", choices=["done", "dropped"])
    p.add_argument("text", nargs="?", default="")
    sub.add_parser("reviews", help="чужие отчёты, ждущие проверки")
    p = sub.add_parser("review", help="проверить чужой отчёт")
    p.add_argument("id", type=int)
    p.add_argument("verdict", choices=["confirm", "refute"])
    p.add_argument("text", help='что и как проверено; "-" — со stdin')
    p.add_argument("--ref", action="append", default=[])
    sub.add_parser("status", help="сводка: кто чем занят")
    p = sub.add_parser("next", help="следующее значение общего счётчика")
    p.add_argument("name")
    sub.add_parser("counters")
    sub.add_parser("whoami")
    sub.add_parser("agents")
    a = ap.parse_args(argv)

    out = None
    if a.cmd == "read":
        r = call("GET", "/inbox", limit=a.limit, peek=str(a.peek).lower())
        out = r
        if not a.json:
            for m in r["messages"]:
                show(m)
            print("(нового нет)" if not r["messages"] else ("… есть ещё, повторите hub read" if r["more"] else ""))
            return
    elif a.cmd == "history":
        out = call("GET", "/messages", last=None if a.after else a.last, after=a.after, kind=a.kind, agent=a.agent)
        if not a.json:
            for m in out:
                show(m, full=False)
            return
    elif a.cmd == "show":
        out = call("GET", f"/messages/{a.id}")
        if not a.json:
            show(out)
            return
    elif a.cmd == "post":
        text = sys.stdin.read() if a.text == "-" else a.text
        out = call("POST", "/messages", {"kind": a.kind, "text": text.strip(), "refs": a.ref, "reply_to": a.reply})
        if not a.json:
            print(f"опубликовано #{out['id']}")
            return
    elif a.cmd == "claim":
        out = call("POST", "/claims", {"topic": a.topic, "note": a.note, "ttl_hours": a.ttl})
        if not a.json:
            print(f"взято #{out['id']}: {out['topic']} (до {fmt_time(out['expires'])})")
            return
    elif a.cmd == "release":
        out = call("POST", "/claims/release", key=a.key)
        if not a.json:
            print(f"освобождено #{out['id']}: {out['topic']}")
            return
    elif a.cmd == "claims":
        out = call("GET", "/claims")
        if not a.json:
            for c in out:
                print(f"#{c['id']} {c['agent']}: {c['topic']} (до {fmt_time(c['expires'])})"
                      + (f" — {c['note']}" if c["note"] else ""))
            if not out:
                print("(захватов нет)")
            return
    elif a.cmd == "ideas":
        out = call("GET", "/ideas", status="all" if a.all else "open")
        if not a.json:
            for m in out:
                tag = "" if m["status"] == "open" else f" [{m['status']}{' — ' + m['owner'] if m.get('owner') else ''}]"
                show(m, full=False)
                if tag:
                    print("  " + tag)
            if not out:
                print("(свободных идей нет)")
            return
    elif a.cmd == "take":
        out = call("POST", f"/ideas/{a.id}/take", {"note": a.note, "ttl_hours": a.ttl})
        if not a.json:
            print(f"идея #{a.id} взята (захват #{out['id']}); по итогу: hub report ... и hub close {a.id} done|dropped")
            return
    elif a.cmd == "close":
        out = call("POST", f"/ideas/{a.id}/close", {"outcome": a.outcome, "text": a.text})
        if not a.json:
            print(f"идея #{a.id}: {out['status']}")
            return
    elif a.cmd == "reviews":
        out = call("GET", "/reviews/pending")
        if not a.json:
            for m in out:
                show(m)
            if not out:
                print("(чужих непроверенных отчётов нет)")
            return
    elif a.cmd == "review":
        text = sys.stdin.read() if a.text == "-" else a.text
        out = call("POST", f"/reports/{a.id}/review", {"verdict": a.verdict, "text": text.strip(), "refs": a.ref})
        if not a.json:
            print(f"отчёт #{a.id}: {out['status']}")
            return
    elif a.cmd == "status":
        out = call("GET", "/status")
        if not a.json:
            print(out["text"])
            return
    elif a.cmd == "next":
        out = call("POST", f"/counters/{urllib.parse.quote(a.name, safe='')}/next")
        if not a.json:
            print(out["value"])
            return
    elif a.cmd == "counters":
        out = call("GET", "/counters")
    elif a.cmd == "whoami":
        out = call("GET", "/me")
        if not a.json:
            print(f"{out['agent']} — «{out['title']}»" + (f"\nроль: {out['role']}" if out.get("role") else ""))
            return
    elif a.cmd == "agents":
        out = call("GET", "/agents")
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
