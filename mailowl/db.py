"""SQLite 存储。每个线程一个连接（WAL），主进程与 MCP 子进程共享同一个库文件。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS mails(
  id INTEGER PRIMARY KEY,
  account TEXT NOT NULL,
  folder TEXT NOT NULL,
  remote_uid TEXT NOT NULL,
  direction TEXT NOT NULL DEFAULT 'in',      -- in | out
  message_id TEXT, in_reply_to TEXT, refs TEXT, thread_key TEXT,
  from_addr TEXT, from_name TEXT, to_addrs TEXT, cc_addrs TEXT, reply_to TEXT,
  subject TEXT, date_ts INTEGER, size INTEGER,
  body_text TEXT, body_truncated INTEGER NOT NULL DEFAULT 0,
  attachments TEXT NOT NULL DEFAULT '[]',
  fetched_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',       -- history | open | handled | ignored
  level INTEGER, category TEXT, summary TEXT, analysis TEXT, deadline TEXT,
  needs_reply INTEGER, triage TEXT, triaged_at INTEGER, triage_error TEXT,
  local_only INTEGER NOT NULL DEFAULT 0,
  route TEXT,                                -- immediate | digest | none
  notified_at INTEGER, digested_at INTEGER, acked_at INTEGER,
  UNIQUE(account, folder, remote_uid)
);
CREATE INDEX IF NOT EXISTS mails_msgid ON mails(message_id);
CREATE INDEX IF NOT EXISTS mails_thread ON mails(thread_key);
CREATE INDEX IF NOT EXISTS mails_from ON mails(from_addr);
CREATE INDEX IF NOT EXISTS mails_date ON mails(date_ts);

CREATE VIRTUAL TABLE IF NOT EXISTS mails_fts USING fts5(
  subject, body_text, from_addr, from_name,
  content='mails', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS mails_ai AFTER INSERT ON mails BEGIN
  INSERT INTO mails_fts(rowid, subject, body_text, from_addr, from_name)
  VALUES (new.id, new.subject, new.body_text, new.from_addr, new.from_name);
END;
CREATE TRIGGER IF NOT EXISTS mails_ad AFTER DELETE ON mails BEGIN
  INSERT INTO mails_fts(mails_fts, rowid, subject, body_text, from_addr, from_name)
  VALUES ('delete', old.id, old.subject, old.body_text, old.from_addr, old.from_name);
END;
CREATE TRIGGER IF NOT EXISTS mails_au AFTER UPDATE OF subject, body_text, from_addr, from_name ON mails BEGIN
  INSERT INTO mails_fts(mails_fts, rowid, subject, body_text, from_addr, from_name)
  VALUES ('delete', old.id, old.subject, old.body_text, old.from_addr, old.from_name);
  INSERT INTO mails_fts(rowid, subject, body_text, from_addr, from_name)
  VALUES (new.id, new.subject, new.body_text, new.from_addr, new.from_name);
END;

CREATE TABLE IF NOT EXISTS sync_state(account TEXT, folder TEXT, key TEXT, value TEXT,
  PRIMARY KEY(account, folder, key));
CREATE TABLE IF NOT EXISTS pop_seen(account TEXT, uidl TEXT, PRIMARY KEY(account, uidl));

CREATE TABLE IF NOT EXISTS feishu_msgs(
  message_id TEXT PRIMARY KEY, mail_id INTEGER, draft_id INTEGER, kind TEXT, created_at INTEGER);

CREATE TABLE IF NOT EXISTS drafts(
  id INTEGER PRIMARY KEY,
  chain_id INTEGER,                          -- 同一封回复的各个版本共享 chain_id（= 首版 id）
  version INTEGER NOT NULL,
  mail_id INTEGER, account TEXT NOT NULL,
  to_addrs TEXT, cc_addrs TEXT, subject TEXT, body TEXT,
  in_reply_to TEXT, refs TEXT,
  status TEXT NOT NULL DEFAULT 'draft',      -- draft | superseded | sending | sent | failed | unknown | cancelled
  origin_scope TEXT, note TEXT,
  created_at INTEGER, sent_at INTEGER, smtp_message_id TEXT, error TEXT,
  feishu_message_id TEXT);
CREATE INDEX IF NOT EXISTS drafts_chain ON drafts(chain_id);

CREATE TABLE IF NOT EXISTS draft_files(
  id INTEGER PRIMARY KEY, chain_id INTEGER NOT NULL, filename TEXT NOT NULL, path TEXT NOT NULL,
  size INTEGER NOT NULL, source TEXT, created_at INTEGER);
CREATE INDEX IF NOT EXISTS draft_files_chain ON draft_files(chain_id);

CREATE TABLE IF NOT EXISTS agent_sessions(
  scope TEXT PRIMARY KEY, session_id TEXT, created_at INTEGER, last_used INTEGER,
  turns INTEGER NOT NULL DEFAULT 0);

-- openai 后端自己保存对话历史（claude_cli 后端由 Claude Code 自己保存会话）
CREATE TABLE IF NOT EXISTS agent_history(
  session_id TEXT PRIMARY KEY, scope TEXT, messages TEXT NOT NULL, updated_at INTEGER);

-- 对话 Agent 申请上网查资料：用户在飞书卡片上同意后，由读不到邮箱的研究 Agent 执行
CREATE TABLE IF NOT EXISTS research_requests(
  id INTEGER PRIMARY KEY, scope TEXT NOT NULL, mail_id INTEGER, question TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',     -- pending | running | done | failed | rejected
  answer TEXT, error TEXT, card_msg_id TEXT, created_at INTEGER, finished_at INTEGER);

CREATE TABLE IF NOT EXISTS todos(
  id INTEGER PRIMARY KEY, mail_id INTEGER, title TEXT, due_ts INTEGER,
  status TEXT NOT NULL DEFAULT 'open',       -- open | done | cancelled | expired（过期一天后自动收起）
  reminded INTEGER NOT NULL DEFAULT 0, announced INTEGER NOT NULL DEFAULT 0,
  origin_scope TEXT, created_at INTEGER, closed_at INTEGER);

CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY, mail_id INTEGER, channel TEXT, kind TEXT, sent_at INTEGER);
CREATE INDEX IF NOT EXISTS notif_time ON notifications(channel, sent_at);

CREATE TABLE IF NOT EXISTS sender_prefs(addr TEXT PRIMARY KEY, weight INTEGER, note TEXT, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS actions(id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, mail_id INTEGER, detail TEXT);
"""

MAIL_COLUMNS = (
    "account", "folder", "remote_uid", "direction", "message_id", "in_reply_to", "refs",
    "thread_key", "from_addr", "from_name", "to_addrs", "cc_addrs", "reply_to", "subject",
    "date_ts", "size", "body_text", "body_truncated", "attachments", "fetched_at", "status",
)


def now() -> int:
    return int(time.time())


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """给旧数据库补新列（CREATE TABLE IF NOT EXISTS 不会改已有表）。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(drafts)")}
        for col in ("body_main", "sig"):                # 落款与正文分开存，卡片上可一键换落款
            if col not in cols:
                self.conn.execute(f"ALTER TABLE drafts ADD COLUMN {col} TEXT")
        if "closed_at" not in {r[1] for r in self.conn.execute("PRAGMA table_info(todos)")}:
            self.conn.execute("ALTER TABLE todos ADD COLUMN closed_at INTEGER")   # 问「最近完成了什么」时按它查

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    def tx(self):
        return _Tx(self.conn)

    # --- 通用 ---
    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(args)).fetchall()

    def q1(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(args)).fetchone()

    def x(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(args))

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        r = self.q1("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else default

    def kv_set(self, key: str, value: str | None) -> None:
        if value is None:
            self.x("DELETE FROM kv WHERE key=?", (key,))
        else:
            self.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))

    def log_action(self, kind: str, mail_id: int | None = None, detail: Any = None) -> None:
        self.x("INSERT INTO actions(ts,kind,mail_id,detail) VALUES(?,?,?,?)",
               (now(), kind, mail_id, json.dumps(detail, ensure_ascii=False) if detail is not None else None))

    # --- 同步状态 ---
    def sync_get(self, account: str, folder: str, key: str) -> str | None:
        r = self.q1("SELECT value FROM sync_state WHERE account=? AND folder=? AND key=?", (account, folder, key))
        return r["value"] if r else None

    def sync_set(self, account: str, folder: str, key: str, value: str) -> None:
        self.x("INSERT INTO sync_state VALUES(?,?,?,?) ON CONFLICT(account,folder,key) DO UPDATE SET value=excluded.value",
               (account, folder, key, value))

    # --- 邮件 ---
    def insert_mail(self, rec: dict[str, Any]) -> int | None:
        """插入一封邮件；(account, folder, remote_uid) 已存在时返回 None。"""
        rec = dict(rec)
        rec.setdefault("fetched_at", now())
        rec.setdefault("status", "open")
        rec.setdefault("direction", "in")
        if isinstance(rec.get("attachments"), (list, tuple)):
            rec["attachments"] = json.dumps(rec["attachments"], ensure_ascii=False)
        cols = [c for c in MAIL_COLUMNS if c in rec]
        sql = (f"INSERT OR IGNORE INTO mails({','.join(cols)}) "
               f"VALUES({','.join('?' for _ in cols)})")
        cur = self.x(sql, [rec[c] for c in cols])
        return cur.lastrowid if cur.rowcount else None

    def mail(self, mail_id: int) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM mails WHERE id=?", (mail_id,))

    def update_mail(self, mail_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE mails SET {sets} WHERE id=?", [*fields.values(), mail_id])
        if fields.get("status") in ("handled", "ignored"):
            # 邮件处理完或标为忽略：由它自动生成的待办（分级/卡片按钮建的）一并完成；对话里让 Agent 建的不动
            self.x("UPDATE todos SET status='done', closed_at=? WHERE mail_id=? AND status='open' "
                   "AND origin_scope IN ('auto','card')", (now(), mail_id))

    def thread(self, mail_id: int, limit: int = 20) -> list[sqlite3.Row]:
        """同一线程的邮件（含我方发出的），按时间升序。线程识别：thread_key、Message-ID 互引。"""
        m = self.mail(mail_id)
        if not m:
            return []
        keys = {k for k in (m["thread_key"], m["message_id"]) if k}
        if not keys:
            return [m]
        ph = ",".join("?" for _ in keys)
        rows = self.q(
            f"SELECT * FROM mails WHERE thread_key IN ({ph}) OR message_id IN ({ph}) "
            f"OR in_reply_to IN ({ph}) ORDER BY date_ts DESC LIMIT ?",
            [*keys, *keys, *keys, limit])
        return sorted(rows, key=lambda r: (r["date_ts"] or 0, r["id"]))

    def search(self, query: str = "", sender: str = "", account: str = "", since_ts: int = 0,
               until_ts: int = 0, direction: str = "", limit: int = 20) -> list[sqlite3.Row]:
        where, args = [], []
        if query:
            q = query.strip()
            if len(q) >= 3:
                where.append("m.id IN (SELECT rowid FROM mails_fts WHERE mails_fts MATCH ?)")
                args.append('"' + q.replace('"', '""') + '"')
            else:  # trigram 至少 3 个字符，更短的退回 LIKE
                where.append("(m.subject LIKE ? OR m.body_text LIKE ?)")
                args += [f"%{q}%", f"%{q}%"]
        if sender:
            where.append("(m.from_addr LIKE ? OR m.from_name LIKE ? OR m.to_addrs LIKE ?)")
            args += [f"%{sender}%"] * 3
        if account:
            where.append("m.account=?")
            args.append(account)
        if since_ts:
            where.append("m.date_ts>=?")
            args.append(since_ts)
        if until_ts:
            where.append("m.date_ts<=?")
            args.append(until_ts)
        if direction:
            where.append("m.direction=?")
            args.append(direction)
        sql = "SELECT m.* FROM mails m"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY m.date_ts DESC LIMIT ?"
        args.append(limit)
        return self.q(sql, args)

    def sender_stats(self, addr: str) -> dict[str, Any]:
        """该发件人的历史：来信数、我方回复数、最近一次来信。用于判断重要性与处理习惯。"""
        if not addr:
            return {"received": 0, "replied": 0}
        r = self.q1("SELECT COUNT(*) n, MAX(date_ts) last FROM mails WHERE from_addr=? AND direction='in'", (addr,))
        s = self.q1("SELECT COUNT(*) n FROM mails WHERE direction='out' AND to_addrs LIKE ?", (f"%{addr}%",))
        ign = self.q1("SELECT COUNT(*) n FROM mails WHERE from_addr=? AND status='ignored'", (addr,))
        pref = self.q1("SELECT weight, note FROM sender_prefs WHERE addr=?", (addr,))
        return {
            "received": r["n"], "last_ts": r["last"], "replied": s["n"], "ignored": ign["n"],
            "pref": pref["weight"] if pref else 0, "pref_note": pref["note"] if pref else None,
        }

    def set_sender_pref(self, addr: str, weight: int, note: str | None = None) -> None:
        self.x("INSERT INTO sender_prefs VALUES(?,?,?,?) ON CONFLICT(addr) DO UPDATE SET "
               "weight=excluded.weight, note=COALESCE(excluded.note, note), updated_at=excluded.updated_at",
               (addr.lower(), weight, note, now()))

    # --- 飞书消息 ↔ 邮件 ---
    def bind_feishu(self, message_id: str | None, kind: str, mail_id: int | None = None,
                    draft_id: int | None = None) -> None:
        if not message_id:
            return
        self.x("INSERT OR REPLACE INTO feishu_msgs VALUES(?,?,?,?,?)", (message_id, mail_id, draft_id, kind, now()))

    def feishu_binding(self, message_id: str | None) -> sqlite3.Row | None:
        if not message_id:
            return None
        return self.q1("SELECT * FROM feishu_msgs WHERE message_id=?", (message_id,))

    # --- 草稿 ---
    def create_draft(self, *, account: str, mail_id: int | None, to_addrs: str, cc_addrs: str,
                     subject: str, body: str, in_reply_to: str | None, refs: str | None,
                     origin_scope: str | None, note: str | None = None,
                     body_main: str | None = None, sig: str | None = None) -> sqlite3.Row:
        """新建草稿；若该邮件已有未发送的草稿链，则作为新版本追加，旧版本置为 superseded。"""
        with self.tx() as c:
            prev = None
            if mail_id is not None:
                prev = c.execute(
                    "SELECT * FROM drafts WHERE mail_id=? AND status IN ('draft','failed') "
                    "ORDER BY id DESC LIMIT 1", (mail_id,)).fetchone()
            if prev is not None:
                chain_id, version = prev["chain_id"], prev["version"] + 1
                c.execute("UPDATE drafts SET status='superseded' WHERE chain_id=? AND status IN ('draft','failed')",
                          (chain_id,))
            else:
                chain_id, version = None, 1
            cur = c.execute(
                "INSERT INTO drafts(chain_id,version,mail_id,account,to_addrs,cc_addrs,subject,body,in_reply_to,"
                "refs,status,origin_scope,note,created_at,body_main,sig) VALUES(?,?,?,?,?,?,?,?,?,?,'draft',?,?,?,?,?)",
                (chain_id, version, mail_id, account, to_addrs, cc_addrs, subject, body, in_reply_to, refs,
                 origin_scope, note, now(), body_main, sig))
            did = cur.lastrowid
            if chain_id is None:
                c.execute("UPDATE drafts SET chain_id=? WHERE id=?", (did, did))
            return c.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()

    def draft(self, draft_id: int) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM drafts WHERE id=?", (draft_id,))

    def latest_draft(self, mail_id: int) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM drafts WHERE mail_id=? AND status NOT IN ('superseded','cancelled') "
                       "ORDER BY id DESC LIMIT 1", (mail_id,))

    def claim_draft_for_send(self, draft_id: int) -> bool:
        """原子地把草稿置为 sending。只有最新版本、状态为 draft/failed、且整条链没有发出或发送中的
        版本时才成功——这是防止重复发送的唯一闸门。"""
        with self.tx() as c:
            d = c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if d is None or d["status"] not in ("draft", "failed"):
                return False
            busy = c.execute("SELECT 1 FROM drafts WHERE chain_id=? AND status IN ('sending','sent','unknown')",
                             (d["chain_id"],)).fetchone()
            newest = c.execute("SELECT MAX(version) v FROM drafts WHERE chain_id=?", (d["chain_id"],)).fetchone()["v"]
            if busy or d["version"] != newest:
                return False
            cur = c.execute("UPDATE drafts SET status='sending', error=NULL WHERE id=? AND status IN ('draft','failed')",
                            (draft_id,))
            return cur.rowcount == 1

    # --- 会话 ---
    def session(self, scope: str) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM agent_sessions WHERE scope=?", (scope,))

    def save_session(self, scope: str, session_id: str) -> None:
        self.x("INSERT INTO agent_sessions(scope,session_id,created_at,last_used,turns) VALUES(?,?,?,?,1) "
               "ON CONFLICT(scope) DO UPDATE SET session_id=excluded.session_id, last_used=excluded.last_used, "
               "turns=CASE WHEN session_id=excluded.session_id THEN turns+1 ELSE 1 END",
               (scope, session_id, now(), now()))

    def drop_session(self, scope: str) -> None:
        self.x("DELETE FROM agent_sessions WHERE scope=?", (scope,))


class _Tx:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False
