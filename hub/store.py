"""Хранилище хаба: SQLite. Лента сообщений, курсоры непрочитанного, захваты задач, счётчики."""
import json
import re
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,          -- имя агента; для людей из Telegram — 'human'
    author TEXT NOT NULL,         -- подпись: заголовок агента или имя человека
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    refs TEXT NOT NULL DEFAULT '[]',
    reply_to INTEGER,
    tg_state TEXT NOT NULL DEFAULT 'pending',   -- pending / sent / skip / failed
    tg_msg INTEGER
);
CREATE TABLE IF NOT EXISTS cursors (agent TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL, norm TEXT NOT NULL, agent TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL, expires REAL NOT NULL, released REAL
);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_tg ON messages (tg_state, id);
CREATE INDEX IF NOT EXISTS messages_tgmsg ON messages (tg_msg);
"""


def norm_topic(topic: str) -> str:
    return re.sub(r"\s+", " ", topic.strip().lower())


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    # ---------- сообщения ----------
    def add_message(self, agent: str, author: str, kind: str, text: str, refs=(), reply_to=None,
                    tg_state="pending", tg_msg=None) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO messages (ts, agent, author, kind, text, refs, reply_to, tg_state, tg_msg) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), agent, author, kind, text, json.dumps(list(refs), ensure_ascii=False), reply_to,
                 tg_state, tg_msg))
            return int(cur.lastrowid)

    def get(self, mid: int) -> dict | None:
        r = self.db.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
        return _msg(r) if r else None

    def by_tg_msg(self, tg_msg: int) -> dict | None:
        r = self.db.execute("SELECT * FROM messages WHERE tg_msg = ? ORDER BY id DESC LIMIT 1", (tg_msg,)).fetchone()
        return _msg(r) if r else None

    def messages(self, after=0, limit=100, kind=None, agent=None, last=None) -> list[dict]:
        where, args = ["id > ?"], [after]
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if agent:
            where.append("agent = ?")
            args.append(agent)
        sql = f"SELECT * FROM messages WHERE {' AND '.join(where)}"
        if last:  # последние N в хронологическом порядке
            rows = self.db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, last)).fetchall()[::-1]
        else:
            rows = self.db.execute(sql + " ORDER BY id LIMIT ?", (*args, limit)).fetchall()
        return [_msg(r) for r in rows]

    def inbox(self, agent: str, limit=100, peek=False) -> tuple[list[dict], bool]:
        """Непрочитанное агентом (кроме его собственных сообщений); курсор двигается, если не peek."""
        with self.lock:
            row = self.db.execute("SELECT last_id FROM cursors WHERE agent = ?", (agent,)).fetchone()
            last = row["last_id"] if row else 0
            rows = self.db.execute("SELECT * FROM messages WHERE id > ? ORDER BY id LIMIT ?",
                                   (last, limit + 1)).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            if rows and not peek:
                self.db.execute("INSERT INTO cursors (agent, last_id) VALUES (?, ?) "
                                "ON CONFLICT(agent) DO UPDATE SET last_id = excluded.last_id", (agent, rows[-1]["id"]))
        return [_msg(r) for r in rows if r["agent"] != agent], more

    def pending_tg(self, limit=20) -> list[dict]:
        rows = self.db.execute("SELECT * FROM messages WHERE tg_state = 'pending' ORDER BY id LIMIT ?",
                               (limit,)).fetchall()
        return [_msg(r) for r in rows]

    def set_tg(self, mid: int, state: str, tg_msg=None):
        with self.lock:
            self.db.execute("UPDATE messages SET tg_state = ?, tg_msg = COALESCE(?, tg_msg) WHERE id = ?",
                            (state, tg_msg, mid))

    # ---------- захваты задач ----------
    def active_claims(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM claims WHERE released IS NULL AND expires > ? ORDER BY id",
                               (time.time(),)).fetchall()
        return [dict(r) for r in rows]

    def claim(self, agent: str, topic: str, note: str, ttl_hours: float) -> tuple[dict, dict | None]:
        """(захват, чужой активный захват той же темы или None). Свой повторный захват продлевает срок."""
        n, now = norm_topic(topic), time.time()
        with self.lock:
            other = self.db.execute("SELECT * FROM claims WHERE norm = ? AND released IS NULL AND expires > ?",
                                    (n, now)).fetchone()
            if other and other["agent"] != agent:
                return dict(other), dict(other)
            if other:
                self.db.execute("UPDATE claims SET expires = ?, note = ? WHERE id = ?",
                                (now + ttl_hours * 3600, note or other["note"], other["id"]))
                cid = other["id"]
            else:
                cid = self.db.execute("INSERT INTO claims (topic, norm, agent, note, ts, expires) VALUES (?,?,?,?,?,?)",
                                      (topic.strip(), n, agent, note, now, now + ttl_hours * 3600)).lastrowid
            cl = dict(self.db.execute("SELECT * FROM claims WHERE id = ?", (cid,)).fetchone())
            cl["renewed"] = other is not None
            return cl, None

    def release(self, agent: str, key: str, force=False) -> dict | None:
        """Освободить захват по id или по теме; чужой — только с force (админ)."""
        with self.lock:
            if key.isdigit():
                r = self.db.execute("SELECT * FROM claims WHERE id = ? AND released IS NULL", (int(key),)).fetchone()
            else:
                r = self.db.execute("SELECT * FROM claims WHERE norm = ? AND released IS NULL ORDER BY id DESC",
                                    (norm_topic(key),)).fetchone()
            if r is None or (r["agent"] != agent and not force):
                return None
            self.db.execute("UPDATE claims SET released = ? WHERE id = ?", (time.time(), r["id"]))
            return dict(r)

    # ---------- счётчики и служебное ----------
    def next_value(self, name: str) -> int:
        with self.lock:
            self.db.execute("INSERT INTO counters (name, value) VALUES (?, 1) "
                            "ON CONFLICT(name) DO UPDATE SET value = value + 1", (name,))
            return int(self.db.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()[0])

    def set_counter(self, name: str, value: int):
        with self.lock:
            self.db.execute("INSERT INTO counters (name, value) VALUES (?, ?) "
                            "ON CONFLICT(name) DO UPDATE SET value = excluded.value", (name, value))

    def counters(self) -> dict:
        return {r["name"]: r["value"] for r in self.db.execute("SELECT * FROM counters ORDER BY name")}

    def kv_get(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else default

    def kv_set(self, key: str, value):
        with self.lock:
            self.db.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def _msg(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["refs"] = json.loads(d["refs"] or "[]")
    return d
