"""IMAP IDLE：和服务器保持一条连接，新邮件一到服务器就主动通知，不用等下一轮轮询。

这里只负责「发现有新邮件」，真正的收信仍走 fetch.IMAPSync（增量、只读、去重），
所以 IDLE 出任何问题都只是退回到定时轮询，不会丢信。

每次 IDLE 用一条独立连接：收到新邮件或到时（默认 25 分钟，RFC 2177 建议 29 分钟内重发）就关掉，
下一轮重新连接。这样不必在 imaplib 的缓冲读取和原始 socket 读取之间来回切换。

不支持 IDLE 的服务器（例如部分 Coremail）退而求其次：保持连接，每 NOOP_SECONDS 秒发一次 NOOP，
看邮件数（EXISTS）有没有增加。NOOP 很轻（不用重新登录），新邮件也能在半分钟内发现。
"""
from __future__ import annotations

import imaplib
import re
import select
import threading
import time
from typing import Callable

from .config import Account
from .fetch import IMAPSync, _quote_mailbox

NEW_MAIL = re.compile(rb"^\* \d+ (EXISTS|RECENT)\b", re.I)
NOOP_SECONDS = 20


class IdleUnsupported(Exception):
    pass


def idle_once(acct: Account, timeout: float, stop: threading.Event | None = None,
              connect: Callable[[Account], imaplib.IMAP4] = IMAPSync.connect, folder: str = "INBOX") -> bool:
    """在 folder 上 IDLE 至多 timeout 秒。有新邮件返回 True，超时或被要求停止返回 False。"""
    M = connect(acct)
    try:
        has_idle = "IDLE" in M.capabilities
        if not has_idle:
            typ, data = M.capability()
            has_idle = typ == "OK" and b"IDLE" in b" ".join(d for d in data if d).upper()
        typ, data = M.select(_quote_mailbox(folder), readonly=True)
        if typ != "OK":
            raise RuntimeError(f"{acct.name}: 无法打开 {folder}: {data}")
        if not has_idle:
            return _noop_watch(M, int(data[0] or 0), timeout, stop)
        tag = M._new_tag()
        M.send(tag + b" IDLE\r\n")
        sock = M.sock
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                return False
            pending = sock.pending() if hasattr(sock, "pending") else 0   # SSL 层里已解密未读的字节
            if not pending:
                ready, _, _ = select.select([sock], [], [], min(10.0, max(0.0, deadline - time.monotonic())))
                if not ready:
                    continue
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError(f"{acct.name}: IDLE 连接被服务器关闭")
            buf += chunk
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                if line.startswith(tag):                  # IDLE 被拒（BAD/NO）
                    raise IdleUnsupported(f"{acct.name}: {line.decode('utf-8', 'replace')}")
                if NEW_MAIL.match(line):
                    return True
        return False
    finally:
        try:
            M.shutdown()
        except Exception:
            pass


def _noop_watch(M: imaplib.IMAP4, count: int, timeout: float, stop: threading.Event | None) -> bool:
    """不支持 IDLE 时：同一条连接上定期 NOOP，邮件数变多就说明来了新邮件。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        nxt = min(deadline, time.monotonic() + NOOP_SECONDS)
        while time.monotonic() < nxt:
            if stop is not None and stop.is_set():
                return False
            time.sleep(min(1.0, max(0.0, nxt - time.monotonic())))
        typ, _ = M.noop()
        if typ != "OK":
            raise RuntimeError(f"NOOP 失败: {typ}")
        _, vals = M.response("EXISTS")
        nums = [int(v) for v in vals or [] if v and v.isdigit()]
        if nums and max(nums) > count:
            return True
    return False
