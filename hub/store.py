"""Хранилище хаба: SQLite. Агенты, лента, непрочитанное, захваты задач, идеи, проверки отчётов, счётчики."""
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    token_hash TEXT NOT NULL,
    bot_token TEXT,
    disabled INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS admins (tg_user INTEGER PRIMARY KEY, name TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,          -- имя агента; люди из Telegram — 'human', сам хаб — 'hub'
    author TEXT NOT NULL,         -- подпись в ленте и в чате
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    refs TEXT NOT NULL DEFAULT '[]',
    reply_to INTEGER,
    status TEXT,                  -- идея: open / taken / done / dropped; отчёт: unreviewed / confirmed / disputed
    owner TEXT,                   -- идея: кто взял
    tg_state TEXT NOT NULL DEFAULT 'pending',   -- pending / sent / skip / failed
    tg_msg INTEGER
);
CREATE TABLE IF NOT EXISTS cursors (agent TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL, norm TEXT NOT NULL, agent TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
    idea INTEGER, ts REAL NOT NULL, expires REAL NOT NULL, released REAL
);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_tg ON messages (tg_state, id);
CREATE INDEX IF NOT EXISTS messages_tgmsg ON messages (tg_msg);
CREATE INDEX IF NOT EXISTS messages_kind ON messages (kind, status);
"""
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


def norm_topic(topic: str) -> str:
    return re.sub(r"\s+", " ", topic.strip().lower())


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def q(self, sql: str, *args) -> list[dict]:
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def q1(self, sql: str, *args) -> dict | None:
        r = self.db.execute(sql, args).fetchone()
        return dict(r) if r else None

    # ---------- агенты ----------
    def agent(self, name: str) -> dict | None:
        return self.q1("SELECT * FROM agents WHERE name = ?", name)

    def agents(self, with_disabled=True) -> list[dict]:
        return self.q("SELECT * FROM agents" + ("" if with_disabled else " WHERE disabled = 0") + " ORDER BY created")

    def agent_by_token(self, token: str) -> dict | None:
        return self.q1("SELECT * FROM agents WHERE token_hash = ? AND disabled = 0", token_hash(token))

    def add_agent(self, name: str, title: str, role: str = "", token: str | None = None) -> str:
        """Новый агент -> его API-токен (показывается один раз, в базе — только хеш)."""
        if not NAME_RE.match(name):
            raise ValueError("имя: латиница в нижнем регистре, цифры, _ и -, 2–32 символа, с буквы")
        if name in ("human", "hub", "admin"):
            raise ValueError("это имя зарезервировано")
        token = token or secrets.token_urlsafe(32)
        with self.lock:
            if self.agent(name):
                raise ValueError(f"агент {name} уже есть")
            self.db.execute("INSERT INTO agents (name, title, role, token_hash, created) VALUES (?, ?, ?, ?, ?)",
                            (name, title or name, role, token_hash(token), time.time()))
        return token

    def update_agent(self, name: str, **fields):
        allowed = {"title", "role", "bot_token", "disabled"}
        bad = set(fields) - allowed
        if bad or not self.agent(name):
            raise ValueError(f"нет агента {name}" if not bad else f"нельзя менять {bad}")
        with self.lock:
            for k, v in fields.items():
                self.db.execute(f"UPDATE agents SET {k} = ? WHERE name = ?", (v, name))

    def rotate_token(self, name: str) -> str:
        if not self.agent(name):
            raise ValueError(f"нет агента {name}")
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.db.execute("UPDATE agents SET token_hash = ? WHERE name = ?", (token_hash(token), name))
        return token

    def seed_agents(self, agents: dict):
        """Агенты из конфига установки (токены там открытым текстом) — в базу, если их там ещё нет."""
        for name, a in agents.items():
            if not self.agent(name) and a.get("api_token"):
                self.add_agent(name, a.get("title") or name, a.get("role", ""), token=a["api_token"])
            if a.get("bot_token") and not (self.agent(name) or {}).get("bot_token"):
                self.update_agent(name, bot_token=a["bot_token"])

    # ---------- админы Telegram ----------
    def is_admin(self, tg_user: int) -> bool:
        return self.q1("SELECT 1 AS x FROM admins WHERE tg_user = ?", tg_user) is not None

    def add_admin(self, tg_user: int, name: str):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO admins (tg_user, name, ts) VALUES (?, ?, ?)",
                            (tg_user, name, time.time()))

    def admins(self) -> list[dict]:
        return self.q("SELECT * FROM admins ORDER BY ts")

    # ---------- сообщения ----------
    def add_message(self, agent: str, author: str, kind: str, text: str, refs=(), reply_to=None,
                    tg_state="pending", tg_msg=None, status=None) -> int:
        if status is None:
            status = {"idea": "open", "report": "unreviewed"}.get(kind)
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO messages (ts, agent, author, kind, text, refs, reply_to, status, tg_state, tg_msg) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), agent, author, kind, text, json.dumps(list(refs), ensure_ascii=False), reply_to,
                 status, tg_state, tg_msg))
            return int(cur.lastrowid)

    def get(self, mid: int) -> dict | None:
        return _msg(self.q1("SELECT * FROM messages WHERE id = ?", mid))

    def by_tg_msg(self, tg_msg: int) -> dict | None:
        return _msg(self.q1("SELECT * FROM messages WHERE tg_msg = ? ORDER BY id DESC LIMIT 1", tg_msg))

    def set_status(self, mid: int, status: str, owner: str | None = None):
        with self.lock:
            self.db.execute("UPDATE messages SET status = ?, owner = COALESCE(?, owner) WHERE id = ?",
                            (status, owner, mid))

    def messages(self, after=0, limit=100, kind=None, agent=None, last=None, status=None) -> list[dict]:
        where, args = ["id > ?"], [after]
        for col, val in (("kind", kind), ("agent", agent), ("status", status)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        sql = f"SELECT * FROM messages WHERE {' AND '.join(where)}"
        if last:  # последние N в хронологическом порядке
            rows = self.q(sql + " ORDER BY id DESC LIMIT ?", *args, last)[::-1]
        else:
            rows = self.q(sql + " ORDER BY id LIMIT ?", *args, limit)
        return [_msg(r) for r in rows]

    def replies(self, mid: int, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM messages WHERE reply_to = ?" + (" AND kind = ?" if kind else "") + " ORDER BY id"
        return [_msg(r) for r in self.q(sql, *((mid, kind) if kind else (mid,)))]

    def inbox(self, agent: str, limit=100, peek=False) -> tuple[list[dict], bool]:
        """Непрочитанное агентом (кроме его собственных сообщений); курсор двигается, если не peek."""
        with self.lock:
            row = self.q1("SELECT last_id FROM cursors WHERE agent = ?", agent)
            last = row["last_id"] if row else 0
            rows = self.q("SELECT * FROM messages WHERE id > ? ORDER BY id LIMIT ?", last, limit + 1)
            more = len(rows) > limit
            rows = rows[:limit]
            if rows and not peek:
                self.db.execute("INSERT INTO cursors (agent, last_id) VALUES (?, ?) "
                                "ON CONFLICT(agent) DO UPDATE SET last_id = excluded.last_id", (agent, rows[-1]["id"]))
        return [_msg(r) for r in rows if r["agent"] != agent], more

    def last_activity(self) -> dict:
        return {r["agent"]: r["ts"] for r in self.q("SELECT agent, MAX(ts) AS ts FROM messages GROUP BY agent")}

    def pending_tg(self, limit=20) -> list[dict]:
        return [_msg(r) for r in self.q("SELECT * FROM messages WHERE tg_state = 'pending' ORDER BY id LIMIT ?", limit)]

    def skip_pending_tg(self):
        with self.lock:
            self.db.execute("UPDATE messages SET tg_state = 'skip' WHERE tg_state = 'pending'")

    def set_tg(self, mid: int, state: str, tg_msg=None):
        with self.lock:
            self.db.execute("UPDATE messages SET tg_state = ?, tg_msg = COALESCE(?, tg_msg) WHERE id = ?",
                            (state, tg_msg, mid))

    # ---------- проверка отчётов ----------
    def unreviewed(self, agent: str | None = None) -> list[dict]:
        """Отчёты без проверки; для агента — только чужие и ещё не проверенные им."""
        rows = [_msg(r) for r in self.q("SELECT * FROM messages WHERE kind = 'report' AND status IN "
                                        "('unreviewed', 'disputed') ORDER BY id")]
        if agent:
            rows = [r for r in rows if r["agent"] != agent
                    and not any(x["agent"] == agent for x in self.replies(r["id"], "review"))]
        return rows

    def review_status(self, report_id: int) -> str:
        """Итог проверок отчёта: любое опровержение — disputed, иначе хотя бы одно подтверждение — confirmed."""
        verdicts = [r["status"] for r in self.replies(report_id, "review")]  # у проверки status — её вердикт
        return "disputed" if "refute" in verdicts else "confirmed" if "confirm" in verdicts else "unreviewed"

    # ---------- захваты задач ----------
    def active_claims(self) -> list[dict]:
        return self.q("SELECT * FROM claims WHERE released IS NULL AND expires > ? ORDER BY id", time.time())

    def claim(self, agent: str, topic: str, note: str, ttl_hours: float, idea: int | None = None):
        """(захват, чужой активный захват той же темы или None). Свой повторный захват продлевает срок."""
        n, now = norm_topic(topic), time.time()
        with self.lock:
            other = self.q1("SELECT * FROM claims WHERE norm = ? AND released IS NULL AND expires > ?", n, now)
            if other and other["agent"] != agent:
                return other, other
            if other:
                self.db.execute("UPDATE claims SET expires = ?, note = ? WHERE id = ?",
                                (now + ttl_hours * 3600, note or other["note"], other["id"]))
                cid = other["id"]
            else:
                cid = self.db.execute(
                    "INSERT INTO claims (topic, norm, agent, note, idea, ts, expires) VALUES (?,?,?,?,?,?,?)",
                    (topic.strip(), n, agent, note, idea, now, now + ttl_hours * 3600)).lastrowid
            cl = self.q1("SELECT * FROM claims WHERE id = ?", cid)
            cl["renewed"] = other is not None
            return cl, None

    def release(self, agent: str, key: str, force=False) -> dict | None:
        """Освободить захват по id или по теме; чужой — только с force (админ)."""
        with self.lock:
            if key.isdigit():
                r = self.q1("SELECT * FROM claims WHERE id = ? AND released IS NULL", int(key))
            else:
                r = self.q1("SELECT * FROM claims WHERE norm = ? AND released IS NULL ORDER BY id DESC",
                            norm_topic(key))
            if r is None or (r["agent"] != agent and not force):
                return None
            self.db.execute("UPDATE claims SET released = ? WHERE id = ?", (time.time(), r["id"]))
            return r

    # ---------- счётчики и служебное ----------
    def next_value(self, name: str) -> int:
        with self.lock:
            self.db.execute("INSERT INTO counters (name, value) VALUES (?, 1) "
                            "ON CONFLICT(name) DO UPDATE SET value = value + 1", (name,))
            return int(self.q1("SELECT value FROM counters WHERE name = ?", name)["value"])

    def set_counter(self, name: str, value: int):
        with self.lock:
            self.db.execute("INSERT INTO counters (name, value) VALUES (?, ?) "
                            "ON CONFLICT(name) DO UPDATE SET value = excluded.value", (name, value))

    def counters(self) -> dict:
        return {r["name"]: r["value"] for r in self.q("SELECT * FROM counters ORDER BY name")}

    def kv_get(self, key: str, default=None):
        r = self.q1("SELECT value FROM kv WHERE key = ?", key)
        return r["value"] if r else default

    def kv_set(self, key: str, value):
        with self.lock:
            self.db.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def _msg(r: dict | None) -> dict | None:
    if r is None:
        return None
    r["refs"] = json.loads(r["refs"] or "[]")
    return r
