"""Small synchronous SQLite transactions; never hold a transaction across an await."""

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users (
 tg INTEGER PRIMARY KEY, yt TEXT NOT NULL UNIQUE, login TEXT NOT NULL, name TEXT NOT NULL,
 zone TEXT NOT NULL, connected REAL NOT NULL, binding TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, blocked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS credentials (
 tg INTEGER PRIMARY KEY REFERENCES users(tg) ON DELETE CASCADE,
 access BLOB NOT NULL, refresh BLOB, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth (
 state TEXT PRIMARY KEY, tg INTEGER NOT NULL, verifier BLOB NOT NULL,
 cookie TEXT, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS conversations (
 tg INTEGER PRIMARY KEY, flow TEXT NOT NULL, mode TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actions (
 token TEXT PRIMARY KEY, tg INTEGER NOT NULL, binding TEXT NOT NULL,
 kind TEXT NOT NULL, data TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notifications (
 tg INTEGER NOT NULL, day TEXT NOT NULL, status TEXT NOT NULL,
 PRIMARY KEY(tg, day));
CREATE TABLE IF NOT EXISTS operations (
 id TEXT PRIMARY KEY, flow TEXT NOT NULL UNIQUE, tg INTEGER NOT NULL,
 yt TEXT NOT NULL, issue TEXT NOT NULL, minutes INTEGER NOT NULL,
 day TEXT NOT NULL, status TEXT NOT NULL, work_id TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS free_entries (
 id TEXT PRIMARY KEY, flow TEXT NOT NULL UNIQUE, tg INTEGER NOT NULL,
 description TEXT NOT NULL, minutes INTEGER NOT NULL CHECK(minutes > 0),
 day TEXT NOT NULL, zone TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS free_entries_owner_day ON free_entries(tg, day);
CREATE TABLE IF NOT EXISTS reminder_preferences (
 tg INTEGER PRIMARY KEY, time TEXT NOT NULL, from_day TEXT NOT NULL);
"""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class DB:
    def __init__(self, path: Path, key: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.cipher = Fernet(key.encode())
        path.chmod(0o600)

    def execute(self, sql, args=()):
        with self.conn:
            return self.conn.execute(sql, args)

    def one(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def seal(self, text):
        return self.cipher.encrypt(text.encode()) if text else None

    def open(self, data):
        return self.cipher.decrypt(data).decode() if data else None

    def user(self, tg):
        return self.one("SELECT * FROM users WHERE tg=?", (tg,))

    def reminder_preference(self, tg):
        return self.one("SELECT * FROM reminder_preferences WHERE tg=?", (tg,))

    def save_reminder_preference(self, tg, value, from_day):
        # Retain personal settings across OAuth disconnect/rebinding.
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO reminder_preferences VALUES(?,?,?)",
                (tg, value, from_day),
            )
            self.conn.execute("DELETE FROM conversations WHERE tg=?", (tg,))

    def bind(self, tg, me, tokens, zone):
        with self.conn:
            existing = self.user(tg)
            if existing and existing["yt"] != me["id"]:
                raise ValueError("Сначала отключи текущий аккаунт командой /disconnect.")
            other = self.one("SELECT tg FROM users WHERE yt=? AND tg!=?", (me["id"], tg))
            if other:
                raise ValueError("Этот аккаунт YouTrack уже подключён к другому Telegram.")
            self.conn.execute("DELETE FROM users WHERE tg=?", (tg,))
            self.conn.execute(
                "INSERT INTO users(tg,yt,login,name,zone,connected,binding) VALUES(?,?,?,?,?,?,?)",
                (
                    tg,
                    me["id"],
                    me["login"],
                    me.get("fullName") or me["login"],
                    zone,
                    time.time(),
                    uuid.uuid4().hex,
                ),
            )
            self._tokens(tg, tokens)
            self.conn.execute("DELETE FROM conversations WHERE tg=?", (tg,))
            self.conn.execute("DELETE FROM actions WHERE tg=?", (tg,))

    def _tokens(self, tg, tokens):
        self.conn.execute(
            "INSERT OR REPLACE INTO credentials VALUES(?,?,?,?)",
            (
                tg,
                self.seal(tokens["access_token"]),
                self.seal(tokens.get("refresh_token")),
                time.time() + float(tokens.get("expires_in", 3600)),
            ),
        )

    def save_tokens(self, tg, tokens):
        with self.conn:
            old = self.one("SELECT refresh FROM credentials WHERE tg=?", (tg,))
            if not old:
                raise ValueError("Account disconnected")
            if not tokens.get("refresh_token"):
                tokens["refresh_token"] = self.open(old["refresh"])
            self._tokens(tg, tokens)

    def disconnect(self, tg):
        with self.conn:
            self.conn.execute("DELETE FROM users WHERE tg=?", (tg,))
            for table in ("oauth", "actions", "conversations"):
                self.conn.execute(f"DELETE FROM {table} WHERE tg=?", (tg,))
        # Operation ledger is retained to reconcile ambiguous writes, not a time-history cache.

    def new_oauth(self, tg):
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        with self.conn:
            self.conn.execute("DELETE FROM oauth WHERE tg=? OR expires<?", (tg, time.time()))
            self.conn.execute(
                "INSERT INTO oauth(state,tg,verifier,expires) VALUES(?,?,?,?)",
                (digest(state), tg, self.seal(verifier), time.time() + 600),
            )
        return state

    def start_oauth(self, state, cookie):
        cur = self.execute(
            "UPDATE oauth SET cookie=? WHERE state=? AND cookie IS NULL AND used=0 AND expires>?",
            (digest(cookie), digest(state), time.time()),
        )
        if cur.rowcount != 1:
            raise ValueError("Ссылка истекла или уже использована. Отправь /start боту.")
        return self.one("SELECT * FROM oauth WHERE state=?", (digest(state),))

    def consume_oauth(self, state, cookie):
        with self.conn:
            row = self.one(
                "SELECT * FROM oauth WHERE state=? AND cookie=? AND used=0 AND expires>?",
                (digest(state), digest(cookie), time.time()),
            )
            if not row:
                raise ValueError("Сессия подключения истекла или уже использована.")
            self.conn.execute("UPDATE oauth SET used=1 WHERE state=?", (digest(state),))
            return row

    def action(self, tg, kind, data=None):
        user = self.user(tg)
        token = secrets.token_urlsafe(12)
        self.execute(
            "INSERT INTO actions VALUES(?,?,?,?,?,?)",
            (
                token,
                tg,
                user["binding"] if user else "",
                kind,
                json.dumps(data or {}, ensure_ascii=False),
                time.time() + 86400,
            ),
        )
        return token

    def get_action(self, token, tg):
        row = self.one(
            "SELECT * FROM actions WHERE token=? AND tg=? AND expires>?", (token, tg, time.time())
        )
        user = self.user(tg)
        if not row or not user or row["binding"] != user["binding"]:
            raise ValueError("Кнопка устарела. Открой /tasks заново.")
        row["data"] = json.loads(row["data"])
        return row

    def conversation(self, tg):
        row = self.one("SELECT * FROM conversations WHERE tg=?", (tg,))
        if row:
            row["data"] = json.loads(row["data"])
        return row

    def set_conversation(self, tg, mode, data=None, flow=None):
        flow = flow or uuid.uuid4().hex
        self.execute(
            "INSERT OR REPLACE INTO conversations VALUES(?,?,?,?)",
            (tg, flow, mode, json.dumps(data or {}, ensure_ascii=False)),
        )
        return flow

    def clear_conversation(self, tg):
        self.execute("DELETE FROM conversations WHERE tg=?", (tg,))

    def free_entry(self, tg, flow):
        return self.one("SELECT * FROM free_entries WHERE tg=? AND flow=?", (tg, flow))

    def save_free_entry(self, tg, flow, description, minutes, day, zone):
        # No users FK: history must survive OAuth disconnect/rebinding.
        with self.conn:
            existing = self.free_entry(tg, flow)
            if existing:
                return existing
            self.conn.execute(
                "INSERT INTO free_entries VALUES(?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, flow, tg, description, minutes, day, zone, time.time()),
            )
            self.conn.execute("DELETE FROM conversations WHERE tg=? AND flow=?", (tg, flow))
        return self.free_entry(tg, flow)

    def free_month(self, tg, start, end):
        return self.all(
            "SELECT * FROM free_entries WHERE tg=? AND day>=? AND day<? ORDER BY day,created,id",
            (tg, start, end),
        )

    def prepare_operation(self, flow, tg, issue, minutes, day):
        user = self.user(tg)
        with self.conn:
            existing = self.one("SELECT * FROM operations WHERE flow=?", (flow,))
            if existing:
                return existing
            uncertain = self.one(
                "SELECT * FROM operations WHERE yt=? AND issue=? AND status IN ('sending','uncertain','prepared')",
                (user["yt"], issue),
            )
            if uncertain:
                return uncertain
            op_id = uuid.uuid4().hex
            self.conn.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)",
                (op_id, flow, tg, user["yt"], issue, minutes, day, "prepared", None, time.time()),
            )
        return self.one("SELECT * FROM operations WHERE id=?", (op_id,))

    def mark_operation(self, op, status, work_id=None):
        self.execute("UPDATE operations SET status=?,work_id=? WHERE id=?", (status, work_id, op))

    def claim_notification(self, tg, day):
        return (
            self.execute(
                "INSERT OR IGNORE INTO notifications VALUES(?,?,'claimed')", (tg, day)
            ).rowcount
            == 1
        )

    def cleanup(self):
        self.execute("DELETE FROM actions WHERE expires<?", (time.time(),))
        self.execute("DELETE FROM oauth WHERE expires<?", (time.time(),))

    def close(self):
        self.conn.close()
