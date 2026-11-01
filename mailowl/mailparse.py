"""MIME 解析：头部、纯文本正文、附件清单、线程键。附件本身不落盘（Mac mini 磁盘紧张）。"""
from __future__ import annotations

import email
import email.header
import email.utils
import html
import re
import time
from email import policy
from email.message import Message
from html.parser import HTMLParser
from typing import Any

_CHARSET_ALIASES = {"gb2312": "gb18030", "gbk": "gb18030", "gb_2312-80": "gb18030", "x-gbk": "gb18030",
                    "ks_c_5601-1987": "cp949", "unknown-8bit": "utf-8", "us-ascii": "utf-8"}
_SUBJECT_PREFIX = re.compile(r"^\s*((re|fw|fwd|回复|答复|转发|aw|sv|wg)\s*(\[\d+\])?\s*[:：]\s*)+", re.I)


def _decode_bytes(data: bytes, charset: str | None) -> str:
    cs = (charset or "utf-8").lower().strip('"')
    cs = _CHARSET_ALIASES.get(cs, cs)
    for c in (cs, "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(c)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def decode_header_value(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, email.header.Header):
        value = str(value)
    try:
        parts = email.header.decode_header(value)
    except Exception:
        return str(value)
    out = []
    for text, cs in parts:
        out.append(_decode_bytes(text, cs) if isinstance(text, bytes) else text)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _raw_header(msg: Message, name: str) -> str:
    """compat32 下含 8bit 字节的头会返回 Header 对象，统一在这里解码；不用 policy.default，
    它的结构化头遇到畸形值会抛异常。"""
    try:
        raw = msg.get(name)
    except Exception:
        raw = None
    if raw is None:
        return ""
    return decode_header_value(raw)


def _addresses(value: str) -> list[tuple[str, str]]:
    try:
        pairs = email.utils.getaddresses([value])
    except Exception:
        return []
    return [(decode_header_value(n), a.strip().lower()) for n, a in pairs if a and "@" in a]


def format_addrs(pairs: list[tuple[str, str]]) -> str:
    return ", ".join(a for _, a in pairs)


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote", "section"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("• ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(src: str) -> str:
    p = _HTMLText()
    try:
        p.feed(src)
        p.close()
        text = "".join(p.out)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", src)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        p = part.get_payload()
        return p if isinstance(p, str) else ""
    return _decode_bytes(payload, part.get_content_charset())


def _filename(part: Message) -> str | None:
    try:
        fn = part.get_filename()
    except Exception:
        fn = part.get_param("name")
    return decode_header_value(fn) if fn else None


def extract_body(msg: Message) -> tuple[str, list[dict[str, Any]]]:
    plain, htmls, attachments = [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = (part.get("Content-Disposition") or "").split(";")[0].strip().lower()
        fn = _filename(part)
        if disp == "attachment" or (fn and not ctype.startswith("text/")) or ctype.startswith(("image/", "application/", "audio/", "video/")):
            raw = part.get_payload(decode=True) or b""
            attachments.append({"filename": fn or "(未命名)", "type": ctype, "size": len(raw)})
            continue
        if ctype == "text/plain":
            plain.append(_part_text(part))
        elif ctype == "text/html":
            htmls.append(_part_text(part))
    if any(p.strip() for p in plain):
        body = "\n\n".join(p.strip() for p in plain if p.strip())
    elif htmls:
        body = "\n\n".join(html_to_text(h) for h in htmls)
    else:
        body = ""
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    return body.strip(), attachments


def normalize_subject(subject: str) -> str:
    return _SUBJECT_PREFIX.sub("", subject or "").strip().lower()


def parse_message(raw: bytes, *, truncated: bool = False) -> dict[str, Any]:
    msg = email.message_from_bytes(raw, policy=policy.compat32)
    subject = _raw_header(msg, "Subject")
    from_pairs = _addresses(_raw_header(msg, "From"))
    to_pairs = _addresses(_raw_header(msg, "To"))
    cc_pairs = _addresses(_raw_header(msg, "Cc"))
    reply_pairs = _addresses(_raw_header(msg, "Reply-To"))
    message_id = (msg.get("Message-ID") or "").strip() or None
    in_reply_to = None
    irt = re.findall(r"<[^>]+>", msg.get("In-Reply-To") or "")
    if irt:
        in_reply_to = irt[0]
    refs_list = re.findall(r"<[^>]+>", msg.get("References") or "")
    date_ts = None
    try:
        d = email.utils.parsedate_to_datetime(msg.get("Date"))
        date_ts = int(d.timestamp())
    except Exception:
        pass
    body, attachments = extract_body(msg)
    if refs_list:
        thread_key = refs_list[0]
    elif in_reply_to:
        thread_key = in_reply_to
    else:
        thread_key = message_id or ("subj:" + normalize_subject(subject))
    return {
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "refs": " ".join(refs_list) or None,
        "thread_key": thread_key,
        "from_name": from_pairs[0][0] if from_pairs else "",
        "from_addr": from_pairs[0][1] if from_pairs else "",
        "to_addrs": format_addrs(to_pairs),
        "cc_addrs": format_addrs(cc_pairs),
        "reply_to": format_addrs(reply_pairs),
        "subject": subject,
        "date_ts": date_ts or int(time.time()),
        "size": len(raw),
        "body_text": body,
        "body_truncated": 1 if truncated else 0,
        "attachments": attachments,
    }
