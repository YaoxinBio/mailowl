"""新邮件快速发现：每个邮箱保持一条已登录的连接，每 WATCH_SECONDS 秒查一次 INBOX 的 UIDNEXT（或邮件总数）。

为什么不用 IMAP IDLE：实测 QQ 邮箱接受 IDLE 却经常不推送（EXAMINE 下从不推送，SELECT 下也时有时无），
科技网 Coremail 干脆不支持 IDLE；而 STATUS 查询在这几家都能在新邮件到达后几秒内反映出来。
STATUS 很轻（不重新登录，每次几百字节），20 秒一次对服务器和代理流量都可以忽略。

这里只负责「发现有新邮件」，真正的收信仍走 fetch.IMAPSync（增量、只读、去重），
所以这里出任何问题都只是退回到定时轮询，不会丢信。连接到时（默认 25 分钟）重建，避免被服务器踢掉。
"""
from __future__ import annotations

import imaplib
import re
import threading
import time
from typing import Callable

from .config import Account
from .fetch import IMAPSync, _quote_mailbox

WATCH_SECONDS = 20


def _snapshot(M: imaplib.IMAP4, folder: str) -> tuple[int | None, int | None]:
    """(UIDNEXT, MESSAGES)。科技网 Coremail 的 STATUS 只回 MESSAGES，不回 UIDNEXT（实测）。"""
    typ, data = M.status(_quote_mailbox(folder), "(UIDNEXT MESSAGES)")
    if typ != "OK" or not data or not data[0]:
        raise RuntimeError(f"STATUS 失败: {typ} {data}")
    nums = {k.decode(): int(v) for k, v in re.findall(rb"(UIDNEXT|MESSAGES) (\d+)", data[0])}
    if not nums:
        raise RuntimeError(f"STATUS 没有 UIDNEXT/MESSAGES: {data[0][:120]!r}")
    return nums.get("UIDNEXT"), nums.get("MESSAGES")


def _has_new(base: tuple[int | None, int | None], cur: tuple[int | None, int | None]) -> bool:
    if base[0] is not None and cur[0] is not None:
        return cur[0] > base[0]                    # UIDNEXT 只增不减，最可靠
    return (cur[1] or 0) > (base[1] or 0)          # 退而看邮件总数（同时有删信可能抵消，定时轮询兜底）


def watch_once(acct: Account, timeout: float, stop: threading.Event | None = None,
               connect: Callable[[Account], imaplib.IMAP4] = IMAPSync.connect, folder: str = "INBOX") -> bool:
    """至多 timeout 秒内盯着 folder：有新邮件返回 True，到时或被要求停止返回 False。"""
    M = connect(acct)
    try:
        base = _snapshot(M, folder)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            nxt = min(deadline, time.monotonic() + WATCH_SECONDS)
            while time.monotonic() < nxt:
                if stop is not None and stop.is_set():
                    return False
                time.sleep(min(1.0, max(0.0, nxt - time.monotonic())))
            if _has_new(base, _snapshot(M, folder)):
                return True
        return False
    finally:
        try:
            M.logout()
        except Exception:
            try:
                M.shutdown()
            except Exception:
                pass
