"""对话 Agent 可用的邮件工具（经 MCP 暴露给 claude -p）。

刻意没有"发送邮件"工具：Agent 只能 save_draft，真正发信只能由用户在飞书草稿卡片上点「确认发送」。
也没有联网工具：需要网上信息时只能 request_research，由用户在飞书上同意后交给读不到邮箱的研究 Agent。
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .attachments import AttachmentError, attachment_from_mail, chain_files, remove_file, store_file
from .config import Config
from .db import DB, now
from .llm import research_backend
from .smtp_send import split_addrs


def _tz(cfg: Config) -> ZoneInfo:
    return ZoneInfo(cfg.timezone)


def fmt_ts(cfg: Config, ts: int | None, with_time: bool = True) -> str:
    if not ts:
        return ""
    d = dt.datetime.fromtimestamp(ts, _tz(cfg))
    return d.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def parse_local_time(cfg: Config, value: str | None) -> int | None:
    """解析 '2026-11-07'、'2026-11-07 17:00'、ISO 8601。只有日期时取当天 18:00。"""
    if not value:
        return None
    v = value.strip().replace("/", "-")
    try:
        d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", v)
        if not m:
            return None
        d = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if len(v) <= 10:
        d = d.replace(hour=18, minute=0)
    if d.tzinfo is None:
        d = d.replace(tzinfo=_tz(cfg))
    return int(d.timestamp())


def mail_brief(cfg: Config, m) -> dict[str, Any]:
    return {
        "id": m["id"], "date": fmt_ts(cfg, m["date_ts"]), "direction": m["direction"],
        "account": m["account"], "from": f'{m["from_name"]} <{m["from_addr"]}>'.strip(),
        "to": m["to_addrs"], "subject": m["subject"], "status": m["status"], "level": m["level"],
        "summary": m["summary"] or ((m["body_text"] or "")[:160].replace("\n", " ")),
        "attachments": [a["filename"] for a in json.loads(m["attachments"] or "[]")],
    }


def my_addresses(cfg: Config) -> set[str]:
    return {a.address.lower() for a in cfg.accounts}


def reply_defaults(cfg: Config, m, reply_all: bool) -> dict[str, str]:
    mine = my_addresses(cfg)
    if m["direction"] == "out":
        to = split_addrs(m["to_addrs"])
    else:
        to = split_addrs(m["reply_to"]) or split_addrs(m["from_addr"])
    cc: list[str] = []
    if reply_all:
        cc = [a for a in split_addrs(m["to_addrs"]) + split_addrs(m["cc_addrs"]) if a not in mine and a not in to]
    subj = m["subject"] or ""
    if not re.match(r"^\s*(re|回复)\s*[:：]", subj, re.I):
        subj = "Re: " + subj
    return {"to": ", ".join(to), "cc": ", ".join(dict.fromkeys(cc)), "subject": subj}


COMMON_CLOSINGS = {"谢谢", "多谢", "感谢", "祝好", "此致", "敬礼", "顺颂商祺", "best regards", "best", "regards",
                   "kind regards", "best wishes", "thanks", "thank you", "sincerely", "yours sincerely"}


def _norm_line(line: str) -> str:
    return re.sub(r"[\s,，.。!！:：;；~～]+", "", line).lower()


def strip_closing(body: str, closings: set[str], lead: str = "") -> str:
    """去掉正文末尾的结尾敬语和署名，避免和落款模板重复。

    整行完全匹配 closings 的删掉；lead 是落款第一句（如「谢谢！」），最后一句话以它结尾时也去掉
    （「我会尽快处理，谢谢！」→「我会尽快处理。」）。
    """
    lines = body.rstrip().split("\n")
    keys = {_norm_line(c) for c in closings} - {""}
    while lines and (not lines[-1].strip() or _norm_line(lines[-1]) in keys):
        lines.pop()
    phrase = re.sub(r"[\s,，.。!！:：;；~～]+$", "", lead.strip())
    if lines and phrase:
        m = re.search(r"[\s,，、;；]*" + re.escape(phrase) + r"[\s,，.。!！~～]*$", lines[-1], re.I)
        if m and m.start() > 0:
            head = lines[-1][:m.start()].rstrip()
            lines[-1] = head + ("" if head[-1:] in "。！？.!?" else ("." if head[-1:].isascii() else "。"))
    return "\n".join(lines).rstrip()


class MailTools:
    def __init__(self, cfg: Config, db: DB, scope: str = ""):
        self.cfg, self.db, self.scope = cfg, db, scope

    # ---- 定义 ----
    def specs(self) -> list[dict[str, Any]]:
        S = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}
        return [
            {"name": "search_mails", "description": "按关键词/发件人/时间检索本地邮件库（含历史与我发出的邮件）。关键词至少 3 个字符时走全文索引。",
             "inputSchema": S({"query": {"type": "string"}, "sender": {"type": "string", "description": "发件人/收件人地址或名字片段"},
                               "account": {"type": "string"}, "days": {"type": "integer", "description": "只看最近 N 天"},
                               "direction": {"type": "string", "enum": ["in", "out"]}, "limit": {"type": "integer"}})},
            {"name": "get_mail", "description": "读取一封邮件的完整信息与正文。",
             "inputSchema": S({"mail_id": {"type": "integer"}, "max_chars": {"type": "integer"}}, ["mail_id"])},
            {"name": "get_thread", "description": "读取某封邮件所在的往来线程（含我方回复），按时间升序，正文截断。",
             "inputSchema": S({"mail_id": {"type": "integer"}, "max_chars_each": {"type": "integer"}}, ["mail_id"])},
            {"name": "list_mails", "description": "列出邮件：pending=重要且未处理；recent=最近收到；needs_reply=需要回复但未回复。",
             "inputSchema": S({"kind": {"type": "string", "enum": ["pending", "recent", "needs_reply"]}, "limit": {"type": "integer"}}, ["kind"])},
            {"name": "save_draft", "description": "保存回复草稿（不会发送）。修改草稿时再次调用即生成新版本。用户会在飞书卡片上审阅并亲自点确认发送。"
                                                 "回复某封邮件时给 mail_id，收件人/主题默认按原邮件推断；写新邮件时不给 mail_id，但必须给 account、to、subject。",
             "inputSchema": S({"mail_id": {"type": "integer"}, "body": {"type": "string", "description": "完整正文（纯文本，含称呼与署名）"},
                               "subject": {"type": "string"}, "to": {"type": "string"}, "cc": {"type": "string"},
                               "reply_all": {"type": "boolean"}, "account": {"type": "string"},
                               "note": {"type": "string", "description": "给用户的一句话说明（例如改了什么）"},
                               **({"signature": {"type": "string", "enum": [x.name for x in self.cfg.signatures] + ["none"],
                                                 "description": "落款模板：body 只写到正文结束（含称呼；不要写「谢谢！」「祝好！」「Best regards」等结尾敬语和署名），"
                                                                "落款由系统按模板追加，用户可在卡片上一键切换。none 表示落款已写在 body 里。"
                                                                "改稿时不给则沿用上一版"}}
                                  if self.cfg.signatures else {})}, ["body"])},
            {"name": "get_draft", "description": "读取某封邮件当前最新的回复草稿（含附件列表）。",
             "inputSchema": S({"mail_id": {"type": "integer"}}, ["mail_id"])},
            {"name": "attach_file_from_mail", "description": "把某封历史邮件（收到的或我发出的）里的附件附到草稿上。附件挂在草稿上，之后改正文也会保留。"
                                                           "先用 get_mail 看附件文件名。",
             "inputSchema": S({"draft_id": {"type": "integer"}, "source_mail_id": {"type": "integer"},
                               "filename": {"type": "string", "description": "附件文件名，可只写一部分"}},
                              ["draft_id", "source_mail_id", "filename"])},
            {"name": "remove_attachment", "description": "从草稿上移除一个附件。",
             "inputSchema": S({"draft_id": {"type": "integer"}, "filename": {"type": "string"}}, ["draft_id", "filename"])},
            {"name": "create_todo", "description": "创建待办，到期前会通过飞书和 Bark 提醒。due 用本地时间，如 2026-11-07 17:00。",
             "inputSchema": S({"title": {"type": "string"}, "due": {"type": "string"}, "mail_id": {"type": "integer"}}, ["title"])},
            {"name": "list_todos", "description": "列出未完成的待办。", "inputSchema": S({})},
            {"name": "update_mail_status", "description": "把邮件标记为 handled（已处理）、ignored（忽略）或 open（重新打开）。",
             "inputSchema": S({"mail_id": {"type": "integer"}, "status": {"type": "string", "enum": ["handled", "ignored", "open"]}}, ["mail_id", "status"])},
            {"name": "set_sender_preference", "description": "记住对某个发件人的偏好：vip=总是重要，mute=只进摘要，normal=恢复默认。",
             "inputSchema": S({"address": {"type": "string"}, "preference": {"type": "string", "enum": ["vip", "normal", "mute"]},
                               "note": {"type": "string"}}, ["address", "preference"])},
        ] + ([
            {"name": "request_research", "description": "需要网上的信息（如会议截稿日期、机构政策、公开资料）时调用：把一个简短、"
                                                      "自成一体的问题交给联网研究助手。问题会先给用户看，用户同意后才执行，结果在下一条消息里给你。"
                                                      "问题里不要抄写邮件正文、地址或其他私人信息。",
             "inputSchema": S({"question": {"type": "string"}}, ["question"])},
        ] if research_backend(self.cfg) else [])

    def handlers(self) -> dict[str, Callable[..., Any]]:
        return {s["name"]: getattr(self, s["name"]) for s in self.specs()}

    def call(self, name: str, args: dict[str, Any]) -> str:
        h = self.handlers().get(name)
        if h is None:
            raise ValueError(f"未知工具 {name}")
        return json.dumps(h(**(args or {})), ensure_ascii=False, default=str)

    # ---- 实现 ----
    def search_mails(self, query: str = "", sender: str = "", account: str = "", days: int = 0,
                     direction: str = "", limit: int = 20):
        since = now() - days * 86400 if days else 0
        rows = self.db.search(query, sender, account, since, 0, direction, min(max(limit, 1), 50))
        return {"count": len(rows), "mails": [mail_brief(self.cfg, r) for r in rows]}

    def get_mail(self, mail_id: int, max_chars: int = 8000):
        m = self.db.mail(mail_id)
        if not m:
            return {"error": f"没有 #{mail_id}"}
        d = mail_brief(self.cfg, m)
        body = m["body_text"] or ""
        d.update(cc=m["cc_addrs"], reply_to=m["reply_to"], analysis=m["analysis"], deadline=m["deadline"],
                 body=body[:max_chars], body_truncated=bool(m["body_truncated"]) or len(body) > max_chars,
                 attachments=json.loads(m["attachments"] or "[]"))
        return d

    def get_thread(self, mail_id: int, max_chars_each: int = 2500):
        rows = self.db.thread(mail_id)
        out = []
        for r in rows:
            b = mail_brief(self.cfg, r)
            b["body"] = (r["body_text"] or "")[:max_chars_each]
            out.append(b)
        return {"count": len(out), "thread": out}

    def list_mails(self, kind: str, limit: int = 20):
        limit = min(max(limit, 1), 50)
        if kind == "pending":
            rows = self.db.q("SELECT * FROM mails WHERE status='open' AND level>=2 ORDER BY date_ts DESC LIMIT ?", (limit,))
        elif kind == "needs_reply":
            rows = self.db.q("SELECT * FROM mails WHERE status='open' AND needs_reply=1 ORDER BY date_ts DESC LIMIT ?", (limit,))
        else:
            rows = self.db.q("SELECT * FROM mails WHERE direction='in' AND status!='history' ORDER BY date_ts DESC LIMIT ?", (limit,))
        return {"count": len(rows), "mails": [mail_brief(self.cfg, r) for r in rows]}

    def save_draft(self, body: str, mail_id: int | None = None, subject: str | None = None, to: str | None = None,
                   cc: str | None = None, reply_all: bool = False, account: str | None = None, note: str | None = None,
                   signature: str | None = None):
        in_reply_to = refs = None
        prev = None
        if mail_id is not None:
            m = self.db.mail(mail_id)
            if not m:
                return {"error": f"没有 #{mail_id}"}
            prev = self.db.latest_draft(mail_id)
            dflt = reply_defaults(self.cfg, m, reply_all)
            if prev is not None and prev["status"] in ("draft", "failed"):
                dflt.update(to=prev["to_addrs"], cc=prev["cc_addrs"], subject=prev["subject"])
            account = m["account"]
            to = to or dflt["to"]
            cc = dflt["cc"] if cc is None else cc
            subject = subject or dflt["subject"]
            in_reply_to = m["message_id"]
            refs = " ".join(x for x in ((m["refs"] or ""), (m["message_id"] or "")) if x).strip() or None
        else:
            if not (account and to and subject):
                return {"error": "写新邮件需要 account、to、subject"}
        try:
            acct = self.cfg.account(account)
        except Exception as e:
            return {"error": str(e)}
        if not acct.can_send:
            return {"error": f"账户 {account} 没有配置 SMTP，无法发信"}
        if not split_addrs(to):
            return {"error": "没有有效的收件人地址"}
        body = body.strip()
        body_main = sig_name = None
        if self.cfg.signatures:
            if signature is None and prev is not None and prev["status"] in ("draft", "failed"):
                signature = prev["sig"]                 # 改稿时没指定落款：沿用上一版
            if signature and signature != "none":
                sig = self.cfg.signature_by_name(signature)
                if sig is None:
                    names = "、".join(x.name for x in self.cfg.signatures)
                    return {"error": f"没有名为 {signature} 的落款，可选：{names}，或 none（落款写在正文里）"}
                closings = set(COMMON_CLOSINGS)
                for x in self.cfg.signatures:
                    closings |= set(x.render(acct).split("\n"))
                closings |= {acct.display_name}
                lead = sig.render(acct).split("\n")[0]
                body_main, sig_name = strip_closing(body, closings, lead), sig.name
                body = f"{body_main}\n\n{sig.render(acct)}"
        d = self.db.create_draft(account=account, mail_id=mail_id, to_addrs=to, cc_addrs=cc or "",
                                 subject=subject, body=body, in_reply_to=in_reply_to, refs=refs,
                                 origin_scope=self.scope, note=note, body_main=body_main, sig=sig_name)
        return {"ok": True, "draft_id": d["id"], "version": d["version"],
                "message": "草稿已保存，将以卡片形式发给用户审阅；不要声称已经发送。"}

    def get_draft(self, mail_id: int):
        d = self.db.latest_draft(mail_id)
        if not d:
            return {"draft": None}
        out = {k: d[k] for k in ("id", "version", "status", "to_addrs", "cc_addrs", "subject", "body")}
        if d["sig"]:  # 正文与落款分开：改稿时只改 body，落款用 signature 参数（不给则沿用）
            out.update(body=d["body_main"], signature=d["sig"])
        out["attachments"] = [{"filename": r["filename"], "size": r["size"]} for r in chain_files(self.db, d["chain_id"])]
        return {"draft": out}

    def _editable_draft(self, draft_id: int):
        d = self.db.draft(draft_id)
        if d is None:
            return None, {"error": f"没有草稿 #{draft_id}"}
        if d["status"] not in ("draft", "failed"):
            return None, {"error": f"草稿 #{draft_id} 状态为 {d['status']}，不能再修改"}
        return d, None

    def attach_file_from_mail(self, draft_id: int, source_mail_id: int, filename: str):
        d, err = self._editable_draft(draft_id)
        if err:
            return err
        try:
            name, data = attachment_from_mail(self.cfg, self.db, source_mail_id, filename)
            f = store_file(self.cfg, self.db, d["chain_id"], name, data, f"mail:{source_mail_id}")
        except AttachmentError as e:
            return {"error": str(e)}
        except Exception as e:  # 连接/登录邮箱服务器失败等
            return {"error": f"从邮箱服务器取附件失败：{type(e).__name__}: {e}"}
        self._touch_draft(d["id"])
        return {"ok": True, "attached": f["filename"], "size": f["size"]}

    def remove_attachment(self, draft_id: int, filename: str):
        d, err = self._editable_draft(draft_id)
        if err:
            return err
        if not remove_file(self.db, d["chain_id"], filename):
            return {"error": f"草稿上没有 {filename}"}
        self._touch_draft(d["id"])
        return {"ok": True}

    def _touch_draft(self, draft_id: int) -> None:
        """附件变化后让主进程刷新草稿卡片。"""
        self.db.x("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (f"draft_dirty:{draft_id}", "1"))

    def create_todo(self, title: str, due: str | None = None, mail_id: int | None = None):
        due_ts = parse_local_time(self.cfg, due) if due else None
        if due and due_ts is None:
            return {"error": f"无法解析时间 {due!r}，请用 2026-11-07 17:00 这种格式"}
        cur = self.db.x("INSERT INTO todos(mail_id,title,due_ts,origin_scope,created_at) VALUES(?,?,?,?,?)",
                        (mail_id, title, due_ts, self.scope, now()))
        return {"ok": True, "todo_id": cur.lastrowid, "due": fmt_ts(self.cfg, due_ts)}

    def list_todos(self):
        rows = self.db.q("SELECT * FROM todos WHERE status='open' ORDER BY COALESCE(due_ts, 9e18)")
        return {"todos": [{"id": r["id"], "title": r["title"], "due": fmt_ts(self.cfg, r["due_ts"]), "mail_id": r["mail_id"]}
                          for r in rows]}

    def update_mail_status(self, mail_id: int, status: str):
        if status not in ("handled", "ignored", "open"):
            return {"error": "status 只能是 handled/ignored/open"}
        if not self.db.mail(mail_id):
            return {"error": f"没有 #{mail_id}"}
        self.db.update_mail(mail_id, status=status, acked_at=now())
        self.db.log_action(f"agent_{status}", mail_id)
        return {"ok": True}

    def set_sender_preference(self, address: str, preference: str, note: str | None = None):
        weight = {"vip": 1, "normal": 0, "mute": -1}.get(preference)
        if weight is None:
            return {"error": "preference 只能是 vip/normal/mute"}
        self.db.set_sender_pref(address, weight, note)
        return {"ok": True}

    def request_research(self, question: str):
        q = (question or "").strip()
        if not q:
            return {"error": "问题不能为空"}
        if len(q) > 500:
            return {"error": "问题太长：请写成一个简短、自成一体的问题（不超过 500 字），不要抄写邮件内容"}
        kind, _, ref = self.scope.partition(":")
        mail_id = int(ref) if kind == "mail" and ref.isdigit() else None
        cur = self.db.x("INSERT INTO research_requests(scope,mail_id,question,created_at) VALUES(?,?,?,?)",
                        (self.scope, mail_id, q, now()))
        return {"ok": True, "request_id": cur.lastrowid,
                "message": "已提交给用户审批。用户同意后，独立的研究助手会上网查询，结果会在下一条消息里交给你。"
                           "现在请用一句话告诉用户你想查什么、为什么。"}
