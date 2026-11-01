"""草稿附件：来源可以是历史邮件里的附件（按需从服务器重新取回），也可以是用户在飞书里发给机器人的文件。

附件挂在草稿链（chain_id）上，修改正文生成新版本时附件保持不变；文件临时存在 data_dir/attachments/chain_<id>/，
草稿链发出或取消后由 purge_chain_files 删除（记录保留，用于卡片显示）。
"""
from __future__ import annotations

import email
import re
import shutil
from email import policy
from pathlib import Path

from .config import Config
from .db import DB, now
from .fetch import IMAPSync, POP3Sync, _payload, _quote_mailbox
from .mailparse import _filename

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024  # 多数邮箱单封上限 25MB，留出编码余量前的原始大小


class AttachmentError(Exception):
    pass


def _safe_name(name: str) -> str:
    name = Path(name or "file").name
    name = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", name).strip(". ") or "file"
    return name[:120]


def chain_files(db: DB, chain_id: int):
    return db.q("SELECT * FROM draft_files WHERE chain_id=? ORDER BY id", (chain_id,))


def store_file(cfg: Config, db: DB, chain_id: int, filename: str, data: bytes, source: str) -> dict:
    if len(data) > MAX_FILE_BYTES:
        raise AttachmentError(f"{filename} 有 {len(data) / 2**20:.1f} MB，超过单个附件 20 MB 上限")
    total = sum(r["size"] for r in chain_files(db, chain_id)) + len(data)
    if total > MAX_TOTAL_BYTES:
        raise AttachmentError(f"附件总大小将达到 {total / 2**20:.1f} MB，超过邮箱单封约 25 MB 的上限")
    d = cfg.data_dir / "attachments" / f"chain_{chain_id}"
    d.mkdir(parents=True, exist_ok=True)
    name = _safe_name(filename)
    path = d / name
    n = 1
    while path.exists():
        path = d / f"{Path(name).stem}-{n}{Path(name).suffix}"
        n += 1
    path.write_bytes(data)
    cur = db.x("INSERT INTO draft_files(chain_id,filename,path,size,source,created_at) VALUES(?,?,?,?,?,?)",
               (chain_id, path.name, str(path), len(data), source, now()))
    return {"id": cur.lastrowid, "filename": path.name, "size": len(data)}


def remove_file(db: DB, chain_id: int, filename: str) -> bool:
    r = db.q1("SELECT * FROM draft_files WHERE chain_id=? AND filename=?", (chain_id, filename))
    if not r:
        return False
    try:
        Path(r["path"]).unlink()
    except FileNotFoundError:
        pass
    db.x("DELETE FROM draft_files WHERE id=?", (r["id"],))
    return True


def purge_chain_files(cfg: Config, db: DB, chain_id: int) -> int:
    """草稿链到达终态（已发出 / 已取消）后删除本地附件文件；draft_files 记录保留给卡片显示。"""
    n = 0
    for r in chain_files(db, chain_id):
        try:
            Path(r["path"]).unlink()
            n += 1
        except FileNotFoundError:
            pass
    shutil.rmtree(cfg.data_dir / "attachments" / f"chain_{chain_id}", ignore_errors=True)
    return n


def _raw_from_server(cfg: Config, m) -> bytes:
    acct = cfg.account(m["account"])
    if acct.protocol == "imap":
        validity, _, uid = m["remote_uid"].partition(":")
        M = IMAPSync.connect(acct)
        try:
            typ, _ = M.select(_quote_mailbox(m["folder"]), readonly=True)
            if typ != "OK":
                raise AttachmentError(f"打不开文件夹 {m['folder']}")
            typ, data = M.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = _payload(data)
        finally:
            try:
                M.logout()
            except Exception:
                pass
    else:
        M = POP3Sync.connect(acct)
        try:
            _, lines, _ = M.uidl()
            num = next((int(l.split()[0]) for l in lines if l.split()[1].decode() == m["remote_uid"]), None)
            if num is None:
                raise AttachmentError("这封邮件已不在 POP3 服务器上")
            _, body, _ = M.retr(num)
            raw = b"\r\n".join(body)
        finally:
            try:
                M.quit()
            except Exception:
                pass
    if not raw:
        raise AttachmentError("服务器没有返回邮件内容（可能已被删除或移动）")
    return raw


def attachment_from_mail(cfg: Config, db: DB, mail_id: int, filename: str) -> tuple[str, bytes]:
    """从某封历史邮件取回指定附件（按文件名，忽略大小写，也接受部分匹配）。"""
    m = db.mail(mail_id)
    if m is None:
        raise AttachmentError(f"没有 #{mail_id}")
    if m["folder"] == "mailowl-sent":  # 本程序发出的邮件：附件发出后已删，改从「已发送」文件夹里的同一封取
        alt = db.q1("SELECT * FROM mails WHERE message_id=? AND folder!='mailowl-sent' ORDER BY id LIMIT 1",
                    (m["message_id"],)) if m["message_id"] else None
        if alt is None:
            raise AttachmentError(f"#{mail_id} 是本程序发出的邮件，附件发出后已从本机删除，且「已发送」文件夹里还没有它的副本；"
                                  "请用户重新发送这个文件")
        m = alt
    msg = email.message_from_bytes(_raw_from_server(cfg, m), policy=policy.compat32)
    candidates = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = _filename(part)
        if fn:
            candidates.append((fn, part))
    for exact in (True, False):
        for fn, part in candidates:
            if (fn.lower() == filename.lower()) if exact else (filename.lower() in fn.lower()):
                return fn, part.get_payload(decode=True) or b""
    names = "、".join(fn for fn, _ in candidates) or "无"
    raise AttachmentError(f"#{mail_id} 里没有找到 {filename}（现有附件：{names}）")
