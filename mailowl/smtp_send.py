"""SMTP 发信。只会被「确认发送」按钮触发（见 service.Service.send_draft），Agent 没有发信工具。

失败分两类：
- SendRejected：服务器明确拒绝/尚未交付数据（连接、登录、收件人被拒）——可以安全重试；
- SendUncertain：DATA 阶段或之后出错——邮件可能已经发出，不能自动重试，交给用户判断。
"""
from __future__ import annotations

import imaplib
import logging
import mimetypes
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid

from .config import Account
from .fetch import _quote_mailbox

log = logging.getLogger(__name__)


class SendRejected(Exception):
    pass


class SendUncertain(Exception):
    pass


def split_addrs(value: str | None) -> list[str]:
    return [a for _, a in getaddresses([value or ""]) if a and "@" in a]


def build_message(acct: Account, *, to_addrs: str, cc_addrs: str, subject: str, body: str,
                  in_reply_to: str | None, refs: str | None,
                  attachments: list[tuple[str, bytes]] | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((acct.display_name, acct.address)) if acct.display_name else acct.address
    msg["To"] = ", ".join(split_addrs(to_addrs))
    cc = split_addrs(cc_addrs)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=acct.address.split("@")[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        chain = (refs or "").split()
        if in_reply_to not in chain:
            chain.append(in_reply_to)
        msg["References"] = " ".join(chain[-20:])
    msg.set_content(body, charset="utf-8")
    for filename, data in attachments or []:
        ctype, _ = mimetypes.guess_type(filename)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def send_message(acct: Account, msg: EmailMessage, timeout: int = 60) -> None:
    rcpts = split_addrs(msg.get("To")) + split_addrs(msg.get("Cc"))
    if not rcpts:
        raise SendRejected("没有有效的收件人")
    ctx = ssl.create_default_context()
    delivered_data = False
    try:
        if acct.smtp_security == "ssl":
            s = smtplib.SMTP_SSL(acct.smtp_host, acct.smtp_port, timeout=timeout, context=ctx)
        else:
            s = smtplib.SMTP(acct.smtp_host, acct.smtp_port, timeout=timeout)
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
        try:
            s.login(acct.smtp_username, acct.smtp_password)
            code, resp = s.mail(acct.address)
            if code != 250:
                raise SendRejected(f"MAIL FROM 被拒: {code} {resp!r}")
            accepted = 0
            for r in rcpts:
                code, resp = s.rcpt(r)
                if code in (250, 251):
                    accepted += 1
                else:
                    raise SendRejected(f"收件人 {r} 被拒: {code} {resp!r}")
            if not accepted:
                raise SendRejected("所有收件人都被拒绝")
            delivered_data = True
            code, resp = s.data(msg.as_bytes())
            if code != 250:
                # 明确的非 250 回复表示服务器没有接受这封信
                raise SendRejected(f"DATA 被拒: {code} {resp!r}")
        finally:
            try:
                s.quit()
            except Exception:
                pass
    except (SendRejected, SendUncertain):
        raise
    except smtplib.SMTPAuthenticationError as e:
        raise SendRejected(f"SMTP 登录失败: {e}") from e
    except (smtplib.SMTPException, OSError) as e:
        if delivered_data:
            raise SendUncertain(f"传送正文时出错，邮件可能已发出: {e}") from e
        raise SendRejected(f"{type(e).__name__}: {e}") from e


def append_to_sent(acct: Account, msg: EmailMessage) -> None:
    from .fetch import IMAPSync
    M = IMAPSync.connect(acct)
    try:
        M.append(_quote_mailbox(acct.sent_folder), r"(\Seen)", imaplib.Time2Internaldate(time.time()),
                 msg.as_bytes())
    finally:
        try:
            M.logout()
        except Exception:
            pass
