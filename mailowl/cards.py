"""飞书卡片（JSON 2.0）。

邮件主题、发件人、模型输出等外部文本一律放进 plain_text，避免被当成 markdown/标签解析；
markdown 只用于本程序自己写的固定文字。
按钮回调 value 统一为 {"a": 动作, "m": 邮件 id, "d": 草稿 id, "t": 待办 id}。
"""
from __future__ import annotations

import itertools
import json
import time
from typing import Any

from .triage import LEVEL_NAMES

LEVEL_TEMPLATE = {3: "red", 2: "orange", 1: "blue", 0: "grey"}
# 飞书 SDK 按 (卡片消息, 操作者, 按钮 value) 去重 12 小时；更新卡片后同一个按钮必须带不同的 value，
# 否则「重新发送」「推迟一天」这类第二次点击会被静默丢弃。WS 重投的同一次点击 value 相同，仍会被去重。
_nonce = itertools.count(int(time.time() * 1000))
LEVEL_TAG_COLOR = {3: "red", 2: "orange", 1: "blue", 0: "neutral"}


def _pt(text: str, **kw) -> dict:
    d = {"tag": "plain_text", "content": text}
    d.update(kw)
    return d


def text(content: str, *, size: str | None = None, color: str | None = None) -> dict:
    t = _pt(content)
    if size:
        t["text_size"] = size
    if color:
        t["text_color"] = color
    return {"tag": "div", "text": t}


def note(content: str) -> dict:
    return text(content, size="notation", color="grey")


def md(content: str) -> dict:
    return {"tag": "markdown", "content": content}


def hr() -> dict:
    return {"tag": "hr"}


def button(label: str, value: dict, *, kind: str = "default", confirm: tuple[str, str] | None = None,
           name: str | None = None, submit: bool = False) -> dict:
    b: dict[str, Any] = {"tag": "button", "text": _pt(label), "type": kind,
                         "behaviors": [{"type": "callback", "value": {**value, "n": next(_nonce)}}]}
    if confirm:
        b["confirm"] = {"title": _pt(confirm[0]), "text": _pt(confirm[1])}
    if name:
        b["name"] = name
    if submit:
        b["form_action_type"] = "submit"
    return b


def button_row(buttons: list[dict], simple: bool = False) -> list[dict]:
    if not buttons:
        return []
    if simple:
        return buttons
    return [{"tag": "column_set", "flex_mode": "flow", "horizontal_spacing": "8px",
             "columns": [{"tag": "column", "width": "auto", "elements": [b]} for b in buttons]}]


def card(title: str, template: str, elements: list[dict], *, subtitle: str = "",
         tags: list[tuple[str, str]] | None = None) -> dict:
    header: dict[str, Any] = {"title": _pt(title[:100] or "(无主题)"), "template": template}
    if subtitle:
        header["subtitle"] = _pt(subtitle[:100])
    if tags:
        header["text_tag_list"] = [{"tag": "text_tag", "text": _pt(t[:12]), "color": c} for t, c in tags[:3]]
    return {"schema": "2.0", "config": {"update_multi": True, "width_mode": "fill"},
            "header": header, "body": {"elements": elements}}


def mail_card(m, account_title: str, *, todo: dict | None = None, simple: bool = False, extra_note: str = "") -> dict:
    """todo：这封邮件未完成的待办 {"id", "title", "due"}（due 为已格式化的本地时间或空）。"""
    level = m["level"] if m["level"] is not None else 1
    triage = json.loads(m["triage"] or "{}")
    tags = [(LEVEL_NAMES.get(level, ""), LEVEL_TAG_COLOR.get(level, "neutral"))]
    tags += [(t, "neutral") for t in triage.get("tags", [])[:2]]
    sender = f'{m["from_name"]} <{m["from_addr"]}>' if m["from_name"] else (m["from_addr"] or "")
    els: list[dict] = [note(f"{sender} · {account_title}"), text(m["summary"] or m["subject"] or "")]
    if m["analysis"]:
        els += [md("**AI 分析**"), text(m["analysis"])]
    items = triage.get("action_items") or []
    if items:
        els += [md("**待办事项**"), text("\n".join(f"• {i}" for i in items))]
    if m["deadline"]:
        els.append(md(f"⏰ **截止**：{_safe(m['deadline'])}"))
    if todo:
        due = f"（{todo['due']} 前提醒你）" if todo.get("due") else "（没有截止时间，会列在每日摘要里）"
        els.append(text(f"☐ 已建待办：{todo['title']}{due}"))
    atts = json.loads(m["attachments"] or "[]")
    if atts:
        els.append(text("📎 " + "，".join(f'{a["filename"]}（{_size(a["size"])}）' for a in atts[:6])))
    status = m["status"]
    mid = m["id"]
    if status == "handled":
        els.append(md("✅ **已处理**"))
        btns = [button("查看原文", {"a": "view", "m": mid}), button("起草回复", {"a": "draft", "m": mid}),
                button("重新打开", {"a": "reopen", "m": mid})]
    elif status == "ignored":
        els.append(md("🔕 **已忽略**"))
        btns = [button("查看原文", {"a": "view", "m": mid}), button("重新打开", {"a": "reopen", "m": mid})]
    else:
        btns = [button("查看原文", {"a": "view", "m": mid}),
                button("起草回复", {"a": "draft", "m": mid}, kind="primary"),
                (button("待办完成", {"a": "todo_done", "t": todo["id"]}) if todo
                 else button("创建待办", {"a": "todo", "m": mid})),
                button("已处理", {"a": "done", "m": mid}),
                button("不重要", {"a": "mute", "m": mid})]
    els += [hr(), *button_row(btns, simple)]
    els.append(note(extra_note or f"#{mid} · 直接回复这条消息，就能针对这封邮件和我对话"))
    return card(m["subject"] or "(无主题)", LEVEL_TEMPLATE.get(level, "blue"), els,
                subtitle=f"#{mid} · {LEVEL_NAMES.get(level, '')}", tags=tags)


def draft_card(d, *, account_title: str, files: list | None = None, simple: bool = False,
               sig_names: list[str] | tuple = ()) -> dict:
    did, status = d["id"], d["status"]
    head = f'回复草稿 v{d["version"]}'
    template = "indigo"
    els: list[dict] = [
        text(f'发件邮箱：{account_title}\n收件人：{d["to_addrs"]}' + (f'\n抄送：{d["cc_addrs"]}' if d["cc_addrs"] else "")
             + f'\n主题：{d["subject"]}', size="notation"),
        hr(),
        text(d["body"] or ""),
    ]
    if files:
        els.append(text("📎 " + "，".join(f'{f["filename"]}（{_size(f["size"])}）' for f in files)))
    if d["note"]:
        els.append(note(f'说明：{d["note"]}'))
    btns: list[dict] = []
    if status == "draft" and sig_names and d["sig"]:   # 一键换落款（不调用模型）
        els.append(note("落款"))
        els += button_row([button(("✓ " if n == d["sig"] else "") + n, {"a": "sig", "d": did, "s": n},
                                  kind="primary" if n == d["sig"] else "default") for n in sig_names], simple)
    if status == "draft":
        if not simple:
            els.append({"tag": "form", "name": f"revise_{did}", "elements": [
                {"tag": "input", "name": f"instruction_{did}", "placeholder": _pt("修改意见，例如：第二段缩短，语气更正式"),
                 "max_length": 1000},
                button("按意见修改", {"a": "revise", "d": did}, name=f"revise_btn_{did}", submit=True),
            ]})
        btns = [button("确认发送", {"a": "send", "d": did}, kind="primary_filled",
                       confirm=("确认发送这封邮件？", f'将从 {account_title} 发给 {d["to_addrs"]}'
                                + (f'，带 {len(files)} 个附件' if files else '，没有附件'))),
                button("放弃", {"a": "cancel", "d": did}, kind="danger")]
        els.append(note("回复这条消息可以继续修改；回复时发送文件即可添加附件。只有点「确认发送」才会真正发出。"))
    elif status == "sending":
        head, template = head + " · 发送中", "wathet"
    elif status == "sent":
        head, template = head + " · 已发送", "green"
    elif status == "failed":
        head, template = head + " · 发送失败", "red"
        els.append(text(f'错误：{d["error"] or ""}', color="red"))
        btns = [button("重新发送", {"a": "send", "d": did}, kind="primary",
                       confirm=("重新发送？", "上次发送被服务器拒绝，确认要再试一次吗？")),
                button("放弃", {"a": "cancel", "d": did}, kind="danger")]
    elif status == "unknown":
        head, template = head + " · 结果不确定", "orange"
        els.append(text(f'传送正文时出错，邮件可能已经发出。请先去邮箱「已发送」里确认。\n{d["error"] or ""}', color="orange"))
        btns = [button("确认没发出，重新发送", {"a": "resend_unknown", "d": did}, kind="danger",
                       confirm=("确定没有发出？", "如果其实已经发出，对方会收到两封。")),
                button("已发出，标记完成", {"a": "mark_sent", "d": did})]
    elif status == "superseded":
        head, template = head + " · 已被新版本取代", "grey"
    elif status == "cancelled":
        head, template = head + " · 已放弃", "grey"
    if btns:
        els += button_row(btns, simple)
    return card(head, template, els, subtitle=f'#{d["mail_id"]}' if d["mail_id"] else "新邮件")


def body_card(m, body: str, truncated: bool) -> dict:
    els = [note(f'{m["from_name"]} <{m["from_addr"]}> → {m["to_addrs"]}'), hr(), text(body or "(正文为空)")]
    if truncated:
        els.append(note("正文过长，已截断。完整内容保存在 Mac mini 本地。"))
    return card(f'原文 · {m["subject"] or "(无主题)"}', "grey", els, subtitle=f'#{m["id"]}')


def digest_card(title: str, groups: list[tuple[str, list[str]]], extra: list[str]) -> dict:
    els: list[dict] = []
    for name, lines in groups:
        if not lines:
            continue
        els.append(md(f"**{_safe(name)}**（{len(lines)}）"))
        shown = lines[:25]
        els.append(text("\n".join(shown) + (f"\n…另有 {len(lines) - 25} 封" if len(lines) > 25 else ""), size="notation"))
    if extra:
        els += [hr(), text("\n".join(extra))]
    els.append(note("回复「#编号 你的问题」可以针对某封邮件对话"))
    return card(title, "turquoise", els)


def todo_card(t, due_text: str, mail_subject: str = "", *, simple: bool = False) -> dict:
    els = [text(t["title"])]
    if due_text:
        els.append(md(f"⏰ **到期**：{_safe(due_text)}"))
    if mail_subject:
        els.append(note(f'来自邮件 #{t["mail_id"]}：{mail_subject}'))
    els += button_row([button("完成", {"a": "todo_done", "t": t["id"]}, kind="primary"),
                       button("推迟一天", {"a": "todo_snooze", "t": t["id"]})], simple)
    return card("待办提醒", "yellow", els)


def research_card(r, *, simple: bool = False) -> dict:
    """对话 Agent 申请上网查资料：用户同意后才交给读不到邮箱的研究 Agent。"""
    st, rid = r["status"], r["id"]
    src = f"#{r['mail_id']} 的对话" if r["mail_id"] else "主对话"
    els: list[dict] = [note(f"来自{src} · 研究助手只能上网，看不到你的邮件"), text(r["question"])]
    if st == "pending":
        title, template = "要上网查资料吗？", "wathet"
        els += [hr(), *button_row([button("同意，去查", {"a": "research_go", "r": rid}, kind="primary"),
                                   button("不用了", {"a": "research_no", "r": rid})], simple)]
    elif st == "running":
        title, template = "正在上网查…", "wathet"
    elif st == "done":
        title, template = "上网查资料 · 完成", "green"
        answer = r["answer"] or "（没有结果）"
        els += [hr(), text(answer[:2500] + ("…" if len(answer) > 2500 else ""))]
    elif st == "failed":
        title, template = "上网查资料失败", "red"
        els.append(text(f"⚠️ {r['error'] or '未知错误'}"))
    else:
        title, template = "已取消上网查资料", "grey"
    return card(title, template, els)


def status_card(title: str, lines: list[str], template: str = "blue") -> dict:
    return card(title, template, [text("\n".join(lines))])


def _size(n: int) -> str:
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    return f"{max(1, n // 1024)} KB"


def _safe(s: str) -> str:
    return str(s).replace("<", "＜").replace(">", "＞").replace("*", "＊").replace("[", "［").replace("]", "］")


def simplify(c: dict) -> dict:
    """去掉表单与分栏，作为卡片被飞书拒绝（format_error）时的降级版本。"""
    def walk(elements):
        out = []
        for e in elements:
            if e.get("tag") == "form":
                continue
            if e.get("tag") == "column_set":
                for col in e.get("columns", []):
                    out += walk(col.get("elements", []))
                continue
            out.append(e)
        return out
    c = json.loads(json.dumps(c))
    c["body"]["elements"] = walk(c["body"]["elements"])
    c["header"].pop("text_tag_list", None)
    return c
