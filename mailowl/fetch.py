"""增量收信。IMAP 以只读方式 SELECT 并用 BODY.PEEK，不会把邮件标为已读；POP3 从不 DELE。

首次接入某个文件夹时回填最近 backfill_days 天作为历史（status='history'，不推送、不分级），
之后只有新到的邮件才进入分级与通知流程。
"""
from __future__ import annotations

import datetime as dt
import imaplib
import logging
import poplib
import re
import ssl
import time
from typing import Callable

from .config import Account
from .db import DB
from .mailparse import parse_message

log = logging.getLogger(__name__)

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
PARTIAL_TEXT_BYTES = 300_000
HISTORY_FULL_BYTES = 300_000  # 回填历史时超过这个大小只取头部和正文前段，附件真要用时再按需取回（省代理流量）
POP3_TOP_LINES = 400

imaplib.Commands.setdefault("ID", ("NONAUTH", "AUTH", "SELECTED"))


def _imap_date(ts: float) -> str:
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def _imap_utf7(name: str) -> str:
    """RFC 3501 modified UTF-7：中文文件夹名（如 163 的「已发送」）必须这样编码。"""
    import base64
    out, buf = [], []

    def flush():
        if buf:
            b = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append(f"&{b}-")
            buf.clear()
    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def imap_utf7_decode(name: str) -> str:
    import base64

    def dec(m):
        b = m.group(1)
        if not b:
            return "&"
        b = b.replace(",", "/")
        return base64.b64decode(b + "=" * (-len(b) % 4)).decode("utf-16-be")
    return re.sub(r"&([A-Za-z0-9+,]*)-", dec, name)


def _quote_mailbox(name: str) -> str:
    if name.startswith('"'):
        return name
    name = _imap_utf7(name)
    if re.search(r'[\s"()\[\]{}%*\\]', name):
        return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return name


def _payload(data) -> bytes:
    """把 imaplib FETCH 返回的 [(meta, bytes), b')', ...] 拼成字节串。"""
    out = b""
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            out += bytes(item[1])
    return out


def _resp_int(M: imaplib.IMAP4, code: str) -> int | None:
    try:
        _, vals = M.response(code)
    except Exception:
        return None
    for v in vals or []:
        if v:
            m = re.search(rb"\d+", v if isinstance(v, bytes) else str(v).encode())
            if m:
                return int(m.group())
    return None


def _placeholder(uid: str, err: Exception) -> dict:
    return {"subject": f"(解析失败: {type(err).__name__})", "from_addr": "", "from_name": "",
            "body_text": "", "date_ts": int(time.time()), "thread_key": f"broken:{uid}", "attachments": []}


class IMAPSync:
    def __init__(self, acct: Account, db: DB, max_message_bytes: int,
                 connect: Callable[[Account], imaplib.IMAP4] | None = None):
        self.acct, self.db, self.max_bytes = acct, db, max_message_bytes
        self._connect = connect or self.connect

    @staticmethod
    def connect(acct: Account) -> imaplib.IMAP4:
        ctx = ssl.create_default_context()
        if acct.port == 143:
            M = imaplib.IMAP4(acct.host, acct.port, timeout=60)
            M.starttls(ssl_context=ctx)
        else:
            M = imaplib.IMAP4_SSL(acct.host, acct.port, ssl_context=ctx, timeout=60)
        M.login(acct.username, acct.password)
        if acct.imap_id:
            M._simple_command("ID", '("name" "MailOwl" "version" "0.1" "vendor" "MailOwl")')
        return M

    def run(self) -> list[int]:
        """返回本次新入库、需要分级的邮件 id。"""
        M = self._connect(self.acct)
        new_ids: list[int] = []
        try:
            for folder in self.acct.folders:
                new_ids += self.sync_folder(M, folder, "in")
            if self.acct.sent_folder:
                self.sync_folder(M, self.acct.sent_folder, "out")
        finally:
            try:
                M.logout()
            except Exception:
                pass
        return new_ids

    def sync_folder(self, M: imaplib.IMAP4, folder: str, direction: str) -> list[int]:
        a = self.acct
        typ, data = M.select(_quote_mailbox(folder), readonly=True)
        if typ != "OK":
            raise RuntimeError(f"{a.name}: 无法打开文件夹 {folder}: {data}")
        uidvalidity = _resp_int(M, "UIDVALIDITY") or 0
        uidnext = _resp_int(M, "UIDNEXT")
        saved_v = self.db.sync_get(a.name, folder, "uidvalidity")
        last = int(self.db.sync_get(a.name, folder, "last_uid") or 0)
        first = saved_v is None or int(saved_v) != uidvalidity
        if first:
            if saved_v is not None:
                log.warning("%s/%s UIDVALIDITY 变化 (%s → %s)，按首次接入重新回填", a.name, folder, saved_v, uidvalidity)
            since = _imap_date(time.time() - a.backfill_days * 86400)
            uids = self._search(M, "SINCE", since)[-a.backfill_max:]
            status = "history"
            last = 0
        else:
            uids = [u for u in self._search(M, "UID", f"{last + 1}:*") if u > last]
            status = "open" if direction == "in" else "history"
        sizes = self._sizes(M, uids)
        limit = min(self.max_bytes, HISTORY_FULL_BYTES) if first else self.max_bytes
        if first and uids:
            est = sum(min(sizes.get(u, 0), limit + PARTIAL_TEXT_BYTES) for u in uids)
            log.info("%s/%s 首次接入：回填 %d 封，最多下载约 %.1f MB", a.name, folder, len(uids), est / 2**20)
        new_ids = []
        for uid in uids:
            try:
                raw, truncated = self._fetch(M, uid, sizes.get(uid, 0), limit)
                rec = parse_message(raw, truncated=truncated)
            except (imaplib.IMAP4.abort, OSError):
                raise
            except Exception as e:  # 单封坏信不能卡住整个邮箱
                log.exception("%s/%s UID %s 解析失败", a.name, folder, uid)
                rec = _placeholder(str(uid), e)
            rec.update(account=a.name, folder=folder, remote_uid=f"{uidvalidity}:{uid}",
                       direction=direction, status=status)
            mid = self.db.insert_mail(rec)
            if mid and status == "open":
                new_ids.append(mid)
            if uid > last:
                last = uid
                self.db.sync_set(a.name, folder, "last_uid", str(last))
        if first:
            top = (uidnext - 1) if uidnext else max(self._search(M, "UID", "*") or [last])
            last = max(last, top)
            self.db.sync_set(a.name, folder, "last_uid", str(last))
            self.db.sync_set(a.name, folder, "uidvalidity", str(uidvalidity))
            log.info("%s/%s 首次接入：回填 %d 封历史邮件", a.name, folder, len(uids))
        return new_ids

    @staticmethod
    def _search(M: imaplib.IMAP4, *criteria: str) -> list[int]:
        typ, data = M.uid("SEARCH", None, *criteria)
        if typ != "OK":
            raise RuntimeError(f"UID SEARCH 失败: {data}")
        return sorted({int(x) for chunk in data if chunk for x in chunk.split()})

    @staticmethod
    def _sizes(M: imaplib.IMAP4, uids: list[int]) -> dict[int, int]:
        out: dict[int, int] = {}
        for i in range(0, len(uids), 200):
            chunk = ",".join(map(str, uids[i:i + 200]))
            typ, data = M.uid("FETCH", chunk, "(UID RFC822.SIZE)")
            for item in data or []:
                line = item[0] if isinstance(item, tuple) else item
                if not isinstance(line, (bytes, bytearray)):
                    continue
                mu, ms = re.search(rb"UID (\d+)", line), re.search(rb"RFC822\.SIZE (\d+)", line)
                if mu and ms:
                    out[int(mu.group(1))] = int(ms.group(1))
        return out

    def _fetch(self, M: imaplib.IMAP4, uid: int, size: int, limit: int) -> tuple[bytes, bool]:
        if size and size > limit:
            typ, data = M.uid("FETCH", str(uid), f"(BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.{PARTIAL_TEXT_BYTES}>)")
            truncated = True
        else:
            typ, data = M.uid("FETCH", str(uid), "(BODY.PEEK[])")
            truncated = False
        if typ != "OK":
            raise RuntimeError(f"FETCH UID {uid} 失败: {data}")
        raw = _payload(data)
        if not raw:
            raise RuntimeError(f"FETCH UID {uid} 返回空内容")
        return raw, truncated


class POP3Sync:
    def __init__(self, acct: Account, db: DB, max_message_bytes: int,
                 connect: Callable[[Account], poplib.POP3] | None = None):
        self.acct, self.db, self.max_bytes = acct, db, max_message_bytes
        self._connect = connect or self.connect

    @staticmethod
    def connect(acct: Account) -> poplib.POP3:
        ctx = ssl.create_default_context()
        if acct.port == 110:
            M = poplib.POP3(acct.host, acct.port, timeout=60)
            M.stls(context=ctx)
        else:
            M = poplib.POP3_SSL(acct.host, acct.port, timeout=60, context=ctx)
        M.user(acct.username)
        M.pass_(acct.password)
        return M

    def run(self) -> list[int]:
        a = self.acct
        M = self._connect(a)
        try:
            _, uidl_lines, _ = M.uidl()
            _, list_lines, _ = M.list()
            sizes = {}
            for line in list_lines:
                parts = line.split()
                if len(parts) >= 2:
                    sizes[int(parts[0])] = int(parts[1])
            pairs = []
            for line in uidl_lines:
                parts = line.split()
                if len(parts) >= 2:
                    pairs.append((int(parts[0]), parts[1].decode("ascii", "replace")))
            seen = {r["uidl"] for r in self.db.q("SELECT uidl FROM pop_seen WHERE account=?", (a.name,))}
            fresh = [(n, u) for n, u in sorted(pairs) if u not in seen]
            first = self.db.sync_get(a.name, "INBOX", "pop3_initialized") is None
            if first:
                to_fetch = fresh[-a.backfill_max:]
                skipped = fresh[:-a.backfill_max] if len(fresh) > a.backfill_max else []
                with self.db.tx() as c:
                    c.executemany("INSERT OR IGNORE INTO pop_seen VALUES(?,?)", [(a.name, u) for _, u in skipped])
                status = "history"
            else:
                to_fetch, status = fresh, "open"
            new_ids = []
            cutoff = time.time() - a.backfill_days * 86400
            limit = min(self.max_bytes, HISTORY_FULL_BYTES) if first else self.max_bytes
            for num, uidl in to_fetch:
                try:
                    if sizes.get(num, 0) > limit:
                        _, lines, _ = M.top(num, POP3_TOP_LINES)
                        truncated = True
                    else:
                        _, lines, _ = M.retr(num)
                        truncated = False
                    rec = parse_message(b"\r\n".join(lines), truncated=truncated)
                except (poplib.error_proto, OSError):
                    raise
                except Exception as e:
                    log.exception("%s POP3 #%s 解析失败", a.name, num)
                    rec = _placeholder(uidl, e)
                st = status
                if first and rec.get("date_ts", 0) < cutoff:
                    st = "history"
                rec.update(account=a.name, folder="INBOX", remote_uid=uidl, direction="in", status=st)
                mid = self.db.insert_mail(rec)
                self.db.x("INSERT OR IGNORE INTO pop_seen VALUES(?,?)", (a.name, uidl))
                if mid and st == "open":
                    new_ids.append(mid)
            if first:
                self.db.sync_set(a.name, "INBOX", "pop3_initialized", str(int(time.time())))
                log.info("%s 首次接入（POP3）：回填 %d 封历史邮件，跳过 %d 封更早的", a.name, len(to_fetch),
                         len(fresh) - len(to_fetch))
            return new_ids
        finally:
            try:
                M.quit()
            except Exception:
                pass


def sync_account(acct: Account, db: DB, max_message_bytes: int) -> list[int]:
    cls = IMAPSync if acct.protocol == "imap" else POP3Sync
    return cls(acct, db, max_message_bytes).run()
